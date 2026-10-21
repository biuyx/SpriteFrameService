<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import api from '../api'
import { askConfirm } from '../stores'
import { JOB_TYPES, filterJobs, jobStatus, canCancelJob } from '../jobManagement'

const emit = defineEmits(['close', 'jump'])
const data = ref({ jobs: [], counts: {}, active: 0, admission_paused: false, maintenance: false, accepting_jobs: true })
const filter = ref('active'), keyword = ref(''), type = ref('')
const busy = ref(false), loading = ref(false), error = ref(''), notice = ref(''), updated = ref('')
let timer
const rows = computed(() => filterJobs(data.value.jobs, filter.value, keyword.value, type.value))
const types = computed(() => [...new Set(data.value.jobs.map(j => j.type))].sort())
const tabs = computed(() => [
  ['active', '进行中 / 排队', data.value.active], ['all', '全部', Object.values(data.value.counts).reduce((n, v) => n + v, 0)],
  ['done', '已完成', data.value.counts.done || 0], ['error', '失败', data.value.counts.error || 0],
  ['cancelled', '已取消', data.value.counts.cancelled || 0],
])
async function load() {
  if (loading.value) return
  loading.value = true
  try {
    data.value = await api.manageJobs(2000)
    error.value = ''
    updated.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch (e) { error.value = `刷新失败：${e.message}。下方保留上次查询结果。` }
  finally { loading.value = false }
}
async function mutate(fn) {
  busy.value = true
  notice.value = ''
  try { notice.value = await fn(); await load() }
  catch (e) { error.value = `操作未确认：${e.message}。请刷新核查实际状态。` }
  finally { busy.value = false }
}
async function stopAll() {
  if (!await askConfirm('停止服务器上所有排队和运行中的任务？\n会暂停新任务提交，保留已完成素材；运行任务在可中断点退出。远端生成只能尝试取消，已发生的费用不保证退回。\n之后需手动点击「允许新任务」，已取消任务不会自动重做。', { danger: true })) return
  await mutate(async () => {
    const result = await api.stopAllJobs()
    return `已对 ${result.requested_count} 个任务发出停止请求；${result.queued_cancelled} 个排队任务已取消，仍有 ${result.stopping_count} 个任务正在退出。新任务提交已暂停，已完成素材保留。`
  })
}
async function toggleAdmission() {
  const paused = !data.value.admission_paused
  if (!await askConfirm(paused ? '暂停新任务提交？已有任务继续执行。' : '允许提交新的任务？已取消或中断的任务不会自动恢复。')) return
  await mutate(async () => {
    await api.setJobAdmission(paused)
    return paused ? '已暂停新任务提交。' : '已允许新任务提交，没有重新提交旧任务。'
  })
}
async function cancel(job) {
  if (!await askConfirm(`停止这个${JOB_TYPES[job.type] || job.type}任务？已完成素材保留，运行中的处理可能需要等待当前调用结束。`, { danger: true })) return
  await mutate(async () => { await api.cancelJob(job.id); return '已发出停止请求，请以实际任务状态为准。' })
}
function jump(job) {
  emit('jump', { spriteId: job.context.sprite_id, actionId: job.context.action_id })
}
function date(value) { return value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '—' }
onMounted(() => { load(); timer = setInterval(() => { if (!busy.value) load() }, 4000) })
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="global-jobs-mask" @click.self="emit('close')">
    <section class="global-jobs-dialog" role="dialog" aria-modal="true" aria-labelledby="global-jobs-title">
      <header><div><h2 id="global-jobs-title">全局任务管理</h2><p class="hint">服务器上的任务记录 · 刷新页面或更换浏览器后仍可查看</p></div><button @click="emit('close')">关闭</button></header>
      <div class="job-controls">
        <div><strong :class="data.accepting_jobs ? 'accepting' : 'paused'">{{ data.accepting_jobs ? '允许新任务' : '新任务已暂停' }}</strong><span v-if="data.maintenance" class="hint"> · 服务正在维护</span><p class="hint">停止任务会保留已有素材。恢复提交后，旧任务也不会自动重做。</p></div>
        <div class="control-buttons"><button :disabled="busy || loading || data.maintenance" @click="toggleAdmission">{{ data.admission_paused ? '允许新任务' : '暂停新任务' }}</button><button class="danger-stop" :disabled="busy || loading || !data.active" @click="stopAll">停止全部任务<span v-if="data.active">（{{ data.active }}）</span></button></div>
      </div>
      <p v-if="error" class="job-error" role="alert">{{ error }}</p>
      <p v-if="notice" class="job-notice" role="status">{{ notice }}</p>
      <nav class="job-tabs" aria-label="任务状态"><button v-for="[key,label,count] in tabs" :key="key" :class="{ primary: filter === key }" @click="filter = key">{{ label }} <span>{{ count }}</span></button></nav>
      <div class="job-filters"><input v-model="keyword" aria-label="搜索任务" placeholder="搜索角色、动作或任务编号" /><select v-model="type" aria-label="任务类型"><option value="">全部类型</option><option v-for="t in types" :key="t" :value="t">{{ JOB_TYPES[t] || t }}</option></select><button :disabled="busy || loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button><span class="hint">更新于 {{ updated || '—' }}</span></div>
      <div class="global-job-table"><table><thead><tr><th>角色 / 动作</th><th>任务</th><th>状态与进度</th><th>创建时间</th><th>操作</th></tr></thead><tbody>
        <tr v-for="job in rows" :key="job.id">
          <td><template v-if="job.context?.sprite_id"><strong>{{ job.context.sprite_name }}</strong><div>{{ job.context.action_name }}</div></template><span v-else class="hint">历史记录未保存动作归属</span></td>
          <td>{{ JOB_TYPES[job.type] || job.type }}<code>{{ job.id }}</code><small class="compact-date">{{ date(job.created_at) }}</small></td>
          <td class="job-progress"><span :class="{ 'job-error-text': job.status === 'error' }">{{ jobStatus(job) }}</span><span v-if="['running','queued'].includes(job.status)"> · {{ Math.round(job.progress || 0) }}%</span><div v-if="['running','queued'].includes(job.status)" class="bar"><div :style="{width: `${Math.min(100,Math.max(0,job.progress || 0))}%`}"></div></div><p :class="{ 'job-error-text': job.error }">{{ job.error || job.message || '—' }}</p></td>
          <td class="job-date">{{ date(job.created_at) }}</td>
          <td><div class="row-actions"><button v-if="job.context?.sprite_id" class="small" @click="jump(job)">打开动作</button><button v-if="canCancelJob(job)" class="small" :disabled="busy" @click="cancel(job)">停止</button></div></td>
        </tr>
        <tr v-if="!rows.length"><td colspan="5" class="job-empty">{{ loading && !updated ? '正在加载任务…' : '没有符合条件的任务' }}</td></tr>
      </tbody></table></div>
      <footer class="hint">展示最近 {{ data.limit || 2000 }} 条记录，优先显示未完成任务；「停止全部」作用于服务器所有未完成任务。远端生成取消不保证退款。</footer>
    </section>
  </div>
</template>

<style scoped>
.global-jobs-mask{position:fixed;inset:0;z-index:110;background:#000a;display:flex;align-items:center;justify-content:center;padding:20px}.global-jobs-dialog{width:min(1240px,97vw);max-height:92vh;display:flex;flex-direction:column;background:var(--bg-panel);border:1px solid var(--border);border-radius:12px;padding:24px;box-shadow:0 20px 80px #0006;min-height:440px}header{display:flex;justify-content:space-between;align-items:flex-start;gap:20px}h2{margin:0;font-size:21px}header p{margin:8px 0 18px}.job-controls{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px;background:var(--bg);border:1px solid var(--border);border-radius:8px}.job-controls p{margin:7px 0 0}.control-buttons,.job-filters,.job-tabs,.row-actions{display:flex;align-items:center;gap:8px}.control-buttons{flex-shrink:0;flex-wrap:wrap}.danger-stop{background:#a83939;color:#fff;border-color:#bf5555}.danger-stop:hover{background:#bb4444}.accepting{color:var(--ok)}.paused{color:#eab66a}.job-tabs{flex-wrap:wrap;margin:18px 0 12px}.job-tabs button span{opacity:.7;margin-left:5px}.job-filters{flex-wrap:wrap;margin-bottom:14px}.job-filters input{flex:1;min-width:180px}.global-job-table{overflow:auto;flex:1;min-height:160px}table{width:100%;border-collapse:collapse;font-size:13px}th{text-align:left;color:var(--text-dim);font-weight:500;background:var(--bg-panel);position:sticky;top:0;z-index:1}td,th{padding:12px;border-bottom:1px solid var(--border);vertical-align:top}td:first-child{min-width:150px}td:first-child div{color:var(--text-dim);margin-top:5px}code{display:block;color:var(--text-dim);font-size:11px;margin-top:6px}.job-progress{min-width:240px;max-width:420px}.job-progress p{white-space:pre-wrap;overflow-wrap:anywhere;color:var(--text-dim);font-size:12px;line-height:1.5;margin:6px 0 0}.job-date{min-width:155px;font-size:12px;color:var(--text-dim)}.row-actions{flex-wrap:wrap}.row-actions button{white-space:nowrap}.compact-date{display:none}.job-empty{text-align:center;padding:45px;color:var(--text-dim)}.job-error{background:#702b2b44;border:1px solid #b75555;padding:10px;border-radius:6px;color:var(--err)}.job-notice{background:#365f4344;border:1px solid #527b5e;padding:10px;border-radius:6px}.job-error-text{color:var(--err)!important}footer{padding-top:14px;line-height:1.6}@media(max-width:900px){th:nth-child(4),td:nth-child(4){display:none}.compact-date{display:block;color:var(--text-dim);margin-top:5px;font-size:11px}td:first-child{min-width:110px}.job-progress{min-width:200px}}@media(max-width:760px){.global-jobs-mask{padding:8px}.global-jobs-dialog{padding:16px}.job-controls{align-items:flex-start;flex-direction:column}.control-buttons{width:100%}.job-filters .hint{width:100%}}
</style>
