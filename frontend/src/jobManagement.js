export const JOB_TYPES = {
  background: '抠图', extract: '抽帧', export: '导出', spine_export: 'Spine 导出',
  pipeline: '流水线', generate: '视频生成', gen_first_frame: '首帧生成',
  reconcile: '取回生成结果', processing_batch: '批量加工', generation_batch: '批量生成',
  copy_sprite: '复制角色', batch_import: '批量导入',
}
export const isActiveJob = (job) => ['queued', 'running'].includes(job.status)
export const canCancelJob = (job) => isActiveJob(job) && !job.cancel_requested
export function jobStatus(job) {
  if (isActiveJob(job) && job.cancel_requested) return '正在停止'
  return { queued: '排队中', running: '进行中', done: '已完成', error: '失败', cancelled: '已取消' }[job.status] || job.status
}
export function filterJobs(jobs, status, keyword, type = '') {
  const q = keyword.trim().toLowerCase()
  return jobs.filter((j) => {
    if (status === 'active' ? !isActiveJob(j) : status !== 'all' && j.status !== status) return false
    if (type && j.type !== type) return false
    const c = j.context || {}
    return !q || [j.id, j.type, JOB_TYPES[j.type], c.sprite_name, c.action_name, c.action_id].join(' ').toLowerCase().includes(q)
  })
}
