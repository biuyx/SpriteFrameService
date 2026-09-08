<script setup>
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { useStore, gotoLibrary, gotoSprite, openAction, loadCapabilities, toast,
         confirmDialog, resolveConfirm, askConfirm } from './stores'
import { useJobs, cancelJob } from './jobs'
import { currentTab } from './nav'
import { installRouter } from './router'
import api, { setUnauthorizedHandler } from './api'
import LoginView from './components/LoginView.vue'
import SettingsModal from './components/SettingsModal.vue'
import SpriteLibraryView from './views/SpriteLibraryView.vue'
import SpriteDetailView from './views/SpriteDetailView.vue'
import FirstFrameView from './views/FirstFrameView.vue'
import GenerateView from './views/GenerateView.vue'
import ExtractView from './views/ExtractView.vue'
import AnalysisView from './views/AnalysisView.vue'
import BackgroundView from './views/BackgroundView.vue'
import ImageOpsView from './views/ImageOpsView.vue'
import EditorView from './views/EditorView.vue'
import ExportView from './views/ExportView.vue'
import HistoryView from './views/HistoryView.vue'
import JobPanel from './components/JobPanel.vue'
import FrameBrowser from './components/FrameBrowser.vue'
import VideoPanel from './components/VideoPanel.vue'
import ResourceMenu from './components/ResourceMenu.vue'
import TemplateLibraryModal from './components/TemplateLibraryModal.vue'
import FfSetLibraryModal from './components/FfSetLibraryModal.vue'
import PromptLibraryModal from './components/PromptLibraryModal.vue'
import ProjectTransferModal from './components/ProjectTransferModal.vue'

const store = useStore()
const jobs = useJobs()
const tab = currentTab
const ready = ref(false)
const err = ref('')
const jobsVisible = ref(true)   // 后台任务栏是否显示

// 认证状态：needLogin 为真时整个应用被登录页挡住
const authRequired = ref(false)
const needLogin = ref(false)
const loginNotice = ref('')
const settingsOpen = ref(false)
// 全局资源弹窗（顶栏「资源库」菜单打开，任一层都可用）：tpl | ffset | prompt | transfer
const resModal = ref(null)
function onImported() { store.libraryVersion++ }

// 有新任务启动时自动展开任务栏
watch(() => jobs.items.length, (n, old) => {
  if (n > old) jobsVisible.value = true
})

const runningCount = () => jobs.items.filter((j) => j.status === 'running').length

// 工作台左栏：按工序顺序排列的流水线（历史回退移到顶栏工具位）
//   group: main 主线 | refine 精修（可选）
const steps = [
  { key: 'firstframe', label: '首帧', group: 'main' },
  { key: 'generate', label: '视频生成', group: 'main' },
  { key: 'extract', label: '抽帧', group: 'main' },
  { key: 'analysis', label: '动作分析', group: 'main', optional: true },
  { key: 'background', label: '背景抠图', group: 'main' },
  { key: 'image', label: '图像处理', group: 'refine' },
  { key: 'editor', label: '魔棒编辑', group: 'refine' },
  { key: 'export', label: '导出', group: 'main' },
]
const mainOrder = steps.filter((s) => s.group === 'main').map((s) => s.key)
// 素材类工序自带视频预览，右侧常驻预览面板不重复显示
const SOURCE_STEPS = new Set(['firstframe', 'generate', 'extract'])

// 当前动作在看板列表中的摘要（导出次数等不在工作态里）
const curSummary = computed(() =>
  store.spriteActions.find((a) => a.id === store.currentAction?.id)?.summary || {})

