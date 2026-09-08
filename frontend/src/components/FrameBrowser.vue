<script setup>
import { ref, computed, watch } from 'vue'
import { useStore, refreshFrames, toast, askConfirm } from '../stores'
import api from '../api'
import FrameGallery from './FrameGallery.vue'
import FramePackModal from './FramePackModal.vue'

const store = useStore()

// 面板状态
const collapsed = ref(false)      // 折叠为细条
const popped = ref(false)         // 弹出为大视图
const packOpen = ref(false)       // 帧包导出/导入弹窗
const height = ref(240)           // 展开高度
const minHeight = 80
const maxHeight = 600

const selectedCount = computed(() =>
  store.frames.filter((f) => f.is_selected).length
)

const rangeStart = ref(0)
const rangeEnd = ref(0)

async function sel(mode, extra = {}) {
  await api.selection(store.sessionId, { mode, ...extra })
  await refreshFrames()
}

async function delSelected() {
  const idx = store.frames.filter((f) => f.is_selected).map((f) => f.index)
  if (!idx.length) return toast('没有选中的帧')
  if (!(await askConfirm(`删除选中的 ${idx.length} 帧？`, { danger: true }))) return
  await api.deleteFrames(store.sessionId, idx)
  await refreshFrames()
  toast('已删除')
}

async function applyRange() {
  if (rangeStart.value > rangeEnd.value) [rangeStart.value, rangeEnd.value] = [rangeEnd.value, rangeStart.value]
  await sel('range', { range_start: rangeStart.value, range_end: rangeEnd.value })
}

// 以选中的那一帧为首帧，为同一精灵新建动作（血统边 + 物理拷贝首帧图）
async function newActionFromFrame() {
  const idx = store.frames.filter((f) => f.is_selected).map((f) => f.index)
  if (idx.length !== 1) return toast('请恰好选中 1 帧作为新动作的首帧')
  const name = await askConfirm(`以第 ${idx[0]} 帧为首帧，为「${store.currentSprite?.name}」新建动作`, { input: { placeholder: '动作名称，如 idle' } })
  if (!name) return
  const act = await api.createAction(store.currentSprite.id, name, {
    kind: 'action_frame', action: store.sessionId, frame_index: idx[0],
  })
  toast(`已创建动作「${act.name}」（首帧 = 本动作第 ${idx[0]} 帧），可从动作看板进入`)
}

// 拖拽调整高度
const dragging = ref(false)
function startDrag(e) {
  dragging.value = true
  const onMove = (ev) => {
    const delta = (window.innerHeight - ev.clientY) - height.value
    height.value = Math.min(maxHeight, Math.max(minHeight, height.value + delta))
  }
  const onUp = () => {
    dragging.value = false
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
  }
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}

watch(collapsed, (c) => { if (popped.value && c) popped.value = false })

// 没有帧时自动折叠（不占工作区），抽出帧后自动展开
watch(() => store.frameCount, (n, old) => {
  if (n === 0) collapsed.value = true
  else if (old === 0 && n > 0) collapsed.value = false
}, { immediate: true })</script>

<template>
  <div
    class="frame-browser"
    :class="{ collapsed, popped, dragging }"
    :style="collapsed || popped ? {} : { height: height + 'px' }"
  >
    <!-- 顶部拖拽条 -->
    <div v-if="!collapsed && !popped" class="browser-resize" @mousedown="startDrag" title="拖动调整高度"></div>

    <!-- 头部 -->
    <div class="browser-header">
      <span class="browser-title">帧管理</span>
      <span v-if="store.frameCount" class="hint">已选 {{ selectedCount }} / {{ store.frameCount }}</span>
      <span v-else class="hint">暂无帧</span>

      <span class="spacer" style="flex:1"></span>

      <template v-if="!collapsed && store.frameCount">
        <button class="small" @click="sel('all')">全选</button>
        <button class="small" @click="sel('clear')">全不选</button>
        <button class="small" @click="sel('invert')">反选</button>
        <span class="hint">|</span>
        <div class="field inline"><label>从</label><input type="number" v-model.number="rangeStart" :max="store.frameCount - 1" /></div>
        <div class="field inline"><label>到</label><input type="number" v-model.number="rangeEnd" :max="store.frameCount - 1" /></div>
        <button class="small" @click="applyRange">区间选帧</button>
        <span class="hint">|</span>
        <button class="small" :disabled="selectedCount !== 1"
                title="以选中的那一帧为首帧新建动作（血统可追溯）"
                @click="newActionFromFrame">以此帧建动作</button>
        <button class="small danger" @click="delSelected">删除选中</button>
        <span class="hint">|</span>
        <button class="small" title="帧打包下载给外部工具加工，或把加工结果导回替换"
                @click="packOpen = true">帧包</button>
      </template>

      <button class="small" :title="collapsed ? '展开' : '折叠'" @click="collapsed = !collapsed">
        {{ collapsed ? '▲ 展开' : '▼ 折叠' }}
      </button>
      <button class="small" title="弹出/还原" @click="popped = !popped">
        {{ popped ? '⤓ 还原' : '⤢ 弹出' }}
      </button>
    </div>

    <!-- 内容区（弹出时隐藏，由覆盖层展示） -->
    <div v-show="!collapsed && !popped" class="browser-body">
      <FrameGallery />
    </div>
  </div>

  <FramePackModal v-if="packOpen" @close="packOpen = false" />

  <!-- 弹出的大视图 -->
  <div v-if="popped" class="browser-overlay" @click.self="popped = false">
    <div class="browser-overlay-panel">
      <div class="browser-header">
        <span class="browser-title">帧管理</span>
        <span v-if="store.frameCount" class="hint">已选 {{ selectedCount }} / {{ store.frameCount }}</span>
        <span class="spacer" style="flex:1"></span>
        <button class="small" @click="sel('all')">全选</button>
        <button class="small" @click="sel('clear')">全不选</button>
        <button class="small" @click="sel('invert')">反选</button>
        <button class="small danger" @click="delSelected">删除选中</button>
        <button class="small" @click="popped = false">关闭</button>
      </div>
      <div class="browser-overlay-body">
        <FrameGallery />
      </div>
    </div>
  </div>
</template>

<style scoped>
.frame-browser {
  background: var(--bg-panel);
  border-top: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  flex-shrink: 0;
}
.frame-browser.collapsed, .frame-browser.popped { height: 38px; }

.browser-resize {
  height: 5px;
  cursor: ns-resize;
  background: transparent;
  flex-shrink: 0;
}
.browser-resize:hover, .frame-browser.dragging .browser-resize {
  background: var(--accent);
  opacity: .6;
}

.browser-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  flex-shrink: 0;
  flex-wrap: wrap;
  border-bottom: 1px solid var(--border);
}
.browser-title { font-weight: 600; font-size: 13px; }

.browser-body {
  flex: 1;
  overflow-y: auto;
  padding: 10px 12px;
}

/* 弹出层 */
.browser-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,.55);
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.browser-overlay-panel {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.browser-overlay-body { flex: 1; overflow-y: auto; padding: 12px; }
</style>
