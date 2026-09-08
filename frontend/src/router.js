// 轻量 hash 路由：把三层视图状态映射到 URL，支持刷新回位、前进/后退、链接分享。
//   #/sprites                                  精灵库
//   #/sprites/:id                              动作看板
//   #/sprites/:id/actions/:aid[/:tab]          动作工作台（tab 缺省 video）
// 状态 → hash 由 watch 同步；hash → 状态由 hashchange 应用；两边都做幂等比较避免回环。
import { watch } from 'vue'
import api from './api'
import { useStore, gotoLibrary, gotoSprite, openAction, toast } from './stores'
import { currentTab } from './nav'

const TABS = new Set(['video', 'analysis', 'background', 'image', 'editor', 'export', 'history'])

export function parseHash(hash = location.hash) {
  const parts = hash.replace(/^#\/?/, '').split('/').filter(Boolean)
  if (parts[0] !== 'sprites' || !parts[1]) return { view: 'library' }
  if (parts[2] !== 'actions' || !parts[3]) return { view: 'sprite', spriteId: parts[1] }
  return { view: 'workbench', spriteId: parts[1], actionId: parts[3],
           tab: TABS.has(parts[4]) ? parts[4] : 'video' }
}

export function hashFor(state) {
  if (state.view === 'workbench' && state.spriteId && state.actionId)
    return `#/sprites/${state.spriteId}/actions/${state.actionId}/${state.tab || 'video'}`
  if (state.view === 'sprite' && state.spriteId) return `#/sprites/${state.spriteId}`
  return '#/sprites'
}

function currentState() {
  const store = useStore()
  return { view: store.view, spriteId: store.currentSprite?.id,
           actionId: store.currentAction?.id, tab: currentTab.value }
}

let applying = false

export async function applyHash() {
  const store = useStore()
  const target = parseHash()
  const cur = currentState()
  if (hashFor(target) === hashFor(cur)) return          // 已在目标位置
  applying = true
  try {
    if (target.view === 'library') {
      gotoLibrary()
    } else if (target.view === 'sprite') {
      if (store.currentSprite?.id !== target.spriteId) {
        gotoSprite(await api.sprite(target.spriteId))
      } else {
        gotoSprite(store.currentSprite)
      }
    } else {
      if (store.currentAction?.id !== target.actionId) {
        await openAction(target.spriteId, target.actionId)
      }
      currentTab.value = target.tab
    }
  } catch (e) {
    toast(`无法打开该地址: ${e.message}`)
    gotoLibrary()
  } finally {
    applying = false
    syncHash(true)
  }
}

function syncHash(replace = false) {
  const h = hashFor(currentState())
  if (location.hash === h) return
  if (replace) history.replaceState(null, '', h)
  else location.hash = h
}

let installed = false
export function installRouter() {
  if (installed) return
  installed = true
  const store = useStore()
  // 状态 → hash（应用 hash 期间不回写，避免中间态入历史）
  watch(() => [store.view, store.currentSprite?.id, store.currentAction?.id, currentTab.value],
        () => { if (!applying) syncHash() })
  window.addEventListener('hashchange', () => { if (!applying) applyHash() })
  // 首屏：按地址回位（无地址则写入精灵库）
  return applyHash().then(() => syncHash(true))
}
