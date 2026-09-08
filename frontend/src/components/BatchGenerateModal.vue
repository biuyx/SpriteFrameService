<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useStore, toast, askConfirm } from '../stores'
import api from '../api'

const props = defineProps({
  actions: { type: Array, required: true },
  preselected: { type: Array, default: null },   // 看板多选带入：只勾选这些（且可生成）
})
const emit = defineEmits(['close', 'done'])
const store = useStore()

const gen = ref(null)
const model = ref('')
const resolution = ref('480p')
const checked = ref({})            // action_id -> bool
const templatesById = ref({})
const promptOf = ref({})           // action_id -> {name, version, source}（提示词库解析结果）
const SOURCE_TXT = { action: '记忆', template: '模板', key: 'key', group: '分组', global: '全局', builtin: '内置' }
const phase = ref('pick')          // pick | running
const rows = ref([])               // 进度行 [{action_id,name,job_id,status,progress,message,error,variant,duration}]
const skippedRows = ref([])

// 可生成 = 有首帧 + 有模板;默认勾选其中未生成过的
const eligible = (a) => a.summary?.has_first_frame && !!a.template_id
onMounted(async () => {
  try {
    gen.value = await api.genCapabilities()
    model.value = gen.value.default_model
    const t = await api.templates()
    for (const x of t.templates) templatesById.value[x.id] = x
  } catch (e) { toast(`加载失败: ${e.message}`) }
  const pre = props.preselected ? new Set(props.preselected) : null
  for (const a of props.actions) {
    checked.value[a.id] = pre ? (pre.has(a.id) && eligible(a))
      : (eligible(a) && !a.summary?.generated_count && !a.summary?.generating_count)
  }
  try {
    const r = await api.resolvePrompts('video_ref', store.currentSprite.id, props.actions.map(a => a.id))
    promptOf.value = r.resolved
  } catch { /* 提示词库不可用时不显示 */ }
})

function promptLabel(a) {
  const p = promptOf.value[a.id]
  return p ? `${p.name}${p.version ? ` v${p.version}` : ''}（${SOURCE_TXT[p.source] || p.source}）` : ''
}

function tplLabel(a) {
  const t = templatesById.value[a.template_id]
  if (!t) return ''
  return `${t.variant || t.key}${t.duration_hint ? `·${t.duration_hint}s` : ''}`
}

const selectedIds = computed(() =>
  props.actions.filter(a => checked.value[a.id]).map(a => a.id))

function selectUngen() {
  for (const a of props.actions)
    checked.value[a.id] = eligible(a) && !a.summary?.generated_count
}
function selectNone() { for (const a of props.actions) checked.value[a.id] = false }

async function start() {
  const n = selectedIds.value.length
  if (!n) return toast('请至少勾选一个动作')
  if (!(await askConfirm(
    `提交 ${n} 个生成任务？\n模型 ${model.value.includes('mini') ? 'Mini' : model.value.includes('fast') ? 'Fast' : 'Pro'} · ${resolution.value}，时长按各动作模板。\n每个任务按 Ark 实际用量计费。`))) return
  try {
    const r = await api.batchGenerate(store.currentSprite.id, {
      action_ids: selectedIds.value, model: model.value, resolution: resolution.value,
    })
    skippedRows.value = r.skipped
    rows.value = r.submitted.map(x => ({ ...x, status: 'queued', progress: 0, message: '', error: null }))
    phase.value = 'running'
    startPolling()
    if (r.skipped.length) toast(`已提交 ${r.submitted.length} 个，跳过 ${r.skipped.length} 个`)
  } catch (e) {
    toast(`提交失败: ${e.message}`)
  }
}

let timer = null
function startPolling() {
  stopPolling()
  timer = setInterval(poll, 2500)
  poll()
}
function stopPolling() { if (timer) { clearInterval(timer); timer = null } }
onUnmounted(stopPolling)

async function poll() {
  let active = false
  for (const row of rows.value) {
    if (['done', 'error', 'cancelled'].includes(row.status)) continue
    try {
      const j = await api.job(row.job_id)
      row.status = j.status
      row.progress = j.progress
      row.message = j.message
      row.error = j.error
    } catch (e) {
      if (e.status === 404) {         // 服务重启任务丢失：终止而不是无限重试
        row.status = 'error'
        row.error = '任务记录不存在（服务可能已重启）'
      }
      /* 其他错误视为网络瞬断,下轮再试 */
    }
    if (!['done', 'error', 'cancelled'].includes(row.status)) active = true
  }
  if (!active && rows.value.length) {
    stopPolling()
    emit('done')
    const fail = rows.value.filter(r => r.status !== 'done').length
    toast(fail ? `批量生成结束：${rows.value.length - fail} 成功 / ${fail} 失败` : '批量生成全部完成')
  }
}

async function retry(row) {
  try {
    const r = await api.batchGenerate(store.currentSprite.id, {
      action_ids: [row.action_id], model: model.value, resolution: resolution.value,
    })
    if (r.submitted.length) {
      Object.assign(row, { ...r.submitted[0], status: 'queued', progress: 0, message: '', error: null })
      startPolling()
    } else if (r.skipped.length) {
      toast(`无法重试: ${r.skipped[0].reason}`)
    }
  } catch (e) { toast(`重试失败: ${e.message}`) }
}

