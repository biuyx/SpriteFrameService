<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useStore, toast, askConfirm } from '../stores'
import api from '../api'

// 批量生成首帧：立绘 + 参考首帧集 → Seedream 生图（按张计费）
const props = defineProps({ actions: { type: Array, required: true } })
const emit = defineEmits(['close', 'done'])
const store = useStore()

const sets = ref([])
const setId = ref('')
const templatesById = ref({})
const refs = ref([])               // 精灵首帧图库（找立绘标记）
const checked = ref({})
const phase = ref('pick')
const rows = ref([])
const skippedRows = ref([])

const hasFront = computed(() => refs.value.some(r => r.role === 'front'))
const hasBack = computed(() => refs.value.some(r => r.role === 'back'))
const curSet = computed(() => sets.value.find(s => s.id === setId.value) || null)
const setKeys = computed(() => new Set((curSet.value?.frames || []).map(f => f.key)))

function actionKey(a) {
  return templatesById.value[a.template_id]?.key || a.name
}
const eligible = (a) => hasFront.value && setKeys.value.has(actionKey(a))

onMounted(async () => {
  try {
    const [s, t, r] = await Promise.all([
      api.ffsets(), api.templates(), api.spriteRefs(store.currentSprite.id),
    ])
    sets.value = s.sets
    for (const x of t.templates) templatesById.value[x.id] = x
    refs.value = r.refs
    if (sets.value.length) setId.value = sets.value[0].id
    resetChecks()
  } catch (e) { toast(`加载失败: ${e.message}`) }
})

function resetChecks() {
  for (const a of props.actions)
    checked.value[a.id] = eligible(a) && !a.summary?.has_first_frame
}

const selectedIds = computed(() =>
  props.actions.filter(a => checked.value[a.id]).map(a => a.id))

function selectMissing() { resetChecks() }
function selectNone() { for (const a of props.actions) checked.value[a.id] = false }

