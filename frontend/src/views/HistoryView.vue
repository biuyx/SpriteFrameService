<script setup>
import { ref, computed, onMounted } from 'vue'
import { useStore, refreshFrames, toast } from '../stores'
import { startJob } from '../jobs'
import api from '../api'

const store = useStore()
const entries = ref([])
const memory = ref('')
const busy = ref(false)
const recipe = ref({ source: null, steps: [] })

const OP_LABEL = {
  extract: '抽帧', remove_similar: '去相似', background: '抠图', outline: '描边',
  scale: '缩放', crop_whitespace: '空白裁剪', optimize_edges: '边缘优化',
  enhance: 'AI增强', wand_apply: '魔棒编辑', supplement: '补帧',
  export: '导出', revert: '历史回退',
}

function fmtParams(p) {
  if (!p) return ''
  return Object.entries(p)
    .filter(([k]) => k !== 'frame_indices' && k !== 'indices')
    .map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(',') : v}`)
    .join(' · ')
}

async function load() {
  try {
    const r = await api.history(store.sessionId)
    entries.value = r.entries
    memory.value = r.memory || ''
  } catch {
    entries.value = []
  }
  try {
    recipe.value = await api.recipe(store.sessionId)
  } catch {
    recipe.value = { source: null, steps: [] }
  }
}

function fmtTime(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

async function revertTo(stepId) {
  const isInit = stepId === 0
  if (!confirm(isInit ? '回退到初始状态（撤销全部修改）？' : '回退到该步骤完成后的状态（撤销之后的操作）？')) return
  busy.value = true
  await startJob(() => api.revert(store.sessionId, stepId), {
    title: '历史回退',
    onDone: async (r) => {
      await refreshFrames()
      await load()
      toast(r.count ? `已回退，影响 ${r.count} 帧` : '该步骤没有可回退的修改')
    },
  })
  busy.value = false
}

// 撤销上一步：回退到倒数第二条记录的位置（仅 1 条时回退到初始）
function undoLast() {
  if (!entries.value.length) return
  const target = entries.value.length >= 2 ? entries.value[1].step_id : 0
  revertTo(target)
}

onMounted(load)
</script>

<template>
  <div class="panel">
    <div class="section-title"><h2>历史回退</h2></div>
    <p class="desc">对帧的修改（抠图/描边/缩放/裁剪/边缘优化/增强/魔棒编辑）会记录历史快照，可回退到任意步骤。最多保留最近 10 步。</p>

    <div class="row">
      <button class="primary" :disabled="busy || !entries.length" @click="undoLast">撤销上一步</button>
      <button :disabled="busy || !entries.length" @click="revertTo(0)">回退到初始状态</button>
      <button class="small" @click="load">刷新</button>
      <span v-if="memory" class="hint">快照占用：{{ memory }}</span>
    </div>

    <div v-if="entries.length" class="history-list">
      <div v-for="(e, i) in entries" :key="e.step_id" class="history-item">
        <div class="row2">
          <span class="mono">#{{ e.step_id }} · {{ e.operation_name }}</span>
          <span class="hint">{{ fmtTime(e.timestamp) }} · {{ e.affected_count }} 帧</span>
        </div>
        <div class="history-desc">{{ e.description }}</div>
        <div class="row" style="margin-top:4px">
          <button class="small" :disabled="busy || i === 0" @click="revertTo(e.step_id)">回退到此处</button>
          <span v-if="i === 0" class="hint">（最新步骤，无需回退）</span>
        </div>
      </div>
    </div>
    <p v-else class="hint">暂无历史记录。对帧执行处理操作后会自动生成快照。</p>

    <div class="section-title" style="margin-top:22px"><h2>工序记录</h2></div>
    <p class="desc">本动作从素材到导出的完整参数链，用于追溯与复现。</p>

    <div v-if="recipe.source" class="recipe-step recipe-src">
      <b>素材</b>
      <span>{{ recipe.source.kind === 'upload' ? '上传' : recipe.source.kind }}
        {{ recipe.source.filename || '' }}
        <template v-if="recipe.source.video">
          · {{ recipe.source.video.width }}x{{ recipe.source.video.height }}
          · {{ recipe.source.video.duration?.toFixed(1) }}s
        </template>
      </span>
    </div>
    <div v-for="(s, i) in recipe.steps" :key="i" class="recipe-step">
      <b>{{ OP_LABEL[s.op] || s.op }}</b>
      <span class="mono-sm">{{ fmtParams(s.params) }}</span>
      <span v-if="s.result" class="hint">→ {{ fmtParams(s.result) }}</span>
    </div>
    <p v-if="!recipe.source && !recipe.steps.length" class="hint">
      暂无工序记录。上传素材并执行处理后自动累积。</p>
  </div>
</template>

<style scoped>
.history-list { display: flex; flex-direction: column; gap: 8px; max-height: 60vh; overflow-y: auto; }
.history-item {
  background: var(--bg-input); border: 1px solid var(--border); border-radius: 4px; padding: 8px 12px;
}
.history-desc { color: var(--text-dim); font-size: 12px; margin-top: 2px; }
.recipe-step {
  display: flex; align-items: baseline; gap: 10px; padding: 6px 12px;
  background: var(--bg-input); border: 1px solid var(--border); border-radius: 4px;
  margin-bottom: 5px; font-size: 12px;
}
.recipe-step b { min-width: 64px; }
.recipe-src { border-left: 2px solid var(--accent); }
.mono-sm { font-family: Consolas, monospace; font-size: 11px; color: var(--text-dim); }
</style>
