"""自动流水线：对满足条件的动作，按工序顺序一键执行 首帧 → 视频生成 → 抽帧 → 抠图 → 导出。

设计要点：
- 每个动作一条流水线任务（io 池），按选定步骤顺序直接调用各步核心函数；
- 步骤产物已存在则跳过（除非 force），因此"再次执行"天然从失败/未完成处续跑；
- 首帧生成后可自动暂停等人工确认：状态记入动作档案（action.json["pipeline"]），
  任务结束不占线程，确认后 resume 起新任务续跑；
- 视频生成走现有并发闸门；本地重活（抽帧/抠图/导出）用本地信号量限流；
- 执行前 plan() 给出每步 run/skip/blocked 及原因，供界面预览与费用估算。
"""
from __future__ import annotations

import re
import threading
import time
from typing import List, Optional

from app.api.deps import get_session
from app.services.sprite_store import sprite_store
from app.services.template_store import template_store

STEPS = ["firstframe", "generate", "extract", "matting", "export"]
STEP_LABEL = {"firstframe": "首帧", "generate": "视频生成", "extract": "抽帧",
              "matting": "抠图", "export": "导出"}

# 本地重活并发（与 cpu 池规模一致），避免十几个动作同时抠图打爆机器
_local_gate = threading.Semaphore(2)

DEFAULT_OUTPUT_PRESET = {          # 精灵导出预设缺省：序列帧 PNG
    "format": "frames",
    "pngquant_config": {"enabled": False, "quality_min": 60, "quality_max": 80},
    "loop_transition": {"enabled": False, "count": 5, "mode": "blend"},
    "name_pattern": "{sprite}_{action}",
}


class StepBlocked(Exception):
    """前置条件不满足（消息即原因）。"""


# ------------------------------------------------------------ 状态读写
def _state(action: dict) -> dict:
    return dict(action.get("pipeline") or {})


def _save_state(sprite_id: str, action_id: str, state: dict) -> None:
    state["updated_at"] = time.time()
    sprite_store.update_action(sprite_id, action_id, {"pipeline": state})


def _sanitize(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-一-鿿]", "_", name) or "export"


# ------------------------------------------------------------ 各步：检查 / 执行
def _pick_ffset(action: dict, key: str, set_id: Optional[str]) -> Optional[dict]:
    """参考首帧集：指定优先；否则分组匹配的、再否则任一含该 key 的。"""
    from app.services.ffset_store import ffset_store
    sets = ffset_store.list()
    if set_id:
        s = next((x for x in sets if x["id"] == set_id), None)
        return s if s and any(f["key"] == key for f in s["frames"]) else None
    tpl = template_store.get(action.get("template_id") or "")
    group = ((tpl or {}).get("group") or "").strip()
    cands = [s for s in sets if any(f["key"] == key for f in s["frames"])]
    if group:
        g = [s for s in cands if (s.get("group") or "").strip() == group]
        if g:
            return g[0]
    return cands[0] if cands else None


def check_firstframe(sprite_id: str, action: dict, opts: dict):
    from app.core.first_frame_generator import action_key, resolve_refs
    key = action_key(action)
    fs = _pick_ffset(action, key, opts.get("set_id"))
    if fs is None:
        raise StepBlocked(f"参考首帧集缺少动作 {key or '?'}")
    resolve_refs(sprite_id, action, fs["id"])      # 立绘缺失等 ValueError
    return {"set_id": fs["id"], "set_name": fs["name"]}


def check_generate(sprite_id: str, action: dict, opts: dict):
    from app.config import get_settings
    from app.core.oss_uploader import oss_configured
    s = get_settings()
    if not s.generate_enabled:
        raise StepBlocked("未配置 Ark API Key")
    if not (sprite_store.action_dir(sprite_id, action["id"]) / "first_frame.png").is_file():
        raise StepBlocked("缺少首帧（将由上一步生成）")
    tpl = template_store.get(action.get("template_id") or "")
    if tpl is None:
        raise StepBlocked("未关联动作模板（参考视频）")
    if not oss_configured():
        raise StepBlocked("未配置 OSS（参考视频需公网 URL）")
    return {"template": tpl.get("variant") or tpl.get("key")}


def _rule_for(session, action: dict):
    from app.services.take_store import TakeStore
    ts = TakeStore(session.storage)
    cur = ts.get(ts.current_id()) if ts.current_id() else None
    tid = (cur or {}).get("template_id") or action.get("template_id")
    tpl = template_store.get(tid) if tid else None
    return tpl, (tpl or {}).get("extract_rule")


def check_extract(sprite_id: str, action: dict, opts: dict, session=None):
    session = session or get_session(action["id"])
    if session.video_info is None:
        raise StepBlocked("无视频素材（将由上一步生成）")
    tpl, rule = _rule_for(session, action)
    if not rule:
        raise StepBlocked("模板无抽帧规则")
    return {"rule": f"{rule['start']}–{rule['end']}s @{rule['fps']}"}


