import { reactive } from 'vue'
import api from './api'

const state = reactive({
  // 三层视图: 'library' 精灵库 | 'sprite' 动作看板 | 'workbench' 动作工作台
  view: 'library',
  currentSprite: null,    // {id, name, preset}
  currentAction: null,    // {id, name, status, ...}
  spriteActions: [],      // 当前精灵的动作列表（含 summary）：工作台动作切换器/流水线状态用
  lastActionId: null,     // 从工作台返回看板时定位高亮的动作
  libraryVersion: 0,      // 全局资源变动（项目包导入等）后递增，精灵库据此重载

  sessionId: null,        // = 打开中的 action_id
  videoInfo: null,
  videoVersion: 0,        // 视频预览缓存戳：切版本/生成/上传后递增让 <video> 重新加载
  firstFrame: { available: false, version: 0 },   // 首帧参考图就绪状态（首帧/视频生成两步共用）
  takes: { current: null, takes: [] },            // 素材版本（视频生成列表 / 抽帧规则归属共用）
  frames: [],
  frameCount: 0,
  selectedCount: 0,
  capabilities: null,
  activeJob: null,        // 当前关注的 job
  toast: '',
  toastTimer: null,
  // 帧图像版本号：每次刷新帧列表递增，用于让缩略图/预览绕过浏览器缓存加载最新效果
  frameVersion: 0,
  // 共享的循环过渡设置（预览与导出共用）
  loopTransition: { enabled: false, count: 5, mode: 'blend' },
})

export function useStore() {
  return state
}

export function toast(msg) {
  state.toast = msg
  clearTimeout(state.toastTimer)
  state.toastTimer = setTimeout(() => (state.toast = ''), 3500)
}

// ---- 应用内确认对话框（替代原生 confirm/prompt：原生对话框可能被浏览器
//      静默抑制——被抑制的 confirm 直接返回 false，表现为“点了没反应”）----
export const confirmDialog = reactive({
  visible: false,
  message: '',
  danger: false,
  input: null,          // {placeholder, value} 时显示输入框（替代 prompt）
  _resolve: null,
})

/**
 * askConfirm('确定删除？') → Promise<boolean>
 * askConfirm('命名：', {input:{placeholder:'动作名'}}) → Promise<string|null>
 */
export function askConfirm(message, opts = {}) {
  return new Promise((resolve) => {
    confirmDialog.message = message
    confirmDialog.danger = !!opts.danger
    confirmDialog.input = opts.input ? { placeholder: opts.input.placeholder || '', value: opts.input.initial || '' } : null
    confirmDialog.visible = true
    confirmDialog._resolve = resolve
  })
}

export function resolveConfirm(okOrValue) {
  const r = confirmDialog._resolve
  confirmDialog.visible = false
  confirmDialog._resolve = null
  if (r) r(okOrValue)
}

// 打开动作：进入工作台（sessionId 即 action_id）
export async function openAction(spriteId, actionId) {
  const r = await api.openAction(spriteId, actionId)
  state.currentSprite = r.sprite
  state.currentAction = r.action
  state.sessionId = r.action.id
  state.videoInfo = r.session.video_info
  state.view = 'workbench'
  await refreshFrames()
  loadSpriteActions(spriteId)   // 切换器与流水线状态用，不阻塞打开
  probeFirstFrame()             // 流水线「首帧」状态
  loadTakes()
  return r
}

// 首帧参考图是否就绪（探测一次 first-frame 端点）
export async function probeFirstFrame() {
  if (!state.sessionId) return
  try {
    const r = await fetch(api.firstFrameUrl(state.sessionId, Date.now()), { credentials: 'same-origin' })
    state.firstFrame.available = r.ok
  } catch { state.firstFrame.available = false }
}

export function markFirstFrame() {
  state.firstFrame.available = true
  state.firstFrame.version = Date.now()
}

export async function loadTakes() {
  if (!state.sessionId) return
  try { state.takes = await api.takes(state.sessionId) } catch { /* ignore */ }
}

// 当前精灵的动作列表（含 summary）；同精灵重复调用只刷新数据
export async function loadSpriteActions(spriteId) {
  try {
    const r = await api.actions(spriteId)
    if (state.currentSprite?.id === spriteId) state.spriteActions = r.actions
  } catch { /* 列表不可用不影响工作台 */ }
}

// 返回上一层
export function gotoLibrary() {
  state.view = 'library'
  state.currentSprite = null
  state.currentAction = null
  state.sessionId = null
  state.spriteActions = []
  resetProject()
}

export function gotoSprite(sprite) {
  if (sprite) state.currentSprite = sprite
  state.lastActionId = state.currentAction?.id || null   // 看板定位刚离开的动作
  state.view = 'sprite'
  state.currentAction = null
  state.sessionId = null
  resetProject()
}

export async function refreshSession() {
  if (!state.sessionId) return
  try {
    const s = await api.session(state.sessionId)
    state.videoInfo = s.video_info
  } catch { /* ignore */ }
}

export async function refreshFrames() {
  if (!state.sessionId) return
  const data = await api.frames(state.sessionId)
  state.frames = data.frames
  state.frameCount = data.frame_count
  state.selectedCount = data.selected_count
  state.frameVersion += 1   // 使缩略图/预览重新加载最新效果（绕过浏览器缓存）
}

export async function loadCapabilities() {
  state.capabilities = await api.capabilities()
  return state.capabilities
}

export function selectedIndices() {
  return state.frames.filter((f) => f.is_selected).map((f) => f.index)
}

export function resetProject() {
  state.videoInfo = null
  state.frames = []
  state.frameCount = 0
  state.selectedCount = 0
  state.firstFrame = { available: false, version: 0 }
  state.takes = { current: null, takes: [] }
}
