<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useStore, toast, askConfirm } from '../stores'
import api from '../api'

// 自动流水线：计划预览（每动作每步 执行/跳过/阻塞）→ 执行 → 进度；首帧后可暂停等确认
const props = defineProps({
  actions: { type: Array, required: true },
  preselected: { type: Array, default: null },
})
const emit = defineEmits(['close', 'done'])
const store = useStore()

const STEPS = [
  { key: 'firstframe', label: '首帧' },
  { key: 'generate', label: '视频生成' },
  { key: 'extract', label: '抽帧' },
  { key: 'matting', label: '抠图' },
  { key: 'export', label: '导出' },
]
const stepOn = ref({ firstframe: true, generate: true, extract: true, matting: true, export: true })
const force = ref(false)               // 强制重做已完成步骤
const pauseAfterFf = ref(true)         // 首帧生成后暂停等确认
const sets = ref([])
const setId = ref('')                  // '' = 自动匹配
const plans = ref([])
const planning = ref(false)
const phase = ref('pick')              // pick | running
const rows = ref([])
const skippedRows = ref([])

const targetIds = computed(() => props.preselected?.length
  ? props.preselected : props.actions.map(a => a.id))
const steps = computed(() => STEPS.map(s => s.key).filter(k => stepOn.value[k]))

const ICON = { run: '▶', skip: '⏭', blocked: '⛔' }
const STATUS_TXT = { queued: '排队', running: '进行中', paused: '待确认', done: '完成', error: '失败' }

let planTimer = null
async function replan() {
  if (!steps.value.length) { plans.value = []; return }
  planning.value = true
  try {
    const r = await api.pipelinePlan(store.currentSprite.id, {
      action_ids: targetIds.value, steps: steps.value,
      force: force.value ? steps.value : [], set_id: setId.value || null,
    })
    plans.value = r.plans
  } catch (e) { toast(`计划失败: ${e.message}`) } finally { planning.value = false }
}
watch([stepOn, force, setId], () => { clearTimeout(planTimer); planTimer = setTimeout(replan, 250) }, { deep: true })

const runnable = computed(() => plans.value.filter(p => p.runnable))
const cost = computed(() => ({
  images: plans.value.filter(p => p.steps.firstframe?.status === 'run').length,
  videos: plans.value.filter(p => p.steps.generate?.status === 'run').length,
}))

onMounted(async () => {
  try { sets.value = (await api.ffsets()).sets } catch { /* ignore */ }
  await replan()
})

async function start() {
  if (!runnable.value.length) return toast('没有可执行的动作')
  const c = cost.value
  const money = (c.images || c.videos) ? `\n预计消耗：生图 ${c.images} 张、视频 ${c.videos} 个（按量计费）。` : ''
  const pause = stepOn.value.firstframe && pauseAfterFf.value ? '\n首帧生成后会暂停，确认后再继续。' : ''
  if (!(await askConfirm(`对 ${runnable.value.length} 个动作执行流水线（${steps.value.map(k => STEPS.find(s => s.key === k).label).join(' → ')}）？${money}${pause}`))) return
  try {
    const r = await api.pipelineStart(store.currentSprite.id, {
      action_ids: runnable.value.map(p => p.action_id), steps: steps.value,
      force: force.value ? steps.value : [], set_id: setId.value || null,
      pause_after_firstframe: pauseAfterFf.value,
    })
    skippedRows.value = r.skipped
    rows.value = r.submitted.map(x => ({ ...x, status: 'queued', progress: 0, message: '', error: null, pstatus: null }))
    phase.value = 'running'
    startPolling()
  } catch (e) { toast(`提交失败: ${e.message}`) }
}

let timer = null
function startPolling() { if (!timer) { timer = setInterval(poll, 2500); poll() } }
function stopPolling() { if (timer) { clearInterval(timer); timer = null } }
onUnmounted(() => { stopPolling(); clearTimeout(planTimer) })

