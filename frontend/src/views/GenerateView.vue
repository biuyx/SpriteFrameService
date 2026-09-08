<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useStore, refreshSession, probeFirstFrame, loadTakes, toast, askConfirm } from '../stores'
import { startJob } from '../jobs'
import { currentTab } from '../nav'
import api from '../api'
import TemplateLibraryModal from '../components/TemplateLibraryModal.vue'

// 工序 2：视频生成——首帧 + 参考视频 + 提示词 → Seedance；素材版本管理；或直接上传视频
const store = useStore()

// ---- 生成参数 ----
const gen = ref(null)              // /generate/capabilities 结果
const genModel = ref('')
const genPrompt = ref('')
const genRes = ref('480p')
const genRatio = ref('adaptive')
const genDuration = ref(4)
const genSeed = ref('')
const genFirstFrame = ref('action')   // action(首帧参考图) | frame(当前第N帧)
const genFrameIndex = ref(0)
const genBusy = ref(false)
const takes = computed(() => store.takes)

// 生成期间定时刷新版本墙（提交与 take 落盘之间有竞态，且要看到“生成中”状态）
let takesTimer = null
function startTakesPolling() { stopTakesPolling(); takesTimer = setInterval(loadTakes, 3000) }
function stopTakesPolling() { if (takesTimer) { clearInterval(takesTimer); takesTimer = null } }
onUnmounted(stopTakesPolling)

// ---- 参考视频 / 模板 ----
const rvAvailable = ref(false)        // 是否已上传本地参考视频
const rvUse = ref(false)              // 本次生成是否使用参考视频
const rvInput = ref(null)
const rvPreview = ref(false)
const tplAll = ref([])
const tplVariants = ref([])
const tplSelected = ref('')
const tplPreview = ref(false)
const tplLibOpen = ref(false)
const tplOthers = computed(() => tplAll.value.filter((t) => !tplVariants.value.some((v) => v.id === t.id)))

function tplOptLabel(t) {
  const parts = [t.key]
  if (t.variant) parts.push(t.variant)
  return parts.join(' · ') + (t.duration_hint ? `（${t.duration_hint}s）` : '')
}

async function loadTemplates() {
  tplVariants.value = []
  tplAll.value = []
  try {
    const r = await api.templates()
    tplAll.value = r.templates
    const key = (store.currentAction?.name || '').trim()
    let matching = r.templates.filter((t) => t.key === key)
    const boundTpl = r.templates.find((t) => t.id === store.currentAction?.template_id)
    if (!matching.length && boundTpl) matching = r.templates.filter((t) => t.key === boundTpl.key)
    tplVariants.value = matching
    const bound = store.currentAction?.template_id
    if (bound && tplAll.value.some((t) => t.id === bound)) tplSelected.value = bound
    else if (tplVariants.value.length) tplSelected.value = tplVariants.value[0].id
    else tplSelected.value = ''
    if (tplSelected.value) rvUse.value = true
    applyTplDuration()
  } catch { /* ignore */ }
}

function applyTplDuration() {
  const t = tplAll.value.find((x) => x.id === tplSelected.value)
  if (t?.duration_hint && gen.value?.params?.duration?.includes(t.duration_hint)) genDuration.value = t.duration_hint
}

async function onRefVideoFile(file) {
  if (!file) return
  try {
    const r = await api.uploadReferenceVideo(store.sessionId, file)
    rvAvailable.value = true
    rvUse.value = true
    toast(`参考视频已设置（${(r.bytes / 1048576).toFixed(1)}MB）`)
  } catch (e) {
    toast(`上传失败: ${e.message}`)
  } finally {
    if (rvInput.value) rvInput.value.value = ''
  }
}

async function clearRefVideo() {
  if (!(await askConfirm('清除参考视频？'))) return
  await api.deleteReferenceVideo(store.sessionId)
  rvAvailable.value = false
  rvUse.value = false
  toast('已清除')
}

