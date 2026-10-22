"""内存耗尽回归：逐帧处理、磁盘快照、短生命周期解码器、抠图真实内存准入。"""
import hashlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


@pytest.fixture
def disk_frames(tmp_path, rgba):
    from app.services.session import Session
    from app.services.storage import SessionStorage
    from app.models.frame_data import FrameData
    s=Session('oom-test',SessionStorage('oom-test',root=tmp_path/'action'))
    frames=[FrameData(index=i,timestamp=i/12,image=rgba(color=(30+i,50,80)))for i in range(6)]
    s.frame_store.save_raw_frames(frames);s.frame_manager.add_frames(frames);s.persist_metadata()
    return s


def test_matting_streams_one_frame_and_keeps_disk_undo(disk_frames,ctx,monkeypatch):
    from app.api import background
    s=disk_frames;seen=[];removers=[]
    original={f.id:hashlib.sha256(s.frame_store.raw_path(f.id).read_bytes()).hexdigest()for f in s.frame_manager.frames}
    monkeypatch.setattr(s,'load_display_arrays',lambda *args:pytest.fail('must not load whole batch'))
    def remove(self,img,**kwargs):
        removers.append(self)
        self._rembg_session = object()  # 模拟取消回调持有 remover 时的真实模型引用。
        assert sum(f.image is not None or f.processed_image is not None for f in s.frame_manager.frames)==1
        seen.append(img.shape)
        return np.full_like(img,255)
    monkeypatch.setattr(background.BackgroundRemover,'remove_background',remove)
    result=background.run_bg_remove(s,list(range(6)),'ai',{},ctx)
    assert result['processed']==6 and len(seen)==6
    entry=s.history.get_entries()[0]
    assert len(entry.snapshot_paths)==6 and not any(entry.snapshot_states.values())
    assert all(hashlib.sha256(entry.snapshot_paths[i].read_bytes()).hexdigest()==original[f.id]for i,f in enumerate(s.frame_manager.frames))
    assert all(f.image is None and f.processed_image is None for f in s.frame_manager.frames)
    assert all(r._rembg_session is None for r in removers)
    restored=s.history.revert_to(0,s.frame_manager);s.flush_modified_frames(restored)
    assert set(restored)==set(range(6))
    assert all(not f.has_processed for f in s.frame_manager.frames)
    assert all(hashlib.sha256(s.frame_store.raw_path(f.id).read_bytes()).hexdigest()==original[f.id]for f in s.frame_manager.frames)


@pytest.mark.parametrize('stop',['cancel','exception'])
def test_partial_matting_keeps_finished_frames_and_releases_arrays(disk_frames,ctx,monkeypatch,stop):
    from app.api import background
    s=disk_frames;calls=[]
    def remove(self,img,**kwargs):
        calls.append(1)
        if len(calls)==2 and stop=='exception':raise MemoryError('simulated allocation failure')
        if stop=='cancel':ctx.cancel()
        return np.full_like(img,255)
    monkeypatch.setattr(background.BackgroundRemover,'remove_background',remove)
    if stop=='exception':
        with pytest.raises(MemoryError):background.run_bg_remove(s,list(range(6)),'ai',{},ctx)
    else:background.run_bg_remove(s,list(range(6)),'ai',{},ctx)
    assert sum(f.has_processed for f in s.frame_manager.frames)==1
    assert all(f.image is None and f.processed_image is None for f in s.frame_manager.frames)


@pytest.mark.parametrize('force_cpu,fail_cuda',[(True,False),(False,False),(False,True)])
def test_onnx_sessions_disable_retained_workspaces_and_bound_threads(monkeypatch,force_cpu,fail_cuda):
    import onnxruntime as ort
    from app.config import get_settings
    from app.core.background_remover import U2NetLocalSession
    calls=[]
    monkeypatch.setattr(get_settings(),'matting_threads',3)
    monkeypatch.setattr(ort,'get_available_providers',lambda:['CUDAExecutionProvider','CPUExecutionProvider'])
    def create(path,*,sess_options,providers):
        calls.append(list(providers))
        assert sess_options.enable_cpu_mem_arena is False
        assert sess_options.enable_mem_pattern is False
        assert sess_options.intra_op_num_threads==3 and sess_options.inter_op_num_threads==1
        if fail_cuda and 'CUDAExecutionProvider'in providers:raise RuntimeError('unavailable GPU')
        return SimpleNamespace(get_providers=lambda:providers)
    monkeypatch.setattr(ort,'InferenceSession',create)
    model=U2NetLocalSession('fake.onnx',force_cpu=force_cpu)
    assert model.device_type==('CPU'if force_cpu or fail_cuda else 'GPU (CUDA)')
    assert len(calls)==(2 if fail_cuda else 1)


@pytest.fixture
def capture(monkeypatch):
    from app.core import video_processor as module
    state={'active':0,'peak':0,'released':0,'fail':False}
    class Capture:
        def __init__(self,*args):
            self.open=True;state['active']+=1;state['peak']=max(state['peak'],state['active'])
        def isOpened(self):return not state['fail']
        def get(self,k):
            return {module.cv2.CAP_PROP_FPS:12,module.cv2.CAP_PROP_FRAME_COUNT:24,
                    module.cv2.CAP_PROP_FRAME_WIDTH:32,module.cv2.CAP_PROP_FRAME_HEIGHT:32,
                    module.cv2.CAP_PROP_FOURCC:0}[k]
        def release(self):
            if self.open:state['active']-=1;state['released']+=1;self.open=False
    monkeypatch.setattr(module.cv2,'VideoCapture',Capture)
    return state


def test_many_restored_actions_do_not_retain_decoders(tmp_path,capture,monkeypatch):
    from app.services.session import SessionManager
    manager=SessionManager()
    for i in range(24):
        root=tmp_path/str(i);(root/'video').mkdir(parents=True);(root/'video/upload.mp4').touch()
        monkeypatch.setattr(manager,'_locate_root',lambda _,root=root:root)
        session=manager.get(str(i))
        assert session.video_info.frame_count==24 and session._video_processor is None
        assert capture['active']==0
    assert capture['peak']==1 and capture['released']==24


def test_invalid_video_metadata_closes_decoder(tmp_path,capture):
    from app.core.video_processor import VideoProcessor
    p=tmp_path/'bad.mp4';p.touch();capture['fail']=True
    with pytest.raises(IOError):VideoProcessor.read_metadata(str(p))
    assert capture['active']==0 and capture['released']==1


def test_low_memory_matting_waits_without_consuming_permit(monkeypatch):
    import app.services.resources as module
    from app.services.concurrency import ConcurrencyGate
    from app.core.background_remover import BackgroundRemover
    free={'mb':100};evicted=[]
    monkeypatch.setattr(module,'available_memory_mb',lambda:free['mb'])
    monkeypatch.setattr(BackgroundRemover,'release_idle_cache',lambda:evicted.append(1))
    coord=module.ResourceCoordinator(heavy_gate=ConcurrencyGate(lambda:1,'test'),budget=lambda:2000)
    assert '实际可用内存'in coord.try_acquire('matting',['a'],1000)
    assert evicted==[1] and coord.status()['reserved_memory_mb']==0 and coord.heavy_gate.active==0
    free['mb']=1800
    assert coord.try_acquire('matting',['a'],1000)is None
    coord.release('matting',['a'],1000)
