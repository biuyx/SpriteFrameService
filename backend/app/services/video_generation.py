"""Video submission identity and shared, short-lived admission serialization."""
from __future__ import annotations
import hashlib
import json
import threading
from types import SimpleNamespace

from app.services.job_manager import JobSubmissionError
from app.services.take_store import TakeStore

# Shared by standalone, batch video and pipeline start/resume. Never held while
# waiting for provider gates, network calls, session locks or job completion.
submission_lock = threading.RLock()


def signature(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def unresolved(root):
    return TakeStore(SimpleNamespace(video_dir=root / 'video')).unresolved_generation()


def require_resolved(root):
    try:
        pending = unresolved(root)
    except ValueError as exc:
        raise JobSubmissionError(str(exc)) from exc
    if pending:
        raise JobSubmissionError('该动作存在尚未确认结果的生成请求，请先在素材版本中核对/取回原远端结果，避免重复付费')


def active_video(manager, action_id):
    return manager.active_job('generate', action_id=action_id)


def active_pipeline(manager, action_id):
    return manager.active_job('pipeline', action_id=action_id)


def submit_video(manager, session, payload, request_id, fn, gate, sprite_id=None):
    """An acknowledged new request ID is an explicit new take; retries reuse it.

    Legacy callers without IDs conservatively reuse the same parameter request,
    including its terminal result. Updated clients give every confirmed new
    operation an ID and keep it through response loss/reload.
    """
    params = {k: v for k, v in payload.items()
              if k not in ('remember', 'request_id')}
    digest = signature(params)
    key = f'video:{session.id}:{request_id or "legacy-" + digest}'
    with submission_lock:
        old = manager.by_request_key(key)
        if old:
            if old.metadata.get('video_signature') != digest:
                raise JobSubmissionError('请求编号已用于其他生成参数，请重新确认本次操作')
            return old
        busy = active_pipeline(manager, session.id) or active_video(manager, session.id)
        if busy:
            state = '正在停止' if busy.cancel_requested else '正在运行或排队'
            raise JobSubmissionError(f'该动作已有任务{state}（{busy.id}），请等待原任务结束，不要重复提交')
        require_resolved(session.storage.root)
        if not request_id and any(t.get('source') == 'generate'
                                  for t in TakeStore(session.storage)._load()['takes']):
            raise JobSubmissionError('该动作已有生成历史，但此请求没有编号；请刷新页面后重新确认，避免重发旧请求重复付费')
        meta = {'action_id': session.id, 'video_signature': digest}
        if sprite_id:
            meta['sprite_id'] = sprite_id
        return manager.submit('generate', fn, pool='io', gate=gate,
                              target=session.id, params=params, request_key=key, metadata=meta)