// 每步状态：done ✓ / doing ● / todo ○ / optional
function stepState(key) {
  const frames = store.frames
  if (key === 'firstframe') return store.firstFrame.available ? 'done' : 'todo'
  if (key === 'generate') {
    if (store.videoInfo) return 'done'
    return (store.takes.takes || []).some((t) => t.status === 'running' || t.status === 'pending') ? 'doing' : 'todo'
  }
  if (key === 'extract') {
    if (store.frameCount > 0) return 'done'
    return store.videoInfo ? 'doing' : 'todo'
  }
  if (key === 'analysis') {
    return frames.some((f) => f.analysis && Object.values(f.analysis).some(Boolean)) ? 'done' : 'optional'
  }
  if (key === 'background') {
    if (!frames.length) return 'todo'
    const n = frames.filter((f) => f.has_processed).length
    return n === frames.length ? 'done' : n ? 'doing' : 'todo'
  }
  if (key === 'export') return curSummary.value.export_count ? 'done' : 'todo'
  return 'optional'
}
const STATE_ICON = { done: '✓', doing: '●', todo: '○', optional: '·' }

// 「下一步」：主线里当前步之后第一个未完成的步骤
const nextStep = computed(() => {
  const i = mainOrder.indexOf(tab.value)
  const after = i >= 0 ? mainOrder.slice(i + 1) : mainOrder
  const k = after.find((key) => stepState(key) !== 'done') || after[0]
  return k ? steps.find((s) => s.key === k) : null
})

// ---- 动作切换器（同精灵的上一个 / 下一个，保持当前页签） ----
const actionIndex = computed(() =>
  store.spriteActions.findIndex((a) => a.id === store.currentAction?.id))
const switching = ref(false)
async function switchAction(delta) {
  const list = store.spriteActions
  if (!list.length || switching.value) return
  const i = actionIndex.value < 0 ? 0 : actionIndex.value + delta
  if (i < 0 || i >= list.length) return toast(delta > 0 ? '已是最后一个动作' : '已是第一个动作')
  await goAction(list[i].id)
}
async function goAction(actionId) {
  if (!actionId || actionId === store.currentAction?.id) return
  switching.value = true
  try {
    await openAction(store.currentSprite.id, actionId)
  } finally {
    switching.value = false
  }
}
function stageMark(a) {
  const s = a.summary || {}
  if (s.export_count) return '✓'
  if (s.processed_count) return '抠'
  if (s.frame_count) return '帧'
  if (s.generated_count || s.has_video) return '片'
  return s.has_first_frame ? '首' : '–'
}

// ---- 快捷键：[ ] 切换动作，1-6 切换工序，Esc 交给弹窗自身 ----
function onKey(e) {
  if (store.view !== 'workbench') return
  const t = e.target
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return
  if (e.ctrlKey || e.metaKey || e.altKey) return
  if (e.key === '[') { e.preventDefault(); switchAction(-1) }
  else if (e.key === ']') { e.preventDefault(); switchAction(1) }
  else if (/^[1-8]$/.test(e.key)) { tab.value = steps[+e.key - 1].key }
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))

const views = {
  firstframe: FirstFrameView,
  generate: GenerateView,
  extract: ExtractView,
  analysis: AnalysisView,
  background: BackgroundView,
  image: ImageOpsView,
  editor: EditorView,
  export: ExportView,
  history: HistoryView,
}

// 任何请求返回 401（如登录过期）都把界面切回登录页
setUnauthorizedHandler((detail) => {
  needLogin.value = true
  ready.value = false
  loginNotice.value = detail || '登录已过期，请重新登录'
})

async function boot() {
  try {
    const status = await api.authStatus()
    authRequired.value = status.required
    if (status.required && !status.authenticated) {
      needLogin.value = true
      return
    }
    needLogin.value = false
    await loadCapabilities()
    ready.value = true
    await installRouter()      // 按地址回位（刷新/分享链接），并开始同步 hash
  } catch (e) {
    err.value = `无法连接后端服务: ${e.message}`
  }
}

async function onAuthenticated() {
  needLogin.value = false
  loginNotice.value = ''
  err.value = ''
  await boot()
}

