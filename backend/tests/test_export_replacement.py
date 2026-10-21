"""真实小图导出回归：缩短/选帧/换格式不混旧文件，失败和取消保留旧产物。"""
from pathlib import Path
import hashlib
import json

import pytest


@pytest.fixture
def prepared(tmp_path, rgba):
    from app.services.session import Session
    from app.services.storage import SessionStorage
    from app.models.frame_data import FrameData
    s = Session('replace-export', SessionStorage('replace-export', root=tmp_path/'action'))
    frames = [FrameData(index=i, timestamp=i/12, image=rgba()) for i in range(4)]
    s.frame_store.save_raw_frames(frames)
    s.frame_manager.add_frames(frames)
    for i in range(4):
        s.save_processed(i, rgba(color=(30,80,160)))
    s.persist_metadata()
    return s


def export(s, ctx, indices, fmt='frames'):
    from app.api.export_api import run_export
    from app.models.export_config import ExportConfig
    return run_export(s, ExportConfig(format=fmt), indices, 'walk', ctx)


def digest(directory):
    return {f.relative_to(directory).as_posix():hashlib.sha256(f.read_bytes()).hexdigest()
            for f in directory.rglob('*') if f.is_file()}


@pytest.mark.parametrize('indices', [[0,1], [1,3]])
def test_reexport_only_contains_current_frames(prepared, ctx, indices):
    export(prepared, ctx, [0,1,2,3])
    output = prepared.storage.exports_dir/'walk'
    (output/'old-preview.gif').write_bytes(b'old')
    (output/'old-subdir').mkdir()
    (output/'old-subdir'/'old.png').write_bytes(b'old')
    originals = digest(prepared.storage.raw_dir)
    processed = digest(prepared.storage.proc_dir)
    metadata = prepared.storage.frames_json.read_bytes()
    result = export(prepared, ctx, indices)
    expected = [f'walk_{i:04d}.png' for i in indices]
    assert sorted(p.name for p in output.iterdir()) == expected
    assert result['files'] == expected and result['dir'] == str(output)
    assert result['main'] == 'walk'
    assert digest(prepared.storage.raw_dir) == originals
    assert digest(prepared.storage.proc_dir) == processed
    assert prepared.storage.frames_json.read_bytes() == metadata
    assert not list(prepared.storage.root.glob('.export-*'))
    assert json.loads((prepared.storage.root/'recipe.json').read_text(encoding='utf-8'))['steps'][-1]['result']['files'] == expected


@pytest.mark.parametrize('fmt, files', [
    ('gif', ['walk.gif']), ('sprite_sheet', ['walk.json','walk.png']),
    ('webp', ['walk.webp']),
    ('godot', ['walk.tres','walk_frame_0000.png','walk_frame_0001.png']),
])
def test_switch_format_replaces_entire_output(prepared, ctx, fmt, files):
    export(prepared, ctx, [0,1,2,3])
    result = export(prepared, ctx, [0,1], fmt)
    assert result['files'] == files
    assert (prepared.storage.exports_dir/'walk'/result['main']).is_file()
    assert '.export-work-' not in (result['info'] or '')
    assert sorted(p.name for p in (prepared.storage.exports_dir/'walk').iterdir()) == files


@pytest.mark.parametrize('failure', ['exception', 'empty', 'cancel'])
def test_failed_reexport_keeps_previous_and_never_exposes_partial(prepared, ctx, monkeypatch, failure):
    from app.api import export_api
    export(prepared, ctx, [0,1,2,3])
    before = digest(prepared.storage.exports_dir)
    recipe = (prepared.storage.root/'recipe.json').read_bytes()
    def broken(self, frames, cfg):
        assert digest(prepared.storage.exports_dir) == before
        assert [e['name'] for e in prepared.storage.list_exports()] == ['walk']
        if failure != 'empty':
            (cfg.output_path/'walk_0000.png').write_bytes(b'incomplete')
        if failure == 'exception': raise RuntimeError('write failed')
        if failure == 'cancel': ctx.cancel()
        return str(cfg.output_path), None
    monkeypatch.setattr(export_api.Exporter, 'export', broken)
    with pytest.raises((RuntimeError, ValueError)):
        export(prepared, ctx, [0,1])
    assert digest(prepared.storage.exports_dir) == before
    assert (prepared.storage.root/'recipe.json').read_bytes() == recipe
    assert not list(prepared.storage.root.glob('.export-*'))
    assert all(f.image is None and f.processed_image is None for f in prepared.frame_manager.frames)


def test_first_export_failure_does_not_publish_empty_directory(prepared, ctx, monkeypatch):
    from app.api import export_api
    def broken(self, frames, cfg):
        (cfg.output_path/'partial.png').write_bytes(b'partial')
        raise OSError('disk full')
    monkeypatch.setattr(export_api.Exporter, 'export', broken)
    with pytest.raises(OSError): export(prepared, ctx, [0])
    assert prepared.storage.list_exports() == []
    assert not list(prepared.storage.exports_dir.iterdir())


def test_publish_failure_restores_previous(prepared, ctx, monkeypatch):
    export(prepared, ctx, [0,1,2,3])
    before = digest(prepared.storage.exports_dir)
    original = Path.replace
    def fail_staged(self, target):
        if self.name == 'walk' and self.parent.name.startswith('.export-work-'):
            raise OSError('replace failed')
        return original(self, target)
    monkeypatch.setattr(Path, 'replace', fail_staged)
    with pytest.raises(OSError, match='replace failed'): export(prepared, ctx, [0])
    assert digest(prepared.storage.exports_dir) == before
    assert not list(prepared.storage.root.glob('.export-*'))


def test_retry_replaces_incomplete_legacy_directory(prepared, ctx):
    output = prepared.storage.export_dir('walk')
    (output/'.incomplete').touch()
    (output/'old_9999.png').write_bytes(b'partial')
    export(prepared, ctx, [0])
    assert sorted(p.name for p in output.iterdir()) == ['walk_0000.png']
    assert len(prepared.storage.list_exports()) == 1


def test_failed_publish_and_rollback_preserves_recovery_backup(prepared, ctx, monkeypatch):
    export(prepared, ctx, [0,1,2,3])
    before = digest(prepared.storage.exports_dir/'walk')
    original = Path.replace
    def fail_after_backup(self, target):
        if self.parent.name.startswith('.export-work-') or self.name.startswith('.export-previous-'):
            raise OSError('publication and rollback failed')
        return original(self, target)
    monkeypatch.setattr(Path, 'replace', fail_after_backup)
    with pytest.raises(OSError): export(prepared, ctx, [0])
    backups = list(prepared.storage.root.glob('.export-previous-*'))
    assert len(backups) == 1 and digest(backups[0]) == before
    assert prepared.storage.list_exports() == []