const doneCount = computed(() => rows.value.filter(r => r.status === 'done').length)
const failCount = computed(() => rows.value.filter(r => r.status === 'error').length)

const STATUS_TXT = { queued: '排队', running: '生成中', done: '✓ 完成', error: '✗ 失败', cancelled: '已取消' }
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <div class="modal-head">
        <h3>批量生成（{{ store.currentSprite?.name }}）</h3>
        <button class="small" @click="emit('close')">✕</button>
      </div>

      <!-- 勾选阶段 -->
      <template v-if="phase === 'pick'">
        <div class="row" style="gap:10px;margin-bottom:8px;align-items:center">
          <div class="field inline"><label>模型</label>
            <select v-model="model">
              <option v-for="m in gen?.models || []" :key="m.id" :value="m.id">{{ m.label }}</option>
            </select></div>
          <div class="field inline"><label>分辨率</label>
            <select v-model="resolution">
              <option v-for="r in gen?.params?.resolution || []" :key="r">{{ r }}</option>
            </select></div>
          <span class="hint">时长按动作记忆/模板推荐值；提示词按提示词库逐动作匹配</span>
        </div>
        <div class="row" style="gap:8px;margin-bottom:6px">
          <button class="small" @click="selectUngen">选未生成的</button>
          <button class="small" @click="selectNone">全不选</button>
          <span class="hint">已勾选 {{ selectedIds.length }} 个</span>
        </div>
        <div class="act-list">
          <label v-for="a in props.actions" :key="a.id" class="act-row"
                 :class="{ disabled: !eligible(a) }">
            <input type="checkbox" v-model="checked[a.id]" :disabled="!eligible(a)" />
            <b>{{ a.name }}</b>
            <span class="hint">{{ tplLabel(a) }}</span>
            <span v-if="promptLabel(a)" class="hint prompt-tag" :title="'提示词：' + promptLabel(a)">✎ {{ promptLabel(a) }}</span>
            <span class="spacer" style="flex:1"></span>
            <span v-if="!a.summary?.has_first_frame" class="warn-text">缺首帧</span>
            <span v-else-if="!a.template_id" class="warn-text">无模板</span>
            <span v-else-if="a.summary?.generating_count" class="warn-text">生成中</span>
            <span v-else-if="a.summary?.generated_count" class="ok-text">已有 {{ a.summary.generated_count }} 版</span>
          </label>
        </div>
        <div class="modal-foot">
          <button class="primary" :disabled="!selectedIds.length" @click="start">
            开始生成（{{ selectedIds.length }} 个）</button>
          <button @click="emit('close')">取消</button>
        </div>
      </template>

      <!-- 进度阶段 -->
      <template v-else>
        <div class="prog-summary">
          完成 {{ doneCount }} / {{ rows.length }}
          <span v-if="failCount" class="warn-text">（失败 {{ failCount }}）</span>
          <div class="prog-bar"><div class="prog-fill"
               :style="{ width: (doneCount / rows.length * 100) + '%' }"></div></div>
        </div>
        <div class="act-list">
          <div v-for="r in rows" :key="r.action_id" class="act-row">
            <b>{{ r.name }}</b>
            <span class="hint">{{ r.variant }}{{ r.duration ? `·${r.duration}s` : '' }}</span>
            <span class="spacer" style="flex:1"></span>
            <span :class="{ 'ok-text': r.status === 'done', 'warn-text': r.status === 'error' }">
              {{ STATUS_TXT[r.status] || r.status }}
              <template v-if="r.status === 'running'"> {{ Math.round(r.progress) }}%</template>
            </span>
            <button v-if="r.status === 'error'" class="small" @click="retry(r)">重试</button>
          </div>
          <div v-for="s in skippedRows" :key="s.action_id" class="act-row disabled">
            <b>{{ s.name }}</b><span class="spacer" style="flex:1"></span>
            <span class="warn-text">跳过：{{ s.reason }}</span>
          </div>
        </div>
        <p class="hint" style="margin-top:6px">可关闭本窗口，任务在后台继续（任务面板可见）。</p>
        <div class="modal-foot">
          <button @click="emit('close')">关闭</button>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.modal-mask {
  position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 90;
  display: flex; align-items: center; justify-content: center;
}
.modal {
  width: 600px; max-width: 94vw; max-height: 86vh; overflow-y: auto;
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 20px 18px;
}
.modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.modal-head h3 { margin: 0; font-size: 16px; }
.act-list {
  border: 1px solid var(--border); border-radius: 5px; max-height: 320px; overflow-y: auto;
  margin-bottom: 12px;
}
.act-row {
  display: flex; align-items: center; gap: 10px; padding: 6px 12px;
  border-bottom: 1px solid var(--border); font-size: 13px; cursor: pointer;
}
.act-row:last-child { border-bottom: none; }
.act-row.disabled { opacity: .55; cursor: default; }
.act-row:not(.disabled):hover { background: var(--bg-hover); }
.warn-text { color: var(--warn); font-size: 12px; }
.ok-text { color: var(--ok); font-size: 12px; }
.prompt-tag { max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.modal-foot { display: flex; gap: 10px; justify-content: flex-end; }
.prog-summary { font-size: 13px; margin-bottom: 10px; }
.prog-bar {
  height: 6px; background: var(--bg-input); border-radius: 3px; margin-top: 6px; overflow: hidden;
}
.prog-fill { height: 100%; background: var(--accent); transition: width .4s; }
</style>