// 未捕获的 API 错误兜底：至少给一条 toast，不再静默失败
window.addEventListener('unhandledrejection', (e) => {
  const msg = e.reason?.message || String(e.reason || '未知错误')
  toast(`操作失败: ${msg}`)
})

const confirmInput = ref(null)
watch(() => confirmDialog.visible, async (v) => {
  if (v && confirmDialog.input) {
    await nextTick()
    confirmInput.value?.focus()
  }
})

async function doLogout() {
  if (!(await askConfirm('退出登录？'))) return
  try {
    await api.logout()
  } catch { /* 忽略：无论成功与否都回到登录页 */ }
  ready.value = false
  needLogin.value = true
  loginNotice.value = ''
}

onMounted(boot)

function fmtDuration(sec) {
  if (sec == null) return ''
  const m = Math.floor(sec / 60)
  const s = (sec % 60).toFixed(2)
  return m > 0 ? `${m}m${s.padStart(5, '0')}s` : `${s}s`
}

function backToBoard() {
  gotoSprite(store.currentSprite)
  tab.value = 'firstframe'
}

// 任务面板：跳到任务所属的动作工作台
async function jumpToJob(j) {
  if (!j.spriteId || !j.actionId) return
  if (store.view === 'workbench' && store.currentAction?.id === j.actionId) return
  try {
    await openAction(j.spriteId, j.actionId)
  } catch (e) {
    toast(`无法打开: ${e.message}`)
  }
}
</script>

