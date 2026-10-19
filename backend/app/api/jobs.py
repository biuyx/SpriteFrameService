"""后台任务 API：查询状态、取消。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.job_manager import job_manager

router = APIRouter(prefix="/jobs", tags=["jobs"])


class MaintenanceRequest(BaseModel):
    enabled: bool


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