// ---- 提示词库：解析 / 切换 / 沉淀 ----
const SOURCE_TXT = { action: '动作记忆', template: '模板绑定', key: 'key 绑定',
                     group: '分组绑定', global: '全局默认', builtin: '内置', manual: '手动选用' }
const promptMatch = ref(null)
const promptLib = ref([])
const promptSel = ref('')
const promptDirty = ref(false)
const remember = ref(true)
const videoScope = computed(() =>
  rvUse.value && (tplSelected.value || rvAvailable.value) ? 'video_ref' : 'video_i2v')

function libText(rec) {
  const v = rec.versions.find((x) => x.v === rec.current) || rec.versions[rec.versions.length - 1]
  return (v?.text || '').replace('{action}', store.currentAction?.name || '')
}

async function resolvePromptFor() {
  try {
    const [r, lib] = await Promise.all([
      api.resolvePrompt(videoScope.value, store.currentSprite.id, store.sessionId),
      api.prompts(videoScope.value),
    ])
    genPrompt.value = r.text
    promptMatch.value = r
    promptSel.value = r.prompt_id || ''
    promptLib.value = lib.prompts
    promptDirty.value = false
  } catch { /* 库不可用时保留现有文本 */ }
}

function pickLibraryPrompt() {
  const rec = promptLib.value.find((x) => x.id === promptSel.value)
  if (!rec) return
  genPrompt.value = libText(rec)
  promptMatch.value = { prompt_id: rec.id, version: rec.current, name: rec.name, source: 'manual' }
  promptDirty.value = false
}

async function applyTemplate() {
  promptDirty.value = false
  await resolvePromptFor()
  toast(`已按提示词库重新匹配：${promptMatch.value?.name || ''}`)
}

async function saveAsVersion() {
  const id = promptMatch.value?.prompt_id
  if (!id) return
  const note = await askConfirm(`把当前文本保存为「${promptMatch.value.name}」的新版本？`,
                                { input: { placeholder: '版本说明（可空）', initial: '' } })
  if (note === null) return
  try {
    const rec = await api.addPromptVersion(id, genPrompt.value.trim(), note)
    promptMatch.value = { ...promptMatch.value, version: rec.current, source: 'manual' }
    promptDirty.value = false
    promptLib.value = (await api.prompts(videoScope.value)).prompts
    toast(`已保存为 v${rec.current}`)
  } catch (e) { toast(`保存失败: ${e.message}`) }
}

async function saveAsNew() {
  const name = await askConfirm('另存为新的库提示词，名称：', { input: { placeholder: '如：走路·参考视频', initial: '' } })
  if (!name) return
  const tpl = tplAll.value.find((x) => x.id === tplSelected.value)
  const bindings = []
  if (tpl && rvUse.value && await askConfirm(`绑定到当前模板「${tpl.variant || tpl.key}」？（同模板的动作自动匹配）`)) {
    bindings.push({ level: 'template', value: tpl.id })
  }
  try {
    const rec = await api.createPrompt({ scope: videoScope.value, name, text: genPrompt.value.trim(), bindings })
    promptMatch.value = { prompt_id: rec.id, version: rec.current, name: rec.name, source: 'manual' }
    promptSel.value = rec.id
    promptDirty.value = false
    promptLib.value = (await api.prompts(videoScope.value)).prompts
    toast(`已入库「${rec.name}」`)
  } catch (e) { toast(`保存失败: ${e.message}`) }
}

// 作用域或模板选择变化时，只要用户没手改过就重新解析
watch([videoScope, tplSelected], () => { if (!promptDirty.value) resolvePromptFor() })

