<script setup>
import { ref, watch, onMounted } from 'vue'
import { useStore, gotoLibrary, gotoSprite, loadCapabilities, toast } from './stores'
import { useJobs, cancelJob } from './jobs'
import { currentTab } from './nav'
import api, { setUnauthorizedHandler } from './api'
import LoginView from './components/LoginView.vue'
import SettingsModal from './components/SettingsModal.vue'
import SpriteLibraryView from './views/SpriteLibraryView.vue'
import SpriteDetailView from './views/SpriteDetailView.vue'
import VideoView from './views/VideoView.vue'
import AnalysisView from './views/AnalysisView.vue'
import BackgroundView from './views/BackgroundView.vue'
import ImageOpsView from './views/ImageOpsView.vue'
import EditorView from './views/EditorView.vue'
import ExportView from './views/ExportView.vue'
import HistoryView from './views/HistoryView.vue'
import JobPanel from './components/JobPanel.vue'
import FrameBrowser from './components/FrameBrowser.vue'
import VideoPanel from './components/VideoPanel.vue'

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

// 有新任务启动时自动展开任务栏
watch(() => jobs.items.length, (n, old) => {
  if (n > old) jobsVisible.value = true
})

const runningCount = () => jobs.items.filter((j) => j.status === 'running').length

const tabs = [
  { key: 'video', label: '视频抽帧' },
  { key: 'analysis', label: '动作分析' },
  { key: 'background', label: '背景处理' },
  { key: 'image', label: '图像处理' },
  { key: 'editor', label: '魔棒编辑' },
  { key: 'export', label: '导出' },
  { key: 'history', label: '历史回退' },
]

const views = {
  video: VideoView,
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

async function doLogout() {
  if (!confirm('退出登录？')) return
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
  tab.value = 'video'
}
</script>

<template>
  <LoginView v-if="needLogin" :notice="loginNotice" @authenticated="onAuthenticated" />

  <!-- 一层：精灵库 -->
  <div class="toplevel" v-else-if="ready && store.view === 'library'">
    <SpriteLibraryView />
    <div class="corner-ops">
      <button class="small" @click="settingsOpen = true">⚙ 设置</button>
      <button v-if="authRequired" class="small" @click="doLogout">退出登录</button>
    </div>
  </div>

  <!-- 二层：动作看板 -->
  <div class="toplevel" v-else-if="ready && store.view === 'sprite'">
    <SpriteDetailView />
  </div>

  <!-- 三层：动作工作台 -->
  <div class="layout" v-else-if="ready">
    <aside class="sidebar">
      <div class="brand">精灵帧工作室<small>SpriteFrameService</small></div>
      <button
        v-for="t in tabs" :key="t.key"
        class="tab" :class="{ active: tab === t.key }"
        @click="tab = t.key"
      >{{ t.label }}</button>
      <div class="spacer"></div>
      <button class="tab" @click="backToBoard">← 返回动作看板</button>
    </aside>

    <div class="main">
      <div class="topbar">
        <span class="crumb">
          <a @click="gotoLibrary">精灵库</a> /
          <a @click="backToBoard">{{ store.currentSprite?.name }}</a> /
          <b>{{ store.currentAction?.name }}</b>
        </span>
        <span v-if="store.videoInfo" class="session-info">
          {{ store.videoInfo.width }}x{{ store.videoInfo.height }} · {{ fmtDuration(store.videoInfo.duration) }} · {{ store.frameCount }} 帧
        </span>
        <span class="spacer"></span>
        <span v-if="store.capabilities?.platform" class="session-info">
          {{ store.capabilities.platform.os }}
          <span v-if="store.capabilities.platform.gpu_available" style="color: var(--ok)">· GPU</span>
        </span>
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

        <!-- 右侧视频预览（除视频抽帧外常驻） -->
        <div v-if="tab !== 'video'" class="video-panel-wrap">
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

  <JobPanel v-if="jobsVisible && jobs.items.length" :jobs="jobs.items" @cancel="cancelJob" @close="jobsVisible = false" />
  <button v-if="!jobsVisible && jobs.items.length" class="job-toggle" @click="jobsVisible = true">
    后台任务 <span v-if="runningCount()">({{ runningCount() }} 进行中)</span>
  </button>

  <SettingsModal v-if="settingsOpen" @close="settingsOpen = false" />

  <div v-if="store.toast" class="toast">{{ store.toast }}</div>
</template>

<style scoped>
.toplevel { height: 100%; position: relative; }
.corner-ops { position: absolute; top: 18px; right: 24px; display: flex; gap: 8px; }
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
