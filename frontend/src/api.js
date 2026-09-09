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
    const err = new Error(detail)
    err.status = res.status   // 让调用方能区分 404（资源没了）与网络瞬断
    throw err
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
  actionFirstFrameUrl: (sid, aid, v = 0) => `${BASE}/api/sprites/${sid}/actions/${aid}/first-frame?v=${v}`,
  actionFirstFrameAsRef: (sid, aid, role) =>
    request('POST', `/api/sprites/${sid}/actions/${aid}/first-frame/as-ref`, { json: { role } }),
  // 精灵首帧参考图库（一张图可用于多个动作）
  spriteRefs: (sid) => request('GET', `/api/sprites/${sid}/refs`),
  uploadSpriteRef: (sid, file) => {
    const form = new FormData()
    form.append('file', file)
    return request('POST', `/api/sprites/${sid}/refs`, { form })
  },
  spriteRefImageUrl: (sid, rid, v = 0) => `${BASE}/api/sprites/${sid}/refs/${rid}/image?v=${v}`,
  patchSpriteRef: (sid, rid, patch) => request('PATCH', `/api/sprites/${sid}/refs/${rid}`, { json: patch }),
  deleteSpriteRef: (sid, rid) => request('DELETE', `/api/sprites/${sid}/refs/${rid}`),

  // 参考首帧集（完成角色的全套动作首帧，供新角色首帧生图参考）
  ffsets: () => request('GET', '/api/ffsets'),
  ffsetImportDir: (dir, name, group) =>
    request('POST', '/api/ffsets/import-dir', { json: { dir, name, group } }),
  ffsetArchiveSprite: (spriteId, name, group) =>
    request('POST', '/api/ffsets/archive-sprite', { json: { sprite_id: spriteId, name, group } }),
  ffsetFrameUrl: (setId, key) => `${BASE}/api/ffsets/${setId}/frames/${encodeURIComponent(key)}/image`,
  deleteFfset: (setId) => request('DELETE', `/api/ffsets/${setId}`),
  genFirstFrame: (sid, aid, payload) =>
    request('POST', `/api/sprites/${sid}/actions/${aid}/gen-first-frame`, { json: payload }),
  batchGenFirstFrames: (sid, payload) =>
    request('POST', `/api/sprites/${sid}/batch-gen-first-frames`, { json: payload }),
  applySpriteRef: (sid, rid, actionIds) =>
    request('POST', `/api/sprites/${sid}/refs/${rid}/apply`, { json: { action_ids: actionIds } }),
  legacySessions: () => request('GET', '/api/sprites/legacy-sessions'),
  batchScan: (payload) => request('POST', '/api/sprites/batch-scan', { json: payload }),
  batchImport: (payload) => request('POST', '/api/sprites/batch-import', { json: payload }),
  batchGenerate: (sid, payload) =>
    request('POST', `/api/sprites/${sid}/batch-generate`, { json: payload }),
  pipelinePlan: (sid, payload) => request('POST', `/api/sprites/${sid}/pipeline/plan`, { json: payload }),
  pipelineStart: (sid, payload) => request('POST', `/api/sprites/${sid}/pipeline/start`, { json: payload }),
  pipelineResume: (sid, actionIds) =>
    request('POST', `/api/sprites/${sid}/pipeline/resume`, { json: { action_ids: actionIds } }),
  batchExtract: (sid, actionIds) =>
    request('POST', `/api/sprites/${sid}/batch-extract`, { json: { action_ids: actionIds } }),
  templates: () => request('GET', '/api/templates'),
  templateVideoUrl: (tid) => `${BASE}/api/templates/${tid}/video`,
  uploadTemplate: (file, group = '') => {
    const form = new FormData()
    form.append('file', file)
    form.append('group', group)
    return request('POST', '/api/templates/upload', { form })
  },
  patchTemplate: (tid, patch) => request('PATCH', `/api/templates/${tid}`, { json: patch }),
  templateOssUpload: (tid) => request('POST', `/api/templates/${tid}/oss`),
  scanTemplates: (dir, group = '') => request('POST', '/api/templates/scan', { json: { dir, group } }),
  deleteTemplate: (tid) => request('DELETE', `/api/templates/${tid}`),
  claimSession: (sid, sessionId, name) =>
    request('POST', `/api/sprites/${sid}/claim`, { json: { session_id: sessionId, name } }),

  // 提示词库（多版本 / 绑定匹配动作 / 解析）
  prompts: (scope) => request('GET', `/api/prompts${scope ? `?scope=${scope}` : ''}`),
  createPrompt: (payload) => request('POST', '/api/prompts', { json: payload }),
  patchPrompt: (pid, patch) => request('PATCH', `/api/prompts/${pid}`, { json: patch }),
  addPromptVersion: (pid, text, note) =>
    request('POST', `/api/prompts/${pid}/versions`, { json: { text, note } }),
  setPromptCurrent: (pid, v) => request('POST', `/api/prompts/${pid}/current`, { json: { v } }),
  setPromptBindings: (pid, bindings) =>
    request('PUT', `/api/prompts/${pid}/bindings`, { json: { bindings } }),
  deletePrompt: (pid) => request('DELETE', `/api/prompts/${pid}`),
  resolvePrompt: (scope, sid, aid) =>
    request('GET', `/api/prompts/resolve?scope=${scope}&sprite_id=${sid}&action_id=${aid}`),
  resolvePrompts: (scope, sid, actionIds) =>
    request('POST', '/api/prompts/resolve-many', { json: { scope, sprite_id: sid, action_ids: actionIds } }),

  // 项目包 导出/导入
  exportProject: (payload) => request('POST', '/api/project/export', { json: payload, blob: true }),
  importProject: (file) => {
    const form = new FormData()
    form.append('file', file)
    return request('POST', '/api/project/import', { form })
  },

  // 能力 / 设置
  capabilities: () => request('GET', '/api/capabilities'),
  health: () => request('GET', '/api/health'),
  settings: () => request('GET', '/api/settings'),
  saveSettings: (patch) => request('PUT', '/api/settings', { json: patch }),

  // 会话（= 动作工作态；id 即 action_id）
  session: (sid) => request('GET', `/api/sessions/${sid}`),
  deleteSession: (sid) => request('DELETE', `/api/sessions/${sid}`),

  // 视频生成 / take 版本
  genCapabilities: () => request('GET', '/api/generate/capabilities'),
  generate: (sid, params) => request('POST', `/api/sessions/${sid}/generate`, { json: params }),
  uploadFirstFrame: (sid, file) => {
    const form = new FormData()
    form.append('file', file)
    return request('POST', `/api/sessions/${sid}/first-frame`, { form })
  },
  firstFrameUrl: (sid, v = 0) => `${BASE}/api/sessions/${sid}/first-frame?v=${v}`,
  uploadReferenceVideo: (sid, file) => {
    const form = new FormData()
    form.append('file', file)
    return request('POST', `/api/sessions/${sid}/reference-video`, { form })
  },
  referenceVideoUrl: (sid, v = 0) => `${BASE}/api/sessions/${sid}/reference-video?v=${v}`,
  deleteReferenceVideo: (sid) => request('DELETE', `/api/sessions/${sid}/reference-video`),
  takes: (sid) => request('GET', `/api/sessions/${sid}/takes`),
  takeVideoUrl: (sid, tid) => `${BASE}/api/sessions/${sid}/takes/${tid}/video`,
  selectTake: (sid, tid) => request('POST', `/api/sessions/${sid}/takes/${tid}/select`),
  deleteTake: (sid, tid) => request('DELETE', `/api/sessions/${sid}/takes/${tid}`),
  reconcileTakes: (sid) => request('POST', `/api/sessions/${sid}/takes/reconcile`),

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
  exportFramesZip: (sid, type) =>
    request('GET', `/api/sessions/${sid}/frames/export-zip?type=${type}`, { blob: true }),
  importFramesZip: (sid, file, mode) => {
    const form = new FormData()
    form.append('file', file)
    form.append('mode', mode)
    return request('POST', `/api/sessions/${sid}/frames/import-zip`, { form })
  },
  saveExtractRule: (sid, templateId) =>
    request('POST', `/api/sessions/${sid}/frames/extract-rule`, { json: { template_id: templateId } }),
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