// ---- 初始化 ----
async function loadGen() {
  try {
    gen.value = await api.genCapabilities()
    const d = gen.value.defaults || {}
    if (!genModel.value) genModel.value = gen.value.default_model
    genRes.value = genRes.value || d.resolution || '480p'
  } catch { gen.value = null }
  await loadTemplates()
  // 动作记忆：上次生成的参数与模板优先于默认值
  const mem = store.currentAction?.gen_prefs?.video
  if (mem) {
    if (mem.model && (gen.value?.models || []).some((m) => m.id === mem.model)) genModel.value = mem.model
    if (mem.resolution) genRes.value = mem.resolution
    if (mem.ratio) genRatio.value = mem.ratio
    if (mem.duration && gen.value?.params?.duration?.includes(mem.duration)) genDuration.value = mem.duration
    if (mem.template_id && tplAll.value.some((t) => t.id === mem.template_id)) {
      tplSelected.value = mem.template_id
      rvUse.value = true
    }
  }
  if (gen.value && !genPrompt.value.trim()) await resolvePromptFor()
  await probeFirstFrame()
  try {
    const r = await fetch(api.referenceVideoUrl(store.sessionId, Date.now()),
                          { method: 'GET', headers: { Range: 'bytes=0-0' }, credentials: 'same-origin' })
    rvAvailable.value = r.ok
    if (!r.ok && !tplSelected.value) rvUse.value = false
  } catch { rvAvailable.value = false }
  await loadTakes()
}

const canGenerate = computed(() =>
  genFirstFrame.value === 'action' ? store.firstFrame.available : store.frameCount > 0)

// ---- 生成 ----
async function runGenerate() {
  const prompt = genPrompt.value.trim()
  if (!prompt) return toast('请填写提示词')
  if (!canGenerate.value) return toast('请先准备角色首帧参考图（工序 1）')
  if (!(await askConfirm('提交生成任务？（每次生成计费）'))) return
  genBusy.value = true
  try {
    const params = { resolution: genRes.value, ratio: genRatio.value, duration: genDuration.value }
    if (genSeed.value !== '' && !isNaN(+genSeed.value)) params.seed = +genSeed.value
    const first_frame = genFirstFrame.value === 'action' ? { kind: 'action' }
      : { kind: 'frame', frame_index: genFrameIndex.value }
    startTakesPolling()
    const pm = promptDirty.value ? {} : (promptMatch.value || {})
    await startJob(() => api.generate(store.sessionId,
      { prompt, model: genModel.value, params, first_frame,
        template_id: rvUse.value && tplSelected.value ? tplSelected.value : null,
        use_reference_video: rvUse.value && !tplSelected.value && rvAvailable.value,
        prompt_id: pm.prompt_id || null, prompt_version: pm.version || null,
        prompt_name: pm.name || null, remember: remember.value }), {
      title: 'AI 生成视频',
      onDone: async () => {
        stopTakesPolling()
        await loadTakes()
        await refreshSession()
        store.videoVersion++
        toast('生成完成，视频已就绪，可进入「抽帧」')
      },
      onError: async () => {
        stopTakesPolling()
        await loadTakes()
      },
    })
  } catch (e) {
    toast(`生成失败: ${e.message}`)
  } finally {
    genBusy.value = false
  }
}

// ---- 素材版本 ----
const previewTake = ref(null)
const previewErr = ref(false)
function openPreview(t) { previewErr.value = false; previewTake.value = t }

async function useTake(t) {
  const r = await api.selectTake(store.sessionId, t.id)
  store.videoInfo = r.video_info
  store.videoVersion++
  await loadTakes()
  toast('已切换到该版本')
}

async function removeTake(t) {
  const warn = t.source === 'generate'
    ? `删除生成的版本 ${t.id}？该视频是付费生成的，删除后需重新付费生成。`
    : `删除版本 ${t.id}？`
  if (!(await askConfirm(warn, { danger: true }))) return
  await api.deleteTake(store.sessionId, t.id)
  await loadTakes()
  await refreshSession()
  store.videoVersion++
}