async function start() {
  const n = selectedIds.value.length
  if (!n) return toast('请至少勾选一个动作')
  const overwrite = props.actions.filter(a => checked.value[a.id] && a.summary?.has_first_frame).length
  const warn = overwrite ? `\n其中 ${overwrite} 个已有首帧，将被覆盖。` : ''
  if (!(await askConfirm(
    `用参考集「${curSet.value?.name}」为 ${n} 个动作生成首帧？\n生图按张计费（Seedream）。${warn}`))) return
  try {
    const r = await api.batchGenFirstFrames(store.currentSprite.id, {
      action_ids: selectedIds.value, set_id: setId.value,
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
function startPolling() { stopPolling(); timer = setInterval(poll, 2000); poll() }
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
      if (e.status === 404) { row.status = 'error'; row.error = '任务记录不存在（服务可能已重启）' }
    }
    if (!['done', 'error', 'cancelled'].includes(row.status)) active = true
  }
  if (!active && rows.value.length) {
    stopPolling()
    emit('done')
    const fail = rows.value.filter(r => r.status !== 'done').length
    toast(fail ? `首帧生成结束：${rows.value.length - fail} 成功 / ${fail} 失败` : '首帧全部生成完成')
  }
}

async function retry(row) {
  try {
    const r = await api.batchGenFirstFrames(store.currentSprite.id, {
      action_ids: [row.action_id], set_id: setId.value,
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
const STATUS_TXT = { queued: '排队', running: '生成中', done: '✓ 完成', error: '✗ 失败', cancelled: '已取消' }
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <div class="modal-head">
        <h3>批量生成首帧（{{ store.currentSprite?.name }}）</h3>
        <button class="small" @click="emit('close')">✕</button>
      </div>

      <template v-if="phase === 'pick'">
        <div class="row" style="gap:10px;align-items:center;margin-bottom:6px;flex-wrap:wrap">
          <div class="field inline"><label>参考首帧集</label>
            <select v-model="setId" @change="resetChecks">
              <option v-for="s in sets" :key="s.id" :value="s.id">
                {{ s.name }}（{{ s.frames.length }} 张{{ s.group ? ` · ${s.group}` : '' }}）</option>
            </select></div>
          <span :class="hasFront ? 'ok-text' : 'warn-text'">
            正面立绘 {{ hasFront ? '✓' : '未标记' }}</span>
          <span :class="hasBack ? 'ok-text' : 'hint'">
            背面立绘 {{ hasBack ? '✓' : '未标记（背面动作将用正面立绘代替）' }}</span>
        </div>
        <p v-if="!hasFront" class="warn-text" style="margin:0 0 6px;font-size:12px">
          请先在「首帧图库」上传立绘并点图片左下角标记「正面立绘」。</p>
        <p v-if="!sets.length" class="warn-text" style="margin:0 0 6px;font-size:12px">
          还没有参考首帧集——先到精灵库「参考首帧库」导入或从完成的精灵归档。</p>

        <div class="row" style="gap:8px;margin-bottom:6px">
          <button class="small" @click="selectMissing">选缺首帧的</button>
          <button class="small" @click="selectNone">全不选</button>
          <span class="hint">已勾选 {{ selectedIds.length }} 个</span>
        </div>
        <div class="act-list">
          <label v-for="a in props.actions" :key="a.id" class="act-row"
                 :class="{ disabled: !eligible(a) }">
            <input type="checkbox" v-model="checked[a.id]" :disabled="!eligible(a)" />
            <b>{{ a.name }}</b>
            <span class="hint">{{ actionKey(a) }}</span>
            <span class="spacer" style="flex:1"></span>
            <span v-if="!setKeys.has(actionKey(a))" class="warn-text">参考集缺此动作</span>
            <span v-else-if="a.summary?.has_first_frame" class="hint">已有首帧（将覆盖）</span>
            <span v-else class="warn-text">缺首帧</span>
          </label>
        </div>
        <div class="modal-foot">
          <button class="primary" :disabled="!selectedIds.length" @click="start">
            开始生成（{{ selectedIds.length }} 张）</button>
          <button @click="emit('close')">取消</button>
        </div>
      </template>

      <template v-else>
        <div class="prog-summary">
          完成 {{ doneCount }} / {{ rows.length }}
          <div class="prog-bar"><div class="prog-fill"
               :style="{ width: (doneCount / rows.length * 100) + '%' }"></div></div>
        </div>
        <div class="act-list">
          <div v-for="r in rows" :key="r.action_id" class="act-row">
            <b>{{ r.name }}</b>
            <span class="hint">{{ r.message }}</span>
            <span class="spacer" style="flex:1"></span>
            <span :class="{ 'ok-text': r.status === 'done', 'warn-text': r.status === 'error' }"
                  :title="r.error || ''">
              {{ STATUS_TXT[r.status] || r.status }}</span>
            <button v-if="r.status === 'error'" class="small" @click="retry(r)">重试</button>
          </div>
          <div v-for="s in skippedRows" :key="s.action_id" class="act-row disabled">
            <b>{{ s.name }}</b><span class="spacer" style="flex:1"></span>
            <span class="warn-text">跳过：{{ s.reason }}</span>
          </div>
        </div>
        <p class="hint" style="margin-top:6px">生成完成后到看板/工作台确认效果，不满意的可单独重新生成。</p>
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
  width: 620px; max-width: 94vw; max-height: 86vh; overflow-y: auto;
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 20px 18px;
}
.modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.modal-head h3 { margin: 0; font-size: 16px; }
.act-list {
  border: 1px solid var(--border); border-radius: 5px; max-height: 300px; overflow-y: auto;
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
.modal-foot { display: flex; gap: 10px; justify-content: flex-end; }
.prog-summary { font-size: 13px; margin-bottom: 10px; }
.prog-bar { height: 6px; background: var(--bg-input); border-radius: 3px; margin-top: 6px; overflow: hidden; }
.prog-fill { height: 100%; background: var(--accent); transition: width .4s; }
</style>
