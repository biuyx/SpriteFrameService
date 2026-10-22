"""本地处理资源：独立导出名额、共享内存预算、跨入口的动作互斥。"""
from __future__ import annotations
import math
import os
import sys
import threading
import time
from contextlib import contextmanager

from app.config import get_settings
from app.services.concurrency import ConcurrencyGate, local_gate


def available_memory_mb():
    try:
        with open('/proc/meminfo') as source:
            for line in source:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return float('inf')


def memory_budget_mb() -> int:
    explicit = get_settings().local_memory_budget_mb
    if explicit:
        return explicit
    try:
        total = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
    except (AttributeError, ValueError, OSError):
        import ctypes
        class MemoryStatus(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in ('total', 'available', 'page_total',
                'page_available', 'virtual_total', 'virtual_available', 'extended')]
        state = MemoryStatus()
        state.length = ctypes.sizeof(state)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state))
        total = state.total
    return max(256, int(total / 1024**2 * .85))


class ResourceCoordinator:
    def __init__(self, heavy_gate=None, export_gate=None, budget=None):
        self.heavy_gate = heavy_gate if heavy_gate is not None else local_gate
        self.export_gate = export_gate if export_gate is not None else ConcurrencyGate(
            lambda: get_settings().export_workers, '导出')
        self.budget = budget or memory_budget_mb
        self._lock = threading.Lock()
        self._targets = set()
        self._claims = {}
        self._used = 0

    def try_acquire(self, resource, targets, memory_mb):
        gate = self.export_gate if resource == 'export' else self.heavy_gate
        with self._lock:
            if self._targets.intersection(targets):
                return '等待同动作的其他任务完成'
            if self._used + memory_mb > self.budget():
                return f'排队中（内存预算不足，已预留 {self._used}/{self.budget()} MiB）'
            if available_memory_mb() < memory_mb + 512:
                # 模型的 arena 会在任务结束后继续常驻。没有抠图在使用时才可释放缓存。
                module = sys.modules.get('app.core.background_remover')
                if self.heavy_gate.active == 0 and module is not None:
                    module.BackgroundRemover.release_idle_cache()
                if available_memory_mb() < memory_mb + 512:
                    return '排队中（实际可用内存不足，等待其他任务释放内存）'
            if not gate.try_acquire():
                return '排队中（导出名额已满）' if resource == 'export' else '排队中（本地处理名额已满）'
            self._targets.update(targets)
            self._claims.update({target: memory_mb for target in targets})
            self._used += memory_mb
        return None

    def release(self, resource, targets, memory_mb):
        gate = self.export_gate if resource == 'export' else self.heavy_gate
        with self._lock:
            self._targets.difference_update(targets)
            for target in targets:
                self._claims.pop(target, None)
            self._used -= memory_mb
            gate.release()

    def check_estimate(self, target, amount):
        with self._lock:
            reserved = self._claims.get(target)
        if reserved is not None and amount > reserved:
            raise ValueError('排队期间素材尺寸或数量已变化，请重新提交任务以重新预留内存')

    @contextmanager
    def hold(self, ctx, resource='heavy', targets=(), memory_mb=None):
        amount = memory_mb or get_settings().matting_memory_mb
        if amount > self.budget():
            raise ValueError('该任务预计内存超过本地处理预算，请减少帧数或导出尺寸')
        while True:
            if ctx.cancelled():
                raise RuntimeError('已取消')
            reason = self.try_acquire(resource, targets, amount)
            if reason is None:
                break
            ctx.report(0, reason)
            time.sleep(.25)
        try:
            yield
        finally:
            self.release(resource, targets, amount)

    def status(self):
        with self._lock:
            available = available_memory_mb()
            return {'heavy_active': self.heavy_gate.active, 'export_active': self.export_gate.active,
                    'reserved_memory_mb': self._used, 'memory_budget_mb': self.budget(),
                    'available_memory_mb': available if math.isfinite(available) else None,
                    'active_actions': sorted(self._targets)}


resources = ResourceCoordinator()


def estimate_extract_mb(session, start, end, fps):
    info = session.video_info
    count = max(1, int((end - start) * fps) + 1)
    return 256 + math.ceil(count * info.width * info.height * 8 / 1024**2)


def estimate_export_mb(session, indices, config) -> int:
    """只读图头，计入尺寸处理、格式专属缩放、混合尺寸归一化和完整图集。"""
    from PIL import Image
    total = 0
    sizes = []
    scale = getattr(config, 'scale', None)
    outline = getattr(config, 'outline', None)
    for index in indices:
        frame = session.frame_manager.get_frame(index)
        path = session.frame_store.proc_path(frame.id)
        if not path.is_file():
            path = session.frame_store.raw_path(frame.id)
        with Image.open(path) as image:
            width, height = image.size
        pixels = width * height
        if scale is not None and scale.enabled:
            if scale.mode == 'percent':
                width = max(1, math.ceil(width * scale.percent / 100))
                height = max(1, math.ceil(height * scale.percent / 100))
            else:
                width, height = max(width, scale.width), max(height, scale.height)
        if outline is not None and outline.enabled:
            # required_pad rounds each side up independently.
            width += 2 * math.ceil(outline.width)
            height += 2 * math.ceil(outline.width)
        total += pixels * 8 + width * height * 48
        sizes.append((width, height))

    if not sizes:
        return 256
    fmt = config.format
    sheet = config.sprite_config if fmt in ('sprite_sheet', 'webp') else None
    format_cfg = (sheet if sheet is not None else config.webp_config if fmt == 'webp'
                  else config.gif_config if fmt == 'gif'
                  else config.godot_config if fmt == 'godot' and config.godot_config.export_individual_frames
                  else None)
    if format_cfg is not None:
        # Exporters resize again *after* scale/outline; these buffers coexist
        # with the processed frame arrays. Count both stages, even when downsizing.
        if format_cfg.frame_width and format_cfg.frame_height:
            sizes = [(max(1, format_cfg.frame_width), max(1, format_cfg.frame_height))] * len(sizes)
        format_pixels = sum(w * h for w, h in sizes)
        if sheet is not None:
            width, height = sizes[0]
            count = len(sizes)
            # Both PNG and WebP sheets normalize every cell to the first frame.
            format_pixels = max(format_pixels, count * width * height)
            if sheet.layout == 'horizontal':
                cols, rows = count, 1
            elif sheet.layout == 'vertical':
                cols, rows = 1, count
            else:
                cols = max(1, sheet.columns or math.ceil(math.sqrt(count)))
                rows = math.ceil(count / cols)
            padding = max(0, sheet.padding)
            sheet_width = cols * (width + padding) - padding
            sheet_height = rows * (height + padding) - padding
            total += sheet_width * sheet_height * 16
        total += format_pixels * 32
    return 256 + math.ceil(total / 1024**2)