function takeLabel(t) {
  const p = []
  if (t.source === 'generate') {
    if (t.resolution) p.push(t.resolution)
    if (t.actual_duration) p.push(`${t.actual_duration}s`)
    if (t.seed != null) p.push(`seed ${t.seed}`)
  } else {
    p.push(t.filename || '上传')
  }
  return p.join(' · ')
}

function fmtTime(ts) {
  const d = new Date(ts * 1000)
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

// ---- 生成结果 × 参考视频 对比 ----
const compareTake = ref(null)
const cmpRefEl = ref(null)
const cmpGenEl = ref(null)
const cmpRefErr = ref(false)
const cmpGenErr = ref(false)
const cmpPaused = ref(false)
function canCompare(t) { return t.status === 'succeeded' && (t.template_id || t.used_reference_video) }
function openCompare(t) { cmpRefErr.value = false; cmpGenErr.value = false; cmpPaused.value = false; compareTake.value = t }
function cmpRefUrl(t) {
  return t.template_id ? api.templateVideoUrl(t.template_id) : api.referenceVideoUrl(store.sessionId, Date.now())
}
function cmpRefLabel(t) {
  if (!t.template_id) return '本地参考视频'
  const tpl = tplAll.value.find((x) => x.id === t.template_id)
  return tpl ? `模板 · ${tpl.variant || tpl.key}` : '模板（已从库中删除）'
}
function cmpEach(fn) { for (const v of [cmpRefEl.value, cmpGenEl.value]) if (v) fn(v) }
function cmpToggle() { cmpPaused.value = !cmpPaused.value; cmpEach((v) => (cmpPaused.value ? v.pause() : v.play().catch(() => {}))) }
function cmpRestart() { cmpPaused.value = false; cmpEach((v) => { v.currentTime = 0; v.play().catch(() => {}) }) }

// ---- 上传视频（替代生成的素材来源） ----
const dragOver = ref(false)
const uploading = ref(false)
const fileInput = ref(null)
async function uploadFile(file) {
  if (!file || !store.sessionId) return
  uploading.value = true
  try {
    const res = await api.uploadVideo(store.sessionId, file)
    store.videoInfo = res.video_info
    store.videoVersion++
    await loadTakes()
    await refreshSession()
    toast('视频上传成功，可进入「抽帧」')
  } catch (e) {
    toast(`上传失败: ${e.message}`)
  } finally {
    uploading.value = false
    if (fileInput.value) fileInput.value.value = ''
  }
}

onMounted(async () => {
  await refreshSession()
  await loadGen()
  // 有未完结的远端生成任务时自动重挂（重启后取回结果）
  const running = (store.takes.takes || []).some(
    (t) => t.source === 'generate' && (t.status === 'running' || t.status === 'pending'))
  if (running && gen.value?.configured) {
    await startJob(() => api.reconcileTakes(store.sessionId), {
      title: '恢复生成任务',
      onDone: async () => { await loadTakes(); await refreshSession(); store.videoVersion++ },
    })
  }
})
</script>

<template>
  <div class="panel">
    <div class="section-title"><h2>2. 视频生成</h2>
      <span class="hint">首帧 + 参考视频 + 提示词 → Seedance；也可直接上传视频作为素材</span></div>

    <!-- 就绪提示 -->
    <div v-if="!store.firstFrame.available" class="ready-bar warn">
      首帧参考图未就绪——生成必须有首帧。
      <button class="small" @click="currentTab = 'firstframe'">← 去首帧</button>
    </div>

    <div class="gen-box" :class="{ disabled: !gen?.configured }">
      <div class="row" style="align-items:center">
        <b style="font-size:13px">AI 生成视频（Seedance）</b>
        <span v-if="gen && !gen.configured" class="hint warn-text">
          未配置 API Key——在「⚙ 设置」里填写 Ark 密钥即可启用</span>
      </div>
      <template v-if="gen?.configured">
        <div class="row ff-row">
          <div class="ff-preview" title="首帧参考图（在工序 1 管理）" @click="currentTab = 'firstframe'">
            <img v-if="store.firstFrame.available" :src="api.firstFrameUrl(store.sessionId, store.firstFrame.version)" alt="" />
            <span v-else class="ff-empty">无首帧</span>
          </div>
          <div style="flex:1">
            <div class="row" style="margin-bottom:6px">
              <div class="field inline"><label>首帧来源</label>
                <select v-model="genFirstFrame">
                  <option value="action">首帧参考图{{ store.firstFrame.available ? '' : '（未就绪）' }}</option>
                  <option value="frame" :disabled="!store.frameCount">当前第 N 帧</option>
                </select>
              </div>
              <div v-if="genFirstFrame === 'frame'" class="field inline">
                <label>帧</label><input type="number" v-model.number="genFrameIndex" :min="0" :max="store.frameCount - 1" style="width:70px" />
              </div>
              <span v-if="!canGenerate" class="warn-text">生成必须提供角色首帧参考图</span>
            </div>
            <div class="row prompt-bar">
              <span class="hint">提示词：</span>
              <span v-if="promptMatch" class="rule-chip" :title="'匹配来源：' + (SOURCE_TXT[promptMatch.source] || promptMatch.source)">
                {{ promptMatch.name }}<template v-if="promptMatch.version"> v{{ promptMatch.version }}</template>
                · {{ SOURCE_TXT[promptMatch.source] || promptMatch.source }}</span>
              <span v-if="promptDirty" class="warn-text">已手改（未保存）</span>
              <select v-model="promptSel" style="max-width:200px" title="从提示词库选用" @change="pickLibraryPrompt">
                <option value="">— 从库选用 —</option>
                <option v-for="p in promptLib" :key="p.id" :value="p.id">{{ p.name }} v{{ p.current }}</option>
              </select>
              <button class="small" title="按提示词库重新匹配（丢弃手改）" @click="applyTemplate">重新匹配</button>
              <button v-if="promptMatch?.prompt_id" class="small" :disabled="!promptDirty"
                      title="把当前文本存为该库条目的新版本" @click="saveAsVersion">保存为新版本</button>
              <button class="small" title="另存为新的库提示词（可绑定当前模板）" @click="saveAsNew">另存入库</button>
            </div>
            <textarea v-model="genPrompt" rows="2" style="width:100%;resize:vertical"
                      placeholder="提示词" @input="promptDirty = true"></textarea>
          </div>
        </div>
        <div class="row">
          <div class="field inline"><label>模型</label>
            <select v-model="genModel">
              <option v-for="m in gen.models" :key="m.id" :value="m.id">{{ m.label }}</option>
            </select>
          </div>
          <div class="field inline"><label>分辨率</label>
            <select v-model="genRes"><option v-for="r in gen.params.resolution" :key="r">{{ r }}</option></select>
          </div>
          <div class="field inline"><label>比例</label>
            <select v-model="genRatio"><option v-for="r in gen.params.ratio" :key="r">{{ r }}</option></select>
          </div>
          <div class="field inline"><label>时长(s)</label>
            <select v-model.number="genDuration"><option v-for="d in gen.params.duration" :key="d" :value="d">{{ d }}</option></select>
          </div>
          <div class="field inline"><label>seed</label>
            <input v-model="genSeed" placeholder="留空随机" style="width:90px" /></div>
          <button class="primary" :disabled="genBusy || !canGenerate" @click="runGenerate">
            {{ genBusy ? '生成中...' : '生成' }}</button>
          <label style="display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer"
                 title="把本次提示词与参数记为此动作的设定：下次打开与批量生成优先沿用">
            <input type="checkbox" v-model="remember" /> 记住为此动作设定</label>
        </div>
        <div class="row" style="align-items:center">
          <span class="hint">参考视频（可选，动作/镜头节奏参考）：</span>
          <template v-if="tplAll.length">
            <label style="display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer">
              <input type="checkbox" v-model="rvUse" /> 使用模板</label>
            <select v-model="tplSelected" style="max-width:210px" @change="applyTplDuration()">
              <optgroup v-if="tplVariants.length" label="推荐（匹配动作名）">
                <option v-for="t in tplVariants" :key="t.id" :value="t.id">
                  {{ t.variant || t.key }}{{ t.duration_hint ? `（${t.duration_hint}s）` : '' }}</option>
              </optgroup>
              <optgroup v-if="tplOthers.length" label="参考视频库">
                <option v-for="t in tplOthers" :key="t.id" :value="t.id">{{ tplOptLabel(t) }}</option>
              </optgroup>
              <option value="">（不使用 / 本地上传）</option>
            </select>
            <button v-if="tplSelected" class="small" @click="tplPreview = true">预览模板</button>
          </template>
          <template v-if="rvAvailable && !tplSelected">
            <label style="display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer">
              <input type="checkbox" v-model="rvUse" /> 本次生成使用</label>
            <button class="small" @click="rvPreview = true">预览</button>
            <button class="small" @click="rvInput.click()">更换</button>
            <button class="small danger" @click="clearRefVideo">清除</button>
          </template>
          <button v-if="!rvAvailable && !tplSelected" class="small" @click="rvInput.click()">上传参考视频</button>
          <button class="small" title="管理参考视频库（上传/OSS/编辑）" @click="tplLibOpen = true">管理库</button>
          <input ref="rvInput" type="file" accept="video/*" style="display:none"
                 @change="e => onRefVideoFile(e.target.files[0])" />
        </div>
        <p v-if="rvUse && rvAvailable" class="hint" style="margin:2px 0 0">
          已启用参考视频：角色形象仍以首帧为准，视频仅提供动作与镜头节奏。</p>
        <p class="hint" style="margin:4px 0 0">默认 Mini 模型 + 480p + 4s；生成约需数分钟，可切到其他工序继续工作。</p>
      </template>
    </div>

    <!-- 素材版本 -->
    <div class="section-title" style="margin-top:14px"><h2>素材版本</h2>
      <span class="hint" v-if="takes.takes?.length">「用这个」的版本作为抽帧素材</span></div>
    <div v-if="!takes.takes?.length" class="hint" style="padding:10px 0">还没有素材版本——生成或上传视频后出现在这里。</div>
    <div v-else class="take-list">
      <div v-for="t in [...takes.takes].reverse()" :key="t.id" class="take-row"
           :class="{ current: takes.current === t.id }">
        <span class="take-badge" :class="t.status">
          {{ t.status === 'succeeded' ? (t.source === 'generate' ? '生成' : '上传')
             : t.status === 'running' ? '生成中' : t.status === 'failed' ? '失败' : t.status }}</span>
        <span class="take-name">{{ takeLabel(t) }}</span>
        <span class="hint">{{ fmtTime(t.created_at) }}</span>
        <span v-if="t.prompt_id" class="take-badge" :title="t.prompt">
          {{ t.prompt_name || '提示词' }}<template v-if="t.prompt_version"> v{{ t.prompt_version }}</template></span>
        <span v-else-if="t.prompt" class="hint take-prompt" :title="t.prompt">{{ t.prompt.slice(0, 40) }}…</span>
        <span v-if="t.error" class="warn-text" :title="t.error">{{ t.error.slice(0, 50) }}</span>
        <span class="spacer" style="flex:1"></span>
        <span v-if="takes.current === t.id" class="ok-text" style="font-size:12px">✓ 当前使用</span>
        <button v-else-if="t.status === 'succeeded'" class="small" @click="useTake(t)">用这个</button>
        <button v-if="t.status === 'succeeded'" class="small" @click="openPreview(t)">预览</button>
        <button v-if="canCompare(t)" class="small" title="与参考视频并排对比" @click="openCompare(t)">对比</button>
        <button class="small danger" @click="removeTake(t)">删除</button>
      </div>
    </div>
    <div v-if="store.videoInfo" style="margin-top:10px">
      <button class="primary" @click="currentTab = 'extract'">下一步：抽帧 →</button>
    </div>

    <!-- 上传视频 -->
    <div class="section-title" style="margin-top:16px"><h2>或：上传视频</h2></div>
    <div class="upload-drop" :class="{ dragover: dragOver }"
         @dragover.prevent="dragOver = true" @dragleave="dragOver = false"
         @drop.prevent="dragOver = false; uploadFile($event.dataTransfer.files[0])"
         @click="fileInput.click()">
      <div v-if="!uploading">点击或拖拽视频文件到此处<br><span class="hint">支持 mp4 / mov / avi / mkv / webm 等；上传后成为一个素材版本</span></div>
      <div v-else>上传中...</div>
      <input ref="fileInput" type="file" accept="video/*" style="display:none" @change="e => uploadFile(e.target.files[0])" />
    </div>

    <TemplateLibraryModal v-if="tplLibOpen" @close="tplLibOpen = false" @changed="loadTemplates" />

    <div v-if="tplPreview" class="tk-mask" @click.self="tplPreview = false">
      <div class="tk-box">
        <div class="tk-head"><b>动作模板预览</b><span class="spacer" style="flex:1"></span>
          <button class="small" @click="tplPreview = false">✕ 关闭</button></div>
        <video :src="api.templateVideoUrl(tplSelected)" controls autoplay loop style="width:100%;max-height:60vh;background:#000"></video>
      </div>
    </div>

    <div v-if="rvPreview" class="tk-mask" @click.self="rvPreview = false">
      <div class="tk-box">
        <div class="tk-head"><b>参考视频</b><span class="spacer" style="flex:1"></span>
          <button class="small" @click="rvPreview = false">✕ 关闭</button></div>
        <video :src="api.referenceVideoUrl(store.sessionId, Date.now())" controls autoplay loop style="width:100%;max-height:60vh;background:#000"></video>
      </div>
    </div>

    <div v-if="compareTake" class="tk-mask" @click.self="compareTake = null">
      <div class="cmp-box">
        <div class="tk-head">
          <b>对比：{{ takeLabel(compareTake) || compareTake.id }}</b>
          <button class="small" @click="cmpToggle">{{ cmpPaused ? '▶ 播放' : '⏸ 暂停' }}</button>
          <button class="small" @click="cmpRestart">⟲ 同步重播</button>
          <span class="spacer" style="flex:1"></span>
          <button class="small" @click="compareTake = null">✕ 关闭</button>
        </div>
        <div class="cmp-grid">
          <div class="cmp-cell">
            <div class="cmp-label">{{ cmpRefLabel(compareTake) }}</div>
            <video v-if="!cmpRefErr" ref="cmpRefEl" :src="cmpRefUrl(compareTake)" autoplay loop muted @error="cmpRefErr = true"></video>
            <div v-else class="cmp-missing">参考视频不可用（可能已被删除或替换）</div>
          </div>
          <div class="cmp-cell">
            <div class="cmp-label">生成结果 <span class="hint" v-if="compareTake.seed != null">seed {{ compareTake.seed }}</span></div>
            <video v-if="!cmpGenErr" ref="cmpGenEl" :src="api.takeVideoUrl(store.sessionId, compareTake.id)" autoplay loop muted @error="cmpGenErr = true"></video>
            <div v-else class="cmp-missing">该视频编码浏览器不支持预览</div>
          </div>
        </div>
        <p class="hint" style="margin:8px 0 0">两侧循环播放；时长不同会渐渐错位，点「同步重播」重新对齐。</p>
      </div>
    </div>

    <div v-if="previewTake" class="tk-mask" @click.self="previewTake = null">
      <div class="tk-box">
        <div class="tk-head">
          <b>{{ takeLabel(previewTake) || previewTake.id }}</b>
          <span v-if="previewTake.prompt" class="hint tk-prompt" :title="previewTake.prompt">{{ previewTake.prompt.slice(0, 60) }}</span>
          <span class="spacer" style="flex:1"></span>
          <button v-if="takes.current !== previewTake.id" class="small" @click="useTake(previewTake); previewTake = null">用这个</button>
          <button class="small" @click="previewTake = null">✕ 关闭</button>
        </div>
        <video v-if="!previewErr" :src="api.takeVideoUrl(store.sessionId, previewTake.id)" controls autoplay loop
               style="width:100%;max-height:60vh;background:#000" @error="previewErr = true"></video>
        <div v-else class="hint" style="padding:30px;text-align:center">该视频编码浏览器不支持预览（不影响抽帧与后续处理）</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ready-bar {
  display: flex; align-items: center; gap: 10px; padding: 8px 12px; margin-bottom: 10px;
  border-radius: 5px; font-size: 13px; background: var(--bg-input); border: 1px solid var(--border);
}
.ready-bar.warn { border-color: #ff980066; color: var(--warn); }
.gen-box { background: var(--bg-input); border: 1px solid var(--border); border-radius: 6px; padding: 12px 14px; margin-bottom: 6px; }
.gen-box.disabled { opacity: .75; }
.ff-row { align-items: flex-start; }
.ff-preview {
  width: 110px; height: 110px; flex-shrink: 0; cursor: pointer;
  border: 1px dashed var(--border); border-radius: 5px; overflow: hidden;
  display: flex; align-items: center; justify-content: center;
  background: repeating-conic-gradient(#3a3a3a 0 25%, #2e2e2e 0 50%) 0 0/14px 14px;
}
.ff-preview:hover { border-color: var(--accent); }
.ff-preview img { width: 100%; height: 100%; object-fit: contain; }
.ff-empty { font-size: 11px; color: var(--text-dim); text-align: center; }
.warn-text { color: var(--warn); font-size: 12px; }
.ok-text { color: var(--ok); }
.rule-chip { font-size: 12px; color: var(--ok); background: #4caf5018; padding: 3px 10px; border-radius: 10px; }
.prompt-bar { align-items: center; gap: 8px; margin-bottom: 4px; flex-wrap: wrap; }
.take-list { margin-top: 4px; }
.take-row {
  display: flex; align-items: center; gap: 10px; padding: 7px 12px;
  background: var(--bg-input); border: 1px solid var(--border); border-radius: 5px; margin-bottom: 5px; font-size: 13px;
}
.take-row.current { border-color: var(--accent); }
.take-badge { font-size: 11px; padding: 1px 8px; border-radius: 8px; background: var(--bg-hover); color: var(--text-dim); }
.take-badge.running { background: #ff980033; color: var(--warn); }
.take-badge.failed { background: #ef535033; color: var(--err); }
.take-badge.succeeded { background: #4caf5022; color: var(--ok); }
.take-name { font-weight: 600; }
.take-prompt { max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tk-mask { position: fixed; inset: 0; background: rgba(0,0,0,.65); z-index: 95; display: flex; align-items: center; justify-content: center; }
.tk-box { width: 640px; max-width: 92vw; background: var(--bg-panel); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
.tk-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; font-size: 13px; }
.tk-prompt { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cmp-box { width: 980px; max-width: 96vw; background: var(--bg-panel); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
.cmp-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.cmp-cell { min-width: 0; }
.cmp-label { font-size: 12px; color: var(--text-dim); margin-bottom: 6px; display: flex; align-items: center; gap: 8px; }
.cmp-cell video { width: 100%; max-height: 56vh; background: #000; display: block; border-radius: 4px; }
.cmp-missing { display: flex; align-items: center; justify-content: center; min-height: 200px; color: var(--text-dim); font-size: 12px; background: var(--bg-input); border-radius: 4px; }
.danger { border-color: var(--err); color: var(--err); }
</style>
