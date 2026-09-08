<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useStore, toast, askConfirm } from '../stores'
import api from '../api'

const props = defineProps({
  actions: { type: Array, required: true },
  preselected: { type: Array, default: null },   // 看板多选带入
})
const emit = defineEmits(['close', 'done'])
const store = useStore()

const templatesById = ref({})
const checked = ref({})
const phase = ref('pick')          // pick | running
const rows = ref([])
const skippedRows = ref([])

// 可抽 = 有视频素材 + 绑定的模板有抽帧规则（后端会再按 take 的模板精确判定）
function rule(a) {
  return templatesById.value[a.template_id]?.extract_rule || null
}
const eligible = (a) => a.summary?.has_video && !!rule(a)

onMounted(async () => {
  try {
    const t = await api.templates()
    for (const x of t.templates) templatesById.value[x.id] = x
  } catch (e) { toast(`加载模板失败: ${e.message}`) }
  const pre = props.preselected ? new Set(props.preselected) : null
  for (const a of props.actions) {
    checked.value[a.id] = pre ? (pre.has(a.id) && eligible(a)) : (eligible(a) && !a.summary?.frame_count)
  }
})

function ruleLabel(a) {
  const r = rule(a)
  if (!r) return ''
  return `${r.start}–${r.end}s @${r.fps}${r.keep ? ` · 留${r.keep.length}/${r.total}帧` : ''}`
}

const selectedIds = computed(() =>
  props.actions.filter(a => checked.value[a.id]).map(a => a.id))

function selectUnextracted() {
  for (const a of props.actions)
    checked.value[a.id] = eligible(a) && !a.summary?.frame_count
}
function selectNone() { for (const a of props.actions) checked.value[a.id] = false }

async function start() {
  const n = selectedIds.value.length
  if (!n) return toast('请至少勾选一个动作')
  const reextract = props.actions.filter(a => checked.value[a.id] && a.summary?.frame_count)
  const warn = reextract.length ? `\n其中 ${reextract.length} 个已有帧，将清空重抽。` : ''
  if (!(await askConfirm(`按各自模板的抽帧规则，为 ${n} 个动作批量抽帧？${warn}`))) return
  try {
    const r = await api.batchExtract(store.currentSprite.id, selectedIds.value)
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
  timer = setInterval(poll, 2000)
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
    toast(fail ? `批量抽帧结束：${rows.value.length - fail} 成功 / ${fail} 失败` : '批量抽帧全部完成')
  }
}

const doneCount = computed(() => rows.value.filter(r => r.status === 'done').length)
const STATUS_TXT = { queued: '排队', running: '抽帧中', done: '✓ 完成', error: '✗ 失败', cancelled: '已取消' }
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <div class="modal-head">
        <h3>批量抽帧（{{ store.currentSprite?.name }}）</h3>
        <button class="small" @click="emit('close')">✕</button>
      </div>

      <template v-if="phase === 'pick'">
        <p class="hint" style="margin:0 0 8px">
          按各自模板保存的参考抽帧规则执行；没有规则的模板先在任一角色的工作台里调好参数并「保存为模板规则」。</p>
        <div class="row" style="gap:8px;margin-bottom:6px">
          <button class="small" @click="selectUnextracted">选未抽帧的</button>
          <button class="small" @click="selectNone">全不选</button>
          <span class="hint">已勾选 {{ selectedIds.length }} 个</span>
        </div>
        <div class="act-list">
          <label v-for="a in props.actions" :key="a.id" class="act-row"
                 :class="{ disabled: !eligible(a) }">
            <input type="checkbox" v-model="checked[a.id]" :disabled="!eligible(a)" />
            <b>{{ a.name }}</b>
            <span class="hint">{{ ruleLabel(a) }}</span>
            <span class="spacer" style="flex:1"></span>
            <span v-if="!a.summary?.has_video" class="warn-text">无视频</span>
            <span v-else-if="!rule(a)" class="warn-text">模板无规则</span>
            <span v-else-if="a.summary?.frame_count" class="ok-text">已有 {{ a.summary.frame_count }} 帧</span>
          </label>
        </div>
        <div class="modal-foot">
          <button class="primary" :disabled="!selectedIds.length" @click="start">
            开始抽帧（{{ selectedIds.length }} 个）</button>
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
            <span class="hint">{{ r.rule.template }} · {{ r.rule.start }}–{{ r.rule.end }}s @{{ r.rule.fps
              }}{{ r.rule.keep_count ? ` · 留${r.rule.keep_count}/${r.rule.total}` : '' }}</span>
            <span class="spacer" style="flex:1"></span>
            <span :class="{ 'ok-text': r.status === 'done', 'warn-text': r.status === 'error' }">
              {{ STATUS_TXT[r.status] || r.status }}
              <template v-if="r.status === 'running'"> {{ Math.round(r.progress) }}%</template>
            </span>
          </div>
          <div v-for="s in skippedRows" :key="s.action_id" class="act-row disabled">
            <b>{{ s.name }}</b><span class="spacer" style="flex:1"></span>
            <span class="warn-text">跳过：{{ s.reason }}</span>
          </div>
        </div>
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
.modal-foot { display: flex; gap: 10px; justify-content: flex-end; }
.prog-summary { font-size: 13px; margin-bottom: 10px; }
.prog-bar { height: 6px; background: var(--bg-input); border-radius: 3px; margin-top: 6px; overflow: hidden; }
.prog-fill { height: 100%; background: var(--accent); transition: width .4s; }
</style>
