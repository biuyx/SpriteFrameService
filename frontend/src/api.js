// 后端 API 封装
const BASE = ''

// 401 回调：由 App 注册，用于把界面切回登录页
let onUnauthorized = null
export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn
}

async function request(method, path, { json, form, blob } = {}) {
  // credentials 让登录下发的 HttpOnly Cookie 随请求发出
  const opts = { method, headers: {}, credentials: 'same-origin' }
  if (json !== undefined) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(json)
  } else if (form !== undefined) {
    opts.body = form
  }
  const res = await fetch(BASE + path, opts)
  if (!res.ok) {
    let detail = res.statusText
    try {
      const data = await res.json()
      detail = data.detail || JSON.stringify(data)
    } catch { /* ignore */ }
    // 登录接口自身的 401（令牌输错）由登录页就地提示，
    // 不触发「登录已过期」的全局回调，否则会重复报错
    const isAuthEndpoint = path.startsWith('/api/auth/')
    if (res.status === 401 && onUnauthorized && !isAuthEndpoint) onUnauthorized(detail)
    throw new Error(detail)
  }
  if (blob) return res.blob()
  const ct = res.headers.get('content-type') || ''
  if (ct.includes('application/json')) return res.json()
  return res
}

const api = {
  // 认证
  authStatus: () => request('GET', '/api/auth/status'),
  login: (token) => request('POST', '/api/auth/login', { json: { token } }),
  logout: () => request('POST', '/api/auth/logout'),

  // 精灵 / 动作
  sprites: () => request('GET', '/api/sprites'),
  createSprite: (name, tags) => request('POST', '/api/sprites', { json: { name, tags } }),
  sprite: (sid) => request('GET', `/api/sprites/${sid}`),
  patchSprite: (sid, patch) => request('PATCH', `/api/sprites/${sid}`, { json: patch }),
  deleteSprite: (sid) => request('DELETE', `/api/sprites/${sid}`),
  actions: (sid) => request('GET', `/api/sprites/${sid}/actions`),
  createAction: (sid, name, firstFrame) =>
    request('POST', `/api/sprites/${sid}/actions`, { json: { name, first_frame: firstFrame } }),
  patchAction: (sid, aid, patch) => request('PATCH', `/api/sprites/${sid}/actions/${aid}`, { json: patch }),
  deleteAction: (sid, aid) => request('DELETE', `/api/sprites/${sid}/actions/${aid}`),
  openAction: (sid, aid) => request('POST', `/api/sprites/${sid}/actions/${aid}/open`),
  actionCover: (sid, aid, v = 0) => `${BASE}/api/sprites/${sid}/actions/${aid}/cover?v=${v}`,
  legacySessions: () => request('GET', '/api/sprites/legacy-sessions'),
  claimSession: (sid, sessionId, name) =>
    request('POST', `/api/sprites/${sid}/claim`, { json: { session_id: sessionId, name } }),

  // 能力
  capabilities: () => request('GET', '/api/capabilities'),
  health: () => request('GET', '/api/health'),

  // 会话（= 动作工作态；id 即 action_id）
  session: (sid) => request('GET', `/api/sessions/${sid}`),
  deleteSession: (sid) => request('DELETE', `/api/sessions/${sid}`),

  // 视频
  uploadVideo: (sid, file) => {
    const form = new FormData()
    form.append('file', file)
    return request('POST', `/api/sessions/${sid}/video`, { form })
  },
  videoInfo: (sid) => request('GET', `/api/sessions/${sid}/video/info`),

  // 帧
  extract: (sid, params) => request('POST', `/api/sessions/${sid}/frames/extract`, { json: params }),
  frames: (sid) => request('GET', `/api/sessions/${sid}/frames`),
  selection: (sid, params) => request('POST', `/api/sessions/${sid}/frames/selection`, { json: params }),
  deleteFrame: (sid, index) => request('DELETE', `/api/sessions/${sid}/frames/${index}`),
  deleteFrames: (sid, indices) => request('POST', `/api/sessions/${sid}/frames/delete`, { json: indices }),
  reorder: (sid, newOrder) => request('POST', `/api/sessions/${sid}/frames/reorder`, { json: { new_order: newOrder } }),
  frameImage: (sid, index, { type = 'preview', checker = 1, fit = 0, v = 0 } = {}) =>
    `${BASE}/api/sessions/${sid}/frames/${index}/image?type=${type}&checker=${checker}&fit=${fit}&v=${v}`,
  loopTransition: (sid, params) => request('POST', `/api/sessions/${sid}/frames/loop-transition`, { json: params }),
  supplement: (sid, params) => request('POST', `/api/sessions/${sid}/frames/supplement`, { json: params }),

  // 分析
  detect: (sid, params) => request('POST', `/api/sessions/${sid}/analysis/detect`, { json: params }),
  analysis: (sid, index) => request('GET', `/api/sessions/${sid}/analysis/${index}`),
  overlay: (sid, index, mode, fit = 0) =>
    `${BASE}/api/sessions/${sid}/analysis/${index}/overlay?mode=${mode}&fit=${fit}`,
  removeSimilar: (sid, params) => request('POST', `/api/sessions/${sid}/analysis/remove-similar`, { json: params }),
  findLoop: (sid, params) => request('POST', `/api/sessions/${sid}/analysis/find-loop`, { json: params }),

  // 背景
  bgModels: (sid) => request('GET', `/api/sessions/${sid}/background/models`),
  bgRemove: (sid, params) => request('POST', `/api/sessions/${sid}/background/remove`, { json: params }),
  bgTest: (sid, params) => request('POST', `/api/sessions/${sid}/background/test`, { json: params, blob: true }),
  outline: (sid, params) => request('POST', `/api/sessions/${sid}/background/outline`, { json: params }),

  // 图像处理
  scale: (sid, params) => request('POST', `/api/sessions/${sid}/image/scale`, { json: params }),
  crop: (sid, params) => request('POST', `/api/sessions/${sid}/image/crop-whitespace`, { json: params }),
  optimizeEdges: (sid, params) => request('POST', `/api/sessions/${sid}/image/optimize-edges`, { json: params }),
  enhance: (sid, params) => request('POST', `/api/sessions/${sid}/image/enhance`, { json: params }),
  wandSelect: (sid, params) => request('POST', `/api/sessions/${sid}/image/wand/select`, { json: params }),
  wandMask: (sid, frameIndex) => `${BASE}/api/sessions/${sid}/image/wand/mask?frame_index=${frameIndex}`,
  wandApply: (sid, params) => request('POST', `/api/sessions/${sid}/image/wand/apply`, { json: params }),

  // 导出
  export: (sid, params) => request('POST', `/api/sessions/${sid}/export`, { json: params }),
  exports: (sid) => request('GET', `/api/sessions/${sid}/export/list`),
  exportDownload: (sid, name) => `${BASE}/api/sessions/${sid}/export/${name}/download`,

  // 历史 / 工序
  history: (sid) => request('GET', `/api/sessions/${sid}/history`),
  revert: (sid, stepId) => request('POST', `/api/sessions/${sid}/history/revert`, { json: { step_id: stepId } }),
  recipe: (sid) => request('GET', `/api/sessions/${sid}/recipe`),

  // 任务
  job: (id) => request('GET', `/api/jobs/${id}`),
  cancelJob: (id) => request('POST', `/api/jobs/${id}/cancel`),
}

export default api
