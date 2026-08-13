import { reactive } from 'vue'
import api from './api'

const state = reactive({
  // 三层视图: 'library' 精灵库 | 'sprite' 动作看板 | 'workbench' 动作工作台
  view: 'library',
  currentSprite: null,    // {id, name, preset}
  currentAction: null,    // {id, name, status, ...}

  sessionId: null,        // = 打开中的 action_id
  videoInfo: null,
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
  return r
}

// 返回上一层
export function gotoLibrary() {
  state.view = 'library'
  state.currentSprite = null
  state.currentAction = null
  state.sessionId = null
  resetProject()
}

export function gotoSprite(sprite) {
  if (sprite) state.currentSprite = sprite
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
}