<template>
  <LoginView v-if="needLogin" :notice="loginNotice" @authenticated="onAuthenticated" />

  <!-- 一层/二层：精灵库 与 动作看板，共用统一顶栏（避免浮动按钮与页头重叠） -->
  <div class="toplevel" v-else-if="ready && (store.view === 'library' || store.view === 'sprite')">
    <div class="topbar">
      <span class="brand-mini">精灵帧工作室</span>
      <nav class="topnav">
        <a :class="{ on: store.view === 'library' }" @click="gotoLibrary">精灵库</a>
        <ResourceMenu @open="resModal = $event" />
      </nav>
      <span class="crumb" v-if="store.view === 'sprite'">
        / <b>{{ store.currentSprite?.name }}</b>
      </span>
      <span class="spacer"></span>
      <button class="small" @click="settingsOpen = true">⚙ 设置</button>
      <button v-if="authRequired" class="small" @click="doLogout">退出登录</button>
    </div>
    <div class="toplevel-body">
      <SpriteLibraryView v-if="store.view === 'library'" />
      <SpriteDetailView v-else />
    </div>
  </div>

  <!-- 三层：动作工作台 -->
  <div class="layout" v-else-if="ready">
    <aside class="sidebar">
      <div class="brand">精灵帧工作室<small>SpriteFrameService</small></div>
      <div class="pipe-title">工序流水线</div>
      <template v-for="(s, i) in steps" :key="s.key">
        <div v-if="s.group === 'refine' && steps[i - 1]?.group !== 'refine'" class="pipe-group">精修（可选）</div>
        <button class="tab step" :class="['st-' + stepState(s.key), { active: tab === s.key }]"
                :title="`快捷键 ${i + 1}`" @click="tab = s.key">
          <span class="step-ico">{{ STATE_ICON[stepState(s.key)] }}</span>
          <span class="step-label">{{ s.label }}</span>
          <span v-if="s.optional && stepState(s.key) !== 'done'" class="step-opt">可选</span>
        </button>
      </template>
      <button v-if="nextStep && nextStep.key !== tab" class="tab next-step" @click="tab = nextStep.key">
        下一步：{{ nextStep.label }} →</button>
      <div class="spacer"></div>
      <button class="tab" :class="{ active: tab === 'history' }" @click="tab = 'history'">🕘 历史回退</button>
      <button class="tab" @click="backToBoard">← 返回动作看板</button>
    </aside>

    <div class="main">
      <div class="topbar">
        <span class="crumb">
          <a @click="gotoLibrary">精灵库</a> /
          <a @click="backToBoard">{{ store.currentSprite?.name }}</a> /
        </span>
        <!-- 动作切换器：同精灵内横向移动，保持当前工序页签 -->
        <span class="act-switch">
          <button class="small" title="上一个动作（[）" :disabled="switching || actionIndex <= 0"
                  @click="switchAction(-1)">‹</button>
          <select :value="store.currentAction?.id" :disabled="switching"
                  @change="goAction($event.target.value)" title="切换动作（保持当前工序）">
            <option v-for="a in store.spriteActions" :key="a.id" :value="a.id">
              {{ stageMark(a) }} {{ a.name }}</option>
            <option v-if="!store.spriteActions.length" :value="store.currentAction?.id">{{ store.currentAction?.name }}</option>
          </select>
          <button class="small" title="下一个动作（]）"
                  :disabled="switching || actionIndex < 0 || actionIndex >= store.spriteActions.length - 1"
                  @click="switchAction(1)">›</button>
          <span v-if="store.spriteActions.length" class="session-info">{{ actionIndex + 1 }}/{{ store.spriteActions.length }}</span>
        </span>
        <span v-if="store.videoInfo" class="session-info">
          {{ store.videoInfo.width }}x{{ store.videoInfo.height }} · {{ fmtDuration(store.videoInfo.duration) }} · {{ store.frameCount }} 帧
        </span>
        <span class="spacer"></span>
        <span v-if="store.capabilities?.platform" class="session-info">
          {{ store.capabilities.platform.os }}
          <span v-if="store.capabilities.platform.gpu_available" style="color: var(--ok)">· GPU</span>
        </span>
        <ResourceMenu style="margin-left:8px" @open="resModal = $event" />
        <button class="small" style="margin-left:8px" @click="settingsOpen = true">⚙</button>
        <button v-if="authRequired" class="small" style="margin-left:8px" @click="doLogout">退出登录</button>
      </div>

      <div class="main-body">
        <!-- 左侧工作区 -->
        <div class="content-area">
          <div class="content">
            <component :is="views[tab]" />
          </div>
        </div>

        <!-- 右侧视频预览（素材类工序自带预览，其余工序常驻） -->
        <div v-if="!SOURCE_STEPS.has(tab)" class="video-panel-wrap">
          <VideoPanel />
        </div>
      </div>

      <!-- 底部帧管理（类似内容浏览器，可折叠/弹出） -->
      <FrameBrowser />
    </div>
  </div>

  <div v-else-if="err" style="display:flex;height:100%;align-items:center;justify-content:center;">
    <div style="text-align:center;color:var(--err)">
      <h2>无法连接后端服务</h2>
      <p>{{ err }}</p>
      <p class="hint">请确认后端已启动：<code>python backend/run.py</code></p>
    </div>
  </div>

  <div v-else style="display:flex;height:100%;align-items:center;justify-content:center;color:var(--text-dim)">
    正在连接后端服务...
  </div>

  <JobPanel v-if="jobsVisible && jobs.items.length" :jobs="jobs.items"
            @cancel="cancelJob" @close="jobsVisible = false" @jump="jumpToJob" />
  <button v-if="!jobsVisible && jobs.items.length" class="job-toggle" @click="jobsVisible = true">
    后台任务 <span v-if="runningCount()">({{ runningCount() }} 进行中)</span>
  </button>

  <SettingsModal v-if="settingsOpen" @close="settingsOpen = false" />

  <!-- 全局资源库弹窗（顶栏「资源库」菜单） -->
  <TemplateLibraryModal v-if="resModal === 'tpl'" @close="resModal = null" />
  <FfSetLibraryModal v-if="resModal === 'ffset'" @close="resModal = null" />
  <PromptLibraryModal v-if="resModal === 'prompt'" @close="resModal = null" />
  <ProjectTransferModal v-if="resModal === 'transfer'" @close="resModal = null" @imported="onImported" />

  <!-- 全局确认对话框（应用内实现，不依赖可能被浏览器抑制的原生 confirm） -->
  <div v-if="confirmDialog.visible" class="cfm-mask" @click.self="resolveConfirm(confirmDialog.input ? null : false)">
    <div class="cfm-box">
      <p class="cfm-msg">{{ confirmDialog.message }}</p>
      <input v-if="confirmDialog.input" ref="confirmInput" v-model="confirmDialog.input.value"
             :placeholder="confirmDialog.input.placeholder" style="width:100%;margin-bottom:12px"
             @keyup.enter="resolveConfirm(confirmDialog.input.value.trim() || null)" />
      <div class="cfm-ops">
        <button :class="confirmDialog.danger ? 'cfm-danger' : 'primary'"
                @click="resolveConfirm(confirmDialog.input ? (confirmDialog.input.value.trim() || null) : true)">
          确定</button>
        <button @click="resolveConfirm(confirmDialog.input ? null : false)">取消</button>
      </div>
    </div>
  </div>

  <div v-if="store.toast" class="toast">{{ store.toast }}</div>
