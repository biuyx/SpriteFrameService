"""导出不占抠图线程；相同动作和总内存预算依然串行保护。"""
import threading
import time
import uuid

import pytest

from app.services.concurrency import ConcurrencyGate
from app.services.job_manager import JobManager, JobStatus
from app.services.resources import ResourceCoordinator


def wait_for(pred):
    end = time.monotonic() + 5
    while not pred() and time.monotonic() < end:
        time.sleep(.01)
    assert pred()


def manager():
    coord = ResourceCoordinator(ConcurrencyGate(lambda: 1), ConcurrencyGate(lambda: 1), lambda: 1000)
    return JobManager(max_workers=1, coordinator=coord), coord


def test_other_action_export_runs_with_matting_without_freeing_heavy_slot():
    jm, coord = manager()
    release = threading.Event()
    a = jm.submit('background', lambda ctx: release.wait(5), target='a', memory_mb=600)
    wait_for(lambda: a.status == JobStatus.RUNNING)
    same = jm.submit('export', lambda ctx: 'same', target='a', memory_mb=100)
    other = jm.submit('export', lambda ctx: 'other', target='b', memory_mb=100)
    wait_for(lambda: other.finished_at is not None)
    assert same.status == JobStatus.QUEUED
    assert '同动作' in same.message
    assert coord.heavy_gate.active == 1
    release.set()
    wait_for(lambda: same.finished_at is not None and a.finished_at is not None)
    assert coord.status()['reserved_memory_mb'] == 0
    jm.close()


def test_large_export_waits_for_memory_small_export_can_pass():
    jm, coord = manager()
    release = threading.Event()
    a = jm.submit('background', lambda ctx: release.wait(5), target='a', memory_mb=600)
    wait_for(lambda: a.status == JobStatus.RUNNING)
    big = jm.submit('export', lambda ctx: 'big', target='b', memory_mb=500)
    small = jm.submit('export', lambda ctx: 'small', target='c', memory_mb=100)
    wait_for(lambda: small.finished_at is not None)
    assert big.status == JobStatus.QUEUED and '内存预算' in big.message
    assert jm.cancel(big.id)
    release.set()
    wait_for(lambda: a.finished_at is not None)
    assert big.status == JobStatus.CANCELLED
    assert coord.status()['reserved_memory_mb'] == 0
    jm.close()


def test_pipeline_reservation_and_api_use_same_target_and_memory_coordinator():
    jm, coord = manager()
    from conftest import FakeCtx
    with coord.hold(FakeCtx(), 'heavy', ['pipeline-action'], 600):
        blocked = jm.submit('export', lambda ctx: 1, target='pipeline-action', memory_mb=100)
        free = jm.submit('export', lambda ctx: 1, target='another-action', memory_mb=100)
        wait_for(lambda: free.finished_at is not None)
        assert blocked.status == JobStatus.QUEUED
    wait_for(lambda: blocked.finished_at is not None)
    jm.close()


def test_low_real_memory_does_not_evict_a_model_in_use(monkeypatch):
    import app.services.resources as module
    from app.core.background_remover import BackgroundRemover
    monkeypatch.setattr(module, 'available_memory_mb', lambda: 100)
    evictions = []
    monkeypatch.setattr(BackgroundRemover, 'release_idle_cache', lambda: evictions.append(1))
    _, coord = manager()
    assert coord.try_acquire('matting', ['a'], 600) is None
    assert '实际可用内存' in coord.try_acquire('export', ['b'], 100)
    assert not evictions
    coord.release('matting', ['a'], 600)
    assert '实际可用内存' in coord.try_acquire('export', ['b'], 100)
    assert evictions == [1]


def test_idle_cached_model_is_released_for_export(monkeypatch):
    import app.services.resources as module
    from app.core.background_remover import BackgroundRemover
    free = {'mb': 100}
    monkeypatch.setattr(module, 'available_memory_mb', lambda: free['mb'])
    monkeypatch.setattr(BackgroundRemover, 'release_idle_cache', lambda: free.update(mb=2000))
    _, coord = manager()
    assert coord.try_acquire('export', ['a'], 800) is None
    coord.release('export', ['a'], 800)


@pytest.fixture
def tiny_export_session():
    import numpy as np
    from app.api.deps import get_session
    from app.models.frame_data import FrameData
    from app.services.sprite_store import sprite_store
    sp = sprite_store.create_sprite('export_budget_' + uuid.uuid4().hex[:8])
    action = sprite_store.create_action(sp['id'], 'walk')
    session = get_session(action['id'])
    frame = FrameData(index=0, timestamp=0, image=np.full((32, 32, 4), 255, np.uint8))
    session.frame_store.save_raw_frames([frame])
    session.frame_manager.add_frames([frame])
    session.persist_metadata()
    return session


@pytest.mark.parametrize('format_name,options', [
    ('sprite_sheet', {'sprite_config': {'frame_width': 65536, 'frame_height': 65536}}),
    ('webp', {'sprite_config': {'frame_width': 65536, 'frame_height': 65536}}),
    ('webp', {'sprite_config': None, 'webp_config': {'frame_width': 65536, 'frame_height': 65536}}),
    ('gif', {'gif_config': {'frame_width': 65536, 'frame_height': 65536}}),
    ('godot', {'godot_config': {'frame_width': 65536, 'frame_height': 65536}}),
])
def test_format_resize_rejected_before_export_or_image_decode(tiny_export_session, monkeypatch, format_name, options):
    from app.api import export_api
    from app.api.schemas import ExportRequest
    from app.core.exporter import Exporter
    from app.models.export_config import ExportConfig
    from app.services.job_manager import JobSubmissionError
    coord = ResourceCoordinator(budget=lambda: 18432)
    jm = JobManager(coordinator=coord)
    monkeypatch.setattr(export_api, 'job_manager', jm)
    monkeypatch.setattr(Exporter, 'export', lambda *a: pytest.fail('oversize export must not start'))
    monkeypatch.setattr(tiny_export_session, 'load_display_arrays',
                        lambda *a: pytest.fail('budget check must happen before pixel decoding'))
    try:
        with pytest.raises(JobSubmissionError, match='内存超过预算'):
            export_api.create_export(tiny_export_session.id, ExportRequest(
                config=ExportConfig(format=format_name, **options), indices=[0]))
        assert jm.list() == []
    finally:
        # Ensure failing regressions cannot leave an asynchronous task behind.
        for job in jm.list():
            jm.cancel(job['id'])
        wait_for(lambda: all(j['finished_at'] for j in jm.list()))
        jm.close()


@pytest.mark.parametrize('format_name', ['sprite_sheet', 'webp'])
def test_sheet_estimate_includes_padding_and_empty_columns(tiny_export_session, format_name):
    from app.models.export_config import ExportConfig
    from app.services.resources import estimate_export_mb
    cfg = ExportConfig(format=format_name, sprite_config={'columns': 100000000, 'padding': 32})
    # A single input still allocates the full sheet width, including empty columns.
    actual_sheet_bytes = (100000000 * (32 + 32) - 32) * 32 * 4
    assert estimate_export_mb(tiny_export_session, [0], cfg) * 1024**2 >= actual_sheet_bytes