async function poll() {
  let active = false
  for (const row of rows.value) {
    if (['done', 'error', 'cancelled'].includes(row.status)) continue
    try {
      const j = await api.job(row.job_id)
      row.status = j.status; row.progress = j.progress; row.message = j.message; row.error = j.error
      if (j.status === 'done') row.pstatus = j.result?.status || 'done'
      if (j.status === 'error') row.pstatus = 'error'
    } catch (e) {
      if (e.status === 404) { row.status = 'error'; row.error = '任务记录不存在（服务可能已重启）'; row.pstatus = 'error' }
    }
    if (!['done', 'error', 'cancelled'].includes(row.status)) active = true
  }
  if (!active) {
    stopPolling()
    emit('done')
    const paused = rows.value.filter(r => r.pstatus === 'paused').length
    const fail = rows.value.filter(r => r.pstatus === 'error').length
    toast(paused ? `${paused} 个动作首帧已生成，等待确认` : fail ? `流水线结束：${fail} 个失败` : '流水线全部完成')
  }
}

const pausedRows = computed(() => rows.value.filter(r => r.pstatus === 'paused'))
const failedRows = computed(() => rows.value.filter(r => r.pstatus === 'error'))

async function resume(list) {
  if (!list.length) return
  try {
    const r = await api.pipelineResume(store.currentSprite.id, list.map(x => x.action_id))
    for (const s of r.submitted) {
      const row = rows.value.find(x => x.action_id === s.action_id)
      if (row) Object.assign(row, { job_id: s.job_id, status: 'queued', progress: 0, message: '', error: null, pstatus: null })
    }
    startPolling()
  } catch (e) { toast(`继续失败: ${e.message}`) }
}
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <div class="modal-head">
        <h3>一键流水线（{{ store.currentSprite?.name }}）</h3>
        <button class="small" @click="emit('close')">✕</button>
      </div>

      <template v-if="phase === 'pick'">
        <div class="opts">
          <div class="row" style="gap:12px;align-items:center;flex-wrap:wrap">
            <span class="hint">步骤：</span>
            <label v-for="s in STEPS" :key="s.key" class="chk">
              <input type="checkbox" v-model="stepOn[s.key]" /> {{ s.label }}</label>
            <span class="sep">|</span>
            <label class="chk" title="已完成的步骤也重新执行（重新生成/重抽/重抠/重导）">
              <input type="checkbox" v-model="force" /> 强制重做</label>
          </div>
          <div class="row" style="gap:12px;align-items:center;flex-wrap:wrap;margin-top:6px">
            <label class="chk" :class="{ off: !stepOn.firstframe }" title="首帧生成后停下来，人工过目再继续后续步骤">
              <input type="checkbox" v-model="pauseAfterFf" :disabled="!stepOn.firstframe" /> 首帧生成后暂停等确认</label>
            <span class="sep">|</span>
            <div class="field inline"><label>首帧参考集</label>
              <select v-model="setId">
                <option value="">自动匹配（同分组 / 含该动作）</option>
                <option v-for="s in sets" :key="s.id" :value="s.id">{{ s.name }}（{{ s.frames.length }}）</option>
              </select></div>
          </div>
        </div>

        <div class="plan-wrap">
          <table class="plan-tbl">
            <thead><tr>
              <th>动作</th>
              <th v-for="s in STEPS.filter(x => stepOn[x.key])" :key="s.key" style="width:86px">{{ s.label }}</th>
              <th style="width:90px">状态</th>
            </tr></thead>
            <tbody>
              <tr v-for="p in plans" :key="p.action_id" :class="{ dim: !p.runnable }">
                <td><b>{{ p.name }}</b></td>
                <td v-for="s in STEPS.filter(x => stepOn[x.key])" :key="s.key" class="cell" :class="p.steps[s.key]?.status"
                    :title="p.steps[s.key]?.reason || p.steps[s.key]?.note || p.steps[s.key]?.template || p.steps[s.key]?.rule || p.steps[s.key]?.set_name || ''">
                  {{ ICON[p.steps[s.key]?.status] || '' }}
                  <span class="cell-txt">{{ p.steps[s.key]?.status === 'blocked' ? p.steps[s.key].reason : (p.steps[s.key]?.status === 'skip' ? '已完成' : '') }}</span>
                </td>
                <td class="hint">{{ p.pipeline?.status ? (STATUS_TXT[p.pipeline.status] || p.pipeline.status) : '' }}</td>
              </tr>
              <tr v-if="!plans.length"><td :colspan="steps.length + 2" class="hint" style="text-align:center;padding:16px">
                {{ planning ? '计算中…' : '没有动作' }}</td></tr>
            </tbody>
          </table>
        </div>
        <p class="hint" style="margin:6px 0 0">
          ▶ 将执行 · ⏭ 已完成跳过 · ⛔ 阻塞（悬停看原因）。可执行 {{ runnable.length }}/{{ plans.length }} 个；
          预计消耗：生图 {{ cost.images }} 张、视频 {{ cost.videos }} 个。</p>
        <div class="modal-foot">
          <button class="primary" :disabled="!runnable.length || planning" @click="start">
            开始执行（{{ runnable.length }} 个）</button>
          <button @click="emit('close')">取消</button>
        </div>
      </template>

      <template v-else>
        <div class="act-list">
          <div v-for="r in rows" :key="r.action_id" class="act-row">
            <b>{{ r.name }}</b>
            <span class="hint msg" :title="r.error || r.message">{{ r.message }}</span>
            <span class="spacer" style="flex:1"></span>
            <span :class="{ 'ok-text': r.pstatus === 'done', 'warn-text': r.pstatus === 'error' || r.pstatus === 'paused' }">
              {{ r.pstatus ? (STATUS_TXT[r.pstatus] || r.pstatus) : (r.status === 'running' ? Math.round(r.progress) + '%' : STATUS_TXT[r.status] || r.status) }}</span>
            <button v-if="r.pstatus === 'paused'" class="small" @click="resume([r])">确认继续</button>
            <button v-if="r.pstatus === 'error'" class="small" @click="resume([r])">重试</button>
          </div>
          <div v-for="s in skippedRows" :key="s.action_id" class="act-row dim">
            <b>{{ s.name }}</b><span class="spacer" style="flex:1"></span>
            <span class="warn-text">跳过：{{ s.reason }}</span>
          </div>
        </div>
        <p class="hint" style="margin:6px 0 0">
          待确认的动作：到「首帧图库 › 动作首帧总览」过目，不满意可单张重生成，然后点「确认继续」。</p>
        <div class="modal-foot">
          <button v-if="pausedRows.length" class="primary" @click="resume(pausedRows)">确认并继续全部（{{ pausedRows.length }}）</button>
          <button v-if="failedRows.length" class="small" @click="resume(failedRows)">重试失败（{{ failedRows.length }}）</button>
          <button @click="emit('close')">关闭</button>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 90; display: flex; align-items: center; justify-content: center; }