</template>

<style scoped>
.toplevel { height: 100%; display: flex; flex-direction: column; }
.toplevel-body { flex: 1; min-height: 0; }
.brand-mini { font-size: 14px; font-weight: 700; margin-right: 4px; }
.topnav { display: inline-flex; align-items: center; gap: 6px; margin-left: 6px; }
.topnav a {
  cursor: pointer; font-size: 13px; color: var(--text-dim); padding: 3px 8px; border-radius: 4px;
  text-decoration: none;
}
.topnav a:hover { color: var(--text); background: var(--bg-hover); }
.topnav a.on { color: var(--text); font-weight: 600; }

/* 工序流水线左栏 */
.pipe-title { padding: 6px 16px 4px; font-size: 11px; color: var(--text-dim); letter-spacing: .5px; }
.pipe-group { padding: 8px 16px 2px; font-size: 11px; color: var(--text-dim); }
.sidebar .tab.step { display: flex; align-items: center; gap: 8px; }
.step-ico { width: 16px; text-align: center; font-size: 12px; color: var(--text-dim); }
.st-done .step-ico { color: var(--ok); }
.st-doing .step-ico { color: var(--warn); }
.step-label { flex: 1; }
.step-opt { font-size: 10px; color: var(--text-dim); opacity: .8; }
.sidebar .tab.next-step { color: var(--accent-hover); font-size: 12px; padding-top: 6px; }

/* 动作切换器 */
.act-switch { display: inline-flex; align-items: center; gap: 4px; }
.act-switch select { max-width: 200px; padding: 3px 6px; font-size: 13px; }
.act-switch button { padding: 3px 8px; }
.cfm-mask {
  position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 100;
  display: flex; align-items: center; justify-content: center;
}
.cfm-box {
  width: 380px; max-width: 92vw; background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: 8px; padding: 18px 20px;
}
.cfm-msg { margin: 0 0 14px; font-size: 13px; line-height: 1.7; white-space: pre-line; }
.cfm-ops { display: flex; gap: 10px; justify-content: flex-end; }
.cfm-danger { background: var(--err); border-color: var(--err); color: #fff; }
.crumb { font-size: 13px; color: var(--text-dim); }
.crumb a { color: var(--text-dim); cursor: pointer; text-decoration: none; }
.crumb a:hover { color: var(--accent-hover); }
.crumb b { color: var(--text); }
.main-body {
  flex: 1;
  display: flex;
  min-height: 0;
}
.content-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}
.content {
  flex: 1;
  overflow: auto;
  padding: 16px;
}
.video-panel-wrap {
  width: 42%;
  min-width: 320px;
  border-left: 1px solid var(--border);
  overflow-y: auto;
  background: var(--bg-panel);
}
</style>
