"""后台任务 API：查询状态、取消。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Literal

from app.services.job_manager import job_manager

router = APIRouter(prefix="/jobs", tags=["jobs"])


class MaintenanceRequest(BaseModel):
    enabled: bool


class StopAllRequest(BaseModel):
    confirm: Literal[True]


class AdmissionRequest(BaseModel):
    paused: bool


@router.post('/admission')
def admission(req: AdmissionRequest):
    return job_manager.set_admission_paused(req.paused)


@router.get('/control')
def control_state():
    return job_manager.control_state()


@router.post('/stop-all')
def stop_all(req: StopAllRequest):
    return job_manager.stop_all()


@router.get('/manage')
def manage_jobs(limit: int = 500):
    from app.services.sprite_store import sprite_store
    contexts, pipeline_jobs = {}, {}
    for sp in sprite_store.list_sprites():
        for ref in sp.get('actions', []):
            try:
                action = sprite_store.get_action(sp['id'], ref['id'])
            except Exception:
                continue
            context = {'sprite_id': sp['id'], 'sprite_name': sp['name'],
                       'action_id': action['id'], 'action_name': action.get('name', '')}
            contexts[action['id']] = context
            jid = (action.get('pipeline') or {}).get('job_id')
            if jid:
                pipeline_jobs[jid] = context
    rows = job_manager.list(limit=limit)
    for row in rows:
        meta = row.get('metadata') or {}
        aid = meta.get('action_id') or meta.get('session_id')
        if not aid and len(row.get('targets') or []) == 1:
            aid = row['targets'][0]
        row['context'] = contexts.get(aid) or pipeline_jobs.get(row['id']) or {}
    return {'jobs': rows, **job_manager.control_state(), 'limit': max(1, min(limit, 2000))}


@router.post('/maintenance')
def maintenance(req: MaintenanceRequest):
    """部署前先暂停新任务准入；不取消正在进行或排队的任务。"""
    return job_manager.maintenance(req.enabled)


@router.get("")
def list_jobs(limit: int = 50):
    return {"jobs": job_manager.list(limit=limit)}


@router.get("/{job_id}")
def get_job(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        # 任务表在内存中，服务重启即清空。返回终态记录让页面上残留的
        # 轮询自然停止——404 会被轮询方当作网络瞬断而无限重试刷屏。
        return {"id": job_id, "status": "error", "progress": 0, "message": "",
                "result": None,
                "error": "未找到任务记录（可能早于持久化版本），请先检查动作产物再决定是否重试"}
    return job.to_dict()


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str):
    ok = job_manager.cancel(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"cancelled": True, "id": job_id}