def check_matting(sprite_id: str, action: dict, opts: dict, session=None):
    from app.core.background_remover import BackgroundRemover
    sp = sprite_store.get_sprite(sprite_id)
    m = (sp.get("preset") or {}).get("matting") or {}
    model = m.get("model") or BackgroundRemover.default_model()
    if BackgroundRemover.get_model_path(model) is None:
        raise StepBlocked(f"抠图模型未安装: {model}")
    session = session or get_session(action["id"])
    if session.frame_manager.frame_count == 0:
        raise StepBlocked("无帧（将由上一步抽出）")
    return {"model": model}


def check_export(sprite_id: str, action: dict, opts: dict, session=None):
    session = session or get_session(action["id"])
    frames = session.frame_manager.frames
    if not frames:
        raise StepBlocked("无帧（将由上一步抽出）")
    if not any(f.has_processed for f in frames):
        raise StepBlocked("没有已抠图的帧（将由上一步生成）")
    sp = sprite_store.get_sprite(sprite_id)
    out = (sp.get("preset") or {}).get("output") or DEFAULT_OUTPUT_PRESET
    return {"format": out.get("format", "frames")}


def _is_done(step: str, sprite_id: str, action: dict, session=None) -> bool:
    d = sprite_store.action_dir(sprite_id, action["id"])
    if step == "firstframe":
        return (d / "first_frame.png").is_file()
    session = session or get_session(action["id"])
    if step == "generate":
        return session.video_info is not None
    frames = session.frame_manager.frames
    if step == "extract":
        return len(frames) > 0
    if step == "matting":
        return bool(frames) and all(f.has_processed for f in frames)
    if step == "export":
        s = sprite_store._action_summary(sprite_id, action["id"])
        return bool(s.get("export_count"))
    return False


def plan_action(sprite_id: str, action: dict, opts: dict) -> dict:
    """每步 run / skip / blocked。后续步骤依赖前步产物时，前步将执行则视为满足。"""
    steps: List[str] = [s for s in STEPS if s in (opts.get("steps") or STEPS)]
    force = set(opts.get("force") or [])
    session = None
    try:
        session = get_session(action["id"])
    except Exception:
        pass
    result, will_have = {}, {}
    for step in steps:
        try:
            done = _is_done(step, sprite_id, action, session)
        except Exception:
            done = False
        if done and step not in force:
            result[step] = {"status": "skip", "reason": "已完成"}
            will_have[step] = True
            continue
        try:
            checker = {"firstframe": check_firstframe, "generate": check_generate,
                       "extract": check_extract, "matting": check_matting,
                       "export": check_export}[step]
            info = (checker(sprite_id, action, opts) if step in ("firstframe", "generate")
                    else checker(sprite_id, action, opts, session))
            result[step] = {"status": "run", **info}
            will_have[step] = True
        except (StepBlocked, ValueError) as e:
            msg = str(e)
            # 依赖上一步产物：上一步将执行则放行（执行期会再校验）
            prev = {"generate": "firstframe", "extract": "generate",
                    "matting": "extract", "export": "matting"}.get(step)
            if "将由上一步" in msg and prev and will_have.get(prev):
                result[step] = {"status": "run", "note": "依赖上一步产物"}
                will_have[step] = True
            else:
                result[step] = {"status": "blocked", "reason": msg}
                will_have[step] = False
        except Exception as e:
            result[step] = {"status": "blocked", "reason": f"检查失败: {e}"}
            will_have[step] = False
    runnable = any(v["status"] == "run" for v in result.values())
    return {"steps": result, "runnable": runnable}