.modal { width: 760px; max-width: 94vw; max-height: 88vh; overflow-y: auto; background: var(--bg-panel); border: 1px solid var(--border); border-radius: 8px; padding: 16px 20px 18px; }
.modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.modal-head h3 { margin: 0; font-size: 16px; }
.opts { background: var(--bg-input); border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; margin-bottom: 10px; }
.chk { display: flex; align-items: center; gap: 5px; font-size: 13px; cursor: pointer; }
.chk.off { opacity: .5; }
.sep { color: var(--border); }
.plan-wrap { border: 1px solid var(--border); border-radius: 5px; max-height: 360px; overflow: auto; }
.plan-tbl { width: 100%; border-collapse: collapse; font-size: 12px; }
.plan-tbl th { text-align: left; font-weight: 500; color: var(--text-dim); padding: 6px 8px; border-bottom: 1px solid var(--border); background: var(--bg-input); position: sticky; top: 0; }
.plan-tbl td { padding: 5px 8px; border-bottom: 1px solid var(--border); }
.plan-tbl tr.dim { opacity: .55; }
.cell.run { color: var(--accent-hover); }
.cell.skip { color: var(--text-dim); }
.cell.blocked { color: var(--warn); }
.cell-txt { font-size: 11px; margin-left: 2px; }
.act-list { border: 1px solid var(--border); border-radius: 5px; max-height: 380px; overflow-y: auto; margin-bottom: 8px; }
.act-row { display: flex; align-items: center; gap: 10px; padding: 6px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
.act-row:last-child { border-bottom: none; }
.act-row.dim { opacity: .55; }
.msg { max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.warn-text { color: var(--warn); font-size: 12px; }
.ok-text { color: var(--ok); font-size: 12px; }
.modal-foot { display: flex; gap: 10px; justify-content: flex-end; margin-top: 12px; }
</style>