# ------------------------------------------------------------ 执行
def run_pipeline(sprite_id: str, action_id: str, ctx) -> dict:
    """按动作档案里的 pipeline 记录执行（start/resume 都走这里）。"""
    action = sprite_store.get_action(sprite_id, action_id)
    state = _state(action)
    steps = [s for s in STEPS if s in (state.get("steps") or STEPS)]
    force = set(state.get("force") or [])
    done = list(state.get("done") or [])
    opts = {"set_id": state.get("set_id"), "steps": steps, "force": list(force)}
    total = len(steps)
    state.update({"status": "running", "error": None, "job_id": ctx.job_id if hasattr(ctx, "job_id") else None})
    _save_state(sprite_id, action_id, state)

    def stage(i, msg, pct=0):
        ctx.report(min(99, (i + pct / 100) / total * 100), f"[{i + 1}/{total}] {msg}")

    class _Sub:
        """子步骤的 ctx：进度映射到总进度区间。"""
        def __init__(self, i): self.i = i
        def report(self, pct, msg=""): stage(self.i, f"{STEP_LABEL[steps[self.i]]} {msg}", pct or 0)
        def cancelled(self): return ctx.cancelled()
        def register_cancel(self, fn): ctx.register_cancel(fn)

    try:
        for i, step in enumerate(steps):
            if ctx.cancelled():
                raise RuntimeError("已取消")
            action = sprite_store.get_action(sprite_id, action_id)
            if step in done:
                continue
            session = get_session(action_id)
            if step != "firstframe" and _is_done(step, sprite_id, action, session) and step not in force:
                done.append(step); state["done"] = done; _save_state(sprite_id, action_id, state)
                continue
            if step == "firstframe" and _is_done(step, sprite_id, action) and step not in force:
                done.append(step); state["done"] = done; _save_state(sprite_id, action_id, state)
                continue
            state["current"] = step
            _save_state(sprite_id, action_id, state)
            stage(i, STEP_LABEL[step] + "…")

            if step == "firstframe":
                from app.core.first_frame_generator import run_gen_first_frame
                info = check_firstframe(sprite_id, action, opts)
                run_gen_first_frame(sprite_id, action_id, info["set_id"], None, _Sub(i), remember=True)
                done.append(step); state["done"] = done
                if state.get("pause_after_firstframe"):
                    state.update({"status": "paused", "current": None,
                                  "paused_after": "firstframe"})
                    _save_state(sprite_id, action_id, state)
                    ctx.report(100, "首帧已生成，等待确认后继续")
                    return {"status": "paused", "done": done}
            elif step == "generate":
                from app.api.generate import _default_generate_payload, generate_gate
                from app.config import get_settings
                from app.core.video_generator import run_generate
                check_generate(sprite_id, action, opts)
                tpl = template_store.get(action.get("template_id"))
                s = get_settings()
                payload = _default_generate_payload(
                    session, action, tpl,
                    state.get("model") or s.ark_model,
                    state.get("resolution") or "480p", "adaptive")
                generate_gate.acquire(ctx)
                try:
                    run_generate(session, payload, _Sub(i))
                finally:
                    generate_gate.release()
                session = get_session(action_id)
                done.append(step); state["done"] = done
            elif step == "extract":
                from app.api.frames import run_extract
                tpl, rule = _rule_for(session, action)
                if session.video_info is None:
                    raise StepBlocked("无视频素材")
                if not rule:
                    raise StepBlocked("模板无抽帧规则")
                end = min(float(rule["end"]), session.video_info.duration)
                with _local_gate, session.lock:
                    run_extract(session, float(rule["start"]), end, float(rule["fps"]),
                                rule.get("keep"), rule.get("total"), _Sub(i))
                done.append(step); state["done"] = done
            elif step == "matting":
                from app.api.background import run_bg_remove
                info = check_matting(sprite_id, action, opts, session)
                sp = sprite_store.get_sprite(sprite_id)
                m = dict((sp.get("preset") or {}).get("matting") or {})
                params = {"model": info["model"], "alpha_threshold": m.get("alpha_threshold", 128),
                          "erode": m.get("erode", 1), "feather": m.get("feather", 0),
                          "force_cpu": False}
                frames = session.frame_manager.frames
                indices = [f.index for f in frames
                           if step in force or not f.has_processed]
                if indices:
                    with _local_gate, session.lock:
                        run_bg_remove(session, indices, "ai", params, _Sub(i))
                done.append(step); state["done"] = done
            elif step == "export":
                from app.api.export_api import run_export
                from app.models.export_config import ExportConfig
                check_export(sprite_id, action, opts, session)
                sp = sprite_store.get_sprite(sprite_id)
                out = dict((sp.get("preset") or {}).get("output") or DEFAULT_OUTPUT_PRESET)
                pattern = out.pop("name_pattern", None) or "{sprite}_{action}"
                name = _sanitize(pattern.replace("{sprite}", sp.get("name", "sprite"))
                                 .replace("{action}", action.get("name", "action")))
                cfg = ExportConfig(**{k: v for k, v in out.items()
                                      if k not in ("output_path", "output_name", "frame_indices")},
                                   output_name=name)
                indices = [f.index for f in session.frame_manager.frames if f.has_processed]
                with _local_gate, session.lock:
                    run_export(session, cfg, indices, name, _Sub(i))
                done.append(step); state["done"] = done
            _save_state(sprite_id, action_id, state)

        state.update({"status": "done", "current": None})
        _save_state(sprite_id, action_id, state)
        ctx.report(100, "流水线完成")
        return {"status": "done", "done": done}
    except Exception as e:
        msg = str(e)
        state.update({"status": "error", "error": f"{STEP_LABEL.get(state.get('current'), '')}: {msg}"[:300]})
        _save_state(sprite_id, action_id, state)
        raise
