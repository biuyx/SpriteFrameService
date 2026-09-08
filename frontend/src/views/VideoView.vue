<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { useStore, refreshFrames, refreshSession, toast, askConfirm } from '../stores'
import { startJob } from '../jobs'
import api from '../api'
import TemplateLibraryModal from '../components/TemplateLibraryModal.vue'
import FirstFrameLibraryModal from '../components/FirstFrameLibraryModal.vue'
import SaveRuleButton from '../components/SaveRuleButton.vue'

const store = useStore()
const dragOver = ref(false)
const uploading = ref(false)
const fileInput = ref(null)

// ---- AI 生成 ----
const gen = ref(null)              // /generate/capabilities 结果
const genModel = ref('')
const genPrompt = ref('')
const genRes = ref('480p')
const genRatio = ref('adaptive')
const genDuration = ref(4)
const genSeed = ref('')
const genFirstFrame = ref('action')   // action(参考图) | frame(当前第N帧)
const genFrameIndex = ref(0)
const takes = ref({ current: null, takes: [] })
const genBusy = ref(false)
const previewTake = ref(null)     // 弹层预览中的 take
const previewErr = ref(false)

// 生成期间定时刷新版本墙（提交与 take 落盘之间有竞态，且要看到“生成中”状态）
let takesTimer = null
function startTakesPolling() {
  stopTakesPolling()
  takesTimer = setInterval(loadTakes, 3000)
}
function stopTakesPolling() {
  if (takesTimer) { clearInterval(takesTimer); takesTimer = null }
}
onUnmounted(stopTakesPolling)

function openPreview(t) {
  previewErr.value = false
  previewTake.value = t
}

// ---- 生成结果 × 参考视频 对比 ----
const compareTake = ref(null)
const cmpRefEl = ref(null)
const cmpGenEl = ref(null)
const cmpRefErr = ref(false)
const cmpGenErr = ref(false)
const cmpPaused = ref(false)

function canCompare(t) {
  return t.status === 'succeeded' && (t.template_id || t.used_reference_video)
}
function openCompare(t) {
  cmpRefErr.value = false
  cmpGenErr.value = false
  cmpPaused.value = false
  compareTake.value = t
}
function cmpRefUrl(t) {
  return t.template_id ? api.templateVideoUrl(t.template_id)
    : api.referenceVideoUrl(store.sessionId, Date.now())
}
function cmpRefLabel(t) {
  if (!t.template_id) return '本地参考视频'
  const tpl = tplAll.value.find((x) => x.id === t.template_id)
  return tpl ? `模板 · ${tpl.variant || tpl.key}` : '模板（已从库中删除）'
}
function cmpEach(fn) {
  for (const v of [cmpRefEl.value, cmpGenEl.value]) if (v) fn(v)
}
function cmpToggle() {
  cmpPaused.value = !cmpPaused.value
  cmpEach((v) => (cmpPaused.value ? v.pause() : v.play().catch(() => {})))
}
function cmpRestart() {
  cmpPaused.value = false
  cmpEach((v) => { v.currentTime = 0; v.play().catch(() => {}) })
}
const ffAvailable = ref(false)        // 是否已有首帧参考图
const ffVersion = ref(0)              // 参考图缓存戳
const ffInput = ref(null)
const ffLibOpen = ref(false)          // 首帧参考图库弹窗

function onFfApplied() {
  ffAvailable.value = true
  ffVersion.value = Date.now()
  genFirstFrame.value = 'action'
  toast('已从图库设置首帧参考图')
}

// ---- AI 生成首帧（立绘 + 参考首帧集 → Seedream）----
const ffGenOpen = ref(false)
const ffGenSets = ref([])
const ffGenSetId = ref('')

async function openFfGen() {
  try {
    const s = await api.ffsets()
    ffGenSets.value = s.sets
    if (!s.sets.length) return toast('还没有参考首帧集——先到精灵库「参考首帧库」导入或归档')
    if (!ffGenSetId.value) ffGenSetId.value = s.sets[0].id
    ffGenOpen.value = true
  } catch (e) { toast(`加载参考首帧库失败: ${e.message}`) }
}

async function runFfGen() {
  if (!ffGenSetId.value) return toast('请选择参考首帧集')
  const warn = ffAvailable.value ? '当前首帧将被覆盖。' : ''
  if (!(await askConfirm(`AI 生成本动作首帧？（生图按张计费）${warn}`))) return
  ffGenOpen.value = false
  await startJob(() => api.genFirstFrame(store.currentSprite.id, store.sessionId,
                                         { set_id: ffGenSetId.value }), {
    title: 'AI 生成首帧',
    onDone: async () => {
      ffAvailable.value = true
      ffVersion.value = Date.now()
      genFirstFrame.value = 'action'
    },
  })
}
const rvAvailable = ref(false)        // 是否已上传参考视频
const rvUse = ref(false)              // 本次生成是否使用参考视频
const rvInput = ref(null)
const rvPreview = ref(false)
const tplAll = ref([])                // 参考视频库全部模板
const tplVariants = ref([])           // 其中匹配本动作名的（推荐组）
const tplSelected = ref('')           // 选中的模板 id('' = 不使用/本地上传)
const tplPreview = ref(false)
const tplLibOpen = ref(false)         // 参考视频库管理弹窗

// 其余模板（模板库组）：推荐组之外的所有模板
const tplOthers = computed(() =>
  tplAll.value.filter((t) => !tplVariants.value.some((v) => v.id === t.id)))

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
    // 动作名不含 key 时（库导入的动作名=变体名），按已绑定模板的 key 找同门变体
    const boundTpl = r.templates.find((t) => t.id === store.currentAction?.template_id)
    if (!matching.length && boundTpl) {
      matching = r.templates.filter((t) => t.key === boundTpl.key)
    }
    tplVariants.value = matching
    // 默认选中动作关联的模板;无关联但有同 key 模板则选第一个;
    // 不匹配的库模板不自动选（避免误用错误动作的参考视频）
    const bound = store.currentAction?.template_id
    if (bound && tplAll.value.some((t) => t.id === bound)) {
      tplSelected.value = bound
    } else if (tplVariants.value.length) {
      tplSelected.value = tplVariants.value[0].id
    } else {
      tplSelected.value = ''
    }
    if (tplSelected.value) rvUse.value = true
    applyTplDuration()
  } catch { /* ignore */ }
}

// 模板携带推荐时长(文件名里的「N秒」)时自动带出
function applyTplDuration() {
  const t = tplAll.value.find((x) => x.id === tplSelected.value)
  if (t?.duration_hint && gen.value?.params?.duration?.includes(t.duration_hint)) {
    genDuration.value = t.duration_hint
  }
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

async function loadGen() {
  try {
    gen.value = await api.genCapabilities()
    const d = gen.value.defaults || {}
    if (!genModel.value) genModel.value = gen.value.default_model
    genRes.value = genRes.value || d.resolution || '480p'
  } catch { gen.value = null }
  await loadTemplates()
  // 提示词预填必须在模板加载之后——带参考视频与否决定用短提示词还是绿幕长模板
  if (gen.value && !genPrompt.value.trim()) genPrompt.value = templateForAction()
  // 探测首帧参考图/参考视频是否已设置
  try {
    const r = await fetch(api.firstFrameUrl(store.sessionId, Date.now()), { credentials: 'same-origin' })
    ffAvailable.value = r.ok
  } catch { ffAvailable.value = false }
  try {
    const r = await fetch(api.referenceVideoUrl(store.sessionId, Date.now()),
                          { method: 'GET', headers: { Range: 'bytes=0-0' }, credentials: 'same-origin' })
    rvAvailable.value = r.ok
    // 没上传过本地参考视频只影响本地路径；选中了库模板时保持默认勾选
    if (!r.ok && !tplSelected.value) rvUse.value = false
  } catch { rvAvailable.value = false }
  await loadTakes()
}

async function onFirstFrameFile(file) {
  if (!file) return
  try {
    await api.uploadFirstFrame(store.sessionId, file)
    ffAvailable.value = true
    ffVersion.value = Date.now()
    genFirstFrame.value = 'action'
    toast('首帧参考图已设置')
  } catch (e) {
    toast(`上传失败: ${e.message}`)
  } finally {
    if (ffInput.value) ffInput.value.value = ''
  }
}

const canGenerate = computed(() =>
  genFirstFrame.value === 'action' ? ffAvailable.value : store.frameCount > 0)

// ---- 按动作名解析提示词模板 ----
function templateForAction() {
  const pt = gen.value?.prompt_templates
  const name = (store.currentAction?.name || '').trim()
  if (!pt || !name) return ''
  // 带参考视频(reference-driven)时动作由视频定义,提示词只约束镜头与背景
  // (实战验证的短提示词;绿幕长模板是纯 i2v 用的,叠加反而互相干扰)
  if (rvUse.value && (tplSelected.value || rvAvailable.value)) {
    // 统一固定短提示词，不带动作名前缀（动作由参考视频定义）
    return '图片参考视频进行动作，固定镜头，无运镜，背景不变，角色位置朝向需要和参考视频完全一致。'
  }
  const lower = name.toLowerCase()
  // 1) 精确命中(含别名)
  const key = pt.templates[lower] ? lower : pt.aliases[name] || pt.aliases[lower]
  if (key && pt.templates[key]) return pt.templates[key]
  // 2) 部分包含(walk_luggage → walk 模板;不改写模板文字,锚定句必须原样保留)
  for (const k of Object.keys(pt.templates)) {
    if (lower.includes(k)) return pt.templates[k]
  }
  for (const [alias, k] of Object.entries(pt.aliases)) {
    if (name.includes(alias) && pt.templates[k]) return pt.templates[k]
  }
  // 3) 通用模板
  return pt.generic.replace('{action}', name)
}

function applyTemplate() {
  const t = templateForAction()
  if (t) { genPrompt.value = t; toast('已按动作名填入模板提示词') }
}

async function loadTakes() {
  try { takes.value = await api.takes(store.sessionId) } catch { /* ignore */ }
}

async function runGenerate() {
  const prompt = genPrompt.value.trim()
  if (!prompt) return toast('请填写提示词')
  if (!canGenerate.value) return toast('请先上传角色首帧参考图')
  if (!(await askConfirm('提交生成任务？（每次生成计费）'))) return
  genBusy.value = true
  try {
    const params = { resolution: genRes.value, ratio: genRatio.value, duration: genDuration.value }
    if (genSeed.value !== '' && !isNaN(+genSeed.value)) params.seed = +genSeed.value
    const first_frame = genFirstFrame.value === 'action' ? { kind: 'action' }
      : { kind: 'frame', frame_index: genFrameIndex.value }
    startTakesPolling()
    await startJob(() => api.generate(store.sessionId,
      { prompt, model: genModel.value, params, first_frame,
        template_id: rvUse.value && tplSelected.value ? tplSelected.value : null,
        use_reference_video: rvUse.value && !tplSelected.value && rvAvailable.value }), {
      title: 'AI 生成视频',
      onDone: async () => {
        stopTakesPolling()
        await loadGen()
        await refreshSession()
        // 生成的版本已自动启用：初始化抽帧参数与预览
        if (store.videoInfo) {
          endTime.value = store.videoInfo.duration
          fps.value = Math.min(60, Math.max(0.1, store.videoInfo.fps || 10))
          videoErr.value = ''
          videoVersion.value++
          if (!store.frameCount) applyExtractRule()
        }
        toast('生成完成，视频已就绪，可开始抽帧')
      },
      onError: async () => {
        stopTakesPolling()
        await loadTakes()   // 显示 failed 卡与错误信息
      },
    })
  } catch (e) {
    toast(`生成失败: ${e.message}`)
  } finally {
    genBusy.value = false
    await loadGen()
  }
}

async function useTake(t) {
  const r = await api.selectTake(store.sessionId, t.id)
  store.videoInfo = r.video_info
  videoErr.value = ''
  videoVersion.value++          // 预览切到新版本
  endTime.value = r.video_info?.duration ?? endTime.value
  fps.value = Math.min(60, Math.max(0.1, r.video_info?.fps || fps.value))
  await loadTakes()
  if (!store.frameCount) applyExtractRule()
  toast(`已切换到该版本，可开始抽帧`)
}

async function removeTake(t) {
  const warn = t.source === 'generate'
    ? `删除生成的版本 ${t.id}？该视频是付费生成的，删除后需重新付费生成。`
    : `删除版本 ${t.id}？`
  if (!(await askConfirm(warn, { danger: true }))) return
  await api.deleteTake(store.sessionId, t.id)
  await loadTakes()
  await refreshSession()
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

const startTime = ref(0)
const endTime = ref(10)
const fps = ref(10)
const videoEl = ref(null)
const videoErr = ref('')

// ---- 参考抽帧规则：存于模板，同模板生成的视频节奏一致，参数校准一次即可 ----
const ruleApplied = ref(false)
const ruleTpl = computed(() => {
  // 规则归属：当前视频版本生成时用的模板优先，其次动作绑定的模板
  const cur = (takes.value.takes || []).find((t) => t.id === takes.value.current)
  const tid = cur?.template_id || store.currentAction?.template_id
  return tplAll.value.find((x) => x.id === tid) || null
})
const activeRule = computed(() => ruleTpl.value?.extract_rule || null)

function applyExtractRule() {
  const r = activeRule.value
  if (!r || !store.videoInfo) return
  startTime.value = Math.min(r.start, store.videoInfo.duration)
  endTime.value = Math.min(r.end, store.videoInfo.duration)
  fps.value = r.fps
  ruleApplied.value = true
}

async function refreshTplsAfterRuleSave() {
  try {
    const r = await api.templates()
    tplAll.value = r.templates
    ruleApplied.value = true
  } catch { /* ignore */ }
}

const videoVersion = ref(0)   // 递增使 <video> 重新加载(切版本后 URL 否则不变)
const videoUrl = computed(() =>
  store.sessionId ? `/api/sessions/${store.sessionId}/video?v=${videoVersion.value}` : ''
)

// 浏览器 <video> 能解码的常见编码；cv2 能解析 ≠ 浏览器能播放
const BROWSER_CODECS = ['avc1', 'h264', 'vp08', 'vp09', 'vp8', 'vp9', 'av01', 'hev1', 'hvc1']

function codecPlayable(codec) {
  if (!codec) return true   // 未知编码先尝试，失败由 @error 兜底
  const c = String(codec).toLowerCase()
  return BROWSER_CODECS.some((k) => c.includes(k))
}

function onVideoError() {
  const codec = store.videoInfo?.codec || '未知'
  videoErr.value = `该视频编码（${codec}）浏览器不支持预览。不影响抽帧与后续处理——` +
    `抽帧后在底部「帧管理」查看画面即可。`
}

const estimate = computed(() => {
  const dur = Math.max(0, endTime.value - startTime.value)
  return Math.max(0, Math.round(dur * fps.value))
})

function onFile(file) {
  if (!file) return
  uploadFile(file)
}

async function uploadFile(file) {
  if (!store.sessionId) return
  uploading.value = true
  try {
    const res = await api.uploadVideo(store.sessionId, file)
    store.videoInfo = res.video_info
    videoErr.value = ''   // 换了新视频，重新尝试预览
    // 帧率默认取源视频帧率（上限 60）
    const srcFps = store.videoInfo.fps || 10
    fps.value = Math.min(60, Math.max(0.1, srcFps))
    endTime.value = store.videoInfo.duration
    videoVersion.value++
    toast('视频上传成功')
    await refreshSession()
  } catch (e) {
    toast(`上传失败: ${e.message}`)
  } finally {
    uploading.value = false
    // 重置文件输入，允许重复选择同一文件
    if (fileInput.value) fileInput.value.value = ''
  }
}

async function extract() {
  if (!store.videoInfo) return toast('请先上传视频')
  if (startTime.value >= endTime.value) return toast('开始时间必须小于结束时间')
  await startJob(
    () => api.extract(store.sessionId, {
      start_time: startTime.value,
      end_time: endTime.value,
      fps: fps.value,
      // 沿用规则时带上模板：抽帧后按保留集自动删多余帧
      template_rule_id: ruleApplied.value && ruleTpl.value ? ruleTpl.value.id : null,
    }),
    {
      title: `抽帧 (${fps.value} fps)`,
      onDone: async () => {
        await refreshFrames()
      },
    },
  )
}

onMounted(async () => {
  await refreshSession()
  if (store.videoInfo) {
    endTime.value = store.videoInfo.duration
    const srcFps = store.videoInfo.fps || 10
    fps.value = Math.min(60, Math.max(0.1, srcFps))
  }
  await loadGen()
  // 未抽过帧时，自动沿用模板的参考抽帧规则
  if (store.videoInfo && !store.frameCount) applyExtractRule()
  // 有未完结的远端生成任务时自动重挂（重启后取回结果）
  const running = (takes.value.takes || []).some(
    (t) => t.source === 'generate' && (t.status === 'running' || t.status === 'pending'))
  if (running && gen.value?.configured) {
    await startJob(() => api.reconcileTakes(store.sessionId), {
      title: '恢复生成任务',
      onDone: async () => { await loadTakes(); await refreshSession() },
    })
  }
})
</script>

<template>
  <div class="panel">
    <div class="section-title"><h2>1. 获取素材：AI 生成 或 上传视频</h2></div>

    <!-- AI 生成 -->
    <div class="gen-box" :class="{ disabled: !gen?.configured }">
      <div class="row" style="align-items:center">
        <b style="font-size:13px">AI 生成视频（Seedance）</b>
        <span v-if="gen && !gen.configured" class="hint warn-text">
          未配置 API Key——在 backend\.env 设置 SPRITE_ARK_API_KEY 后重启即可启用</span>
      </div>
      <template v-if="gen?.configured">
        <!-- 首帧参考图（必填）：保证角色一致性 -->
        <div class="row ff-row">
          <div class="ff-preview" @click="ffInput.click()">
            <img v-if="ffAvailable" :src="api.firstFrameUrl(store.sessionId, ffVersion)" alt="" />
            <span v-else class="ff-empty">+ 上传角色<br>首帧参考图</span>
          </div>
          <div style="flex:1">
            <div class="row" style="margin-bottom:6px">
              <div class="field inline"><label>首帧来源</label>
                <select v-model="genFirstFrame">
                  <option value="action">参考图{{ ffAvailable ? '' : '（未上传）' }}</option>
                  <option value="frame" :disabled="!store.frameCount">当前第 N 帧</option>
                </select>
              </div>
              <div v-if="genFirstFrame === 'frame'" class="field inline">
                <label>帧</label><input type="number" v-model.number="genFrameIndex" :min="0" :max="store.frameCount - 1" style="width:70px" />
              </div>
              <button class="small" @click="ffInput.click()">{{ ffAvailable ? '更换参考图' : '上传参考图' }}</button>
              <button class="small" title="从精灵首帧图库选择（一张图可用于多个动作）" @click="ffLibOpen = true">从图库选</button>
              <button class="small" title="立绘 + 参考首帧集 → AI 生成本动作首帧（按张计费）" @click="openFfGen">AI 生成首帧</button>
              <button class="small" title="按动作名重新填入模板提示词" @click="applyTemplate">模板提示词</button>
              <span v-if="!canGenerate" class="warn-text">生成必须提供角色首帧参考图</span>
            </div>
            <textarea v-model="genPrompt" rows="2" style="width:100%;resize:vertical"
                      placeholder="提示词，如：角色向前走路，动作循环，白色背景，镜头固定"></textarea>
          </div>
          <input ref="ffInput" type="file" accept="image/*" style="display:none"
                 @change="e => onFirstFrameFile(e.target.files[0])" />
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
        </div>
        <div class="row" style="align-items:center">
          <span class="hint">参考视频（可选，动作/镜头节奏参考）：</span>
          <template v-if="tplAll.length">
            <label style="display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer">
              <input type="checkbox" v-model="rvUse" /> 使用模板</label>
            <select v-model="tplSelected" style="max-width:210px"
                    @change="applyTplDuration(); genPrompt = templateForAction()">
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
          已启用参考视频：角色形象仍以参考图为准，视频仅提供动作与镜头节奏。</p>
        <p class="hint" style="margin:4px 0 0">
          默认 Mini 模型 + 480p + 4s；生成约需数分钟，可切到其他页面继续工作。</p>
        <p v-if="gen.prompt_templates?.notes" class="hint" style="margin:4px 0 0">
          {{ gen.prompt_templates.notes }}</p>
      </template>
    </div>

    <!-- 版本列表 -->
    <div v-if="takes.takes?.length" class="take-list">
      <div class="section-title" style="margin-top:12px"><h2>素材版本</h2></div>
      <div v-for="t in [...takes.takes].reverse()" :key="t.id" class="take-row"
           :class="{ current: takes.current === t.id }">
        <span class="take-badge" :class="t.status">
          {{ t.status === 'succeeded' ? (t.source === 'generate' ? '生成' : '上传')
             : t.status === 'running' ? '生成中' : t.status === 'failed' ? '失败' : t.status }}</span>
        <span class="take-name">{{ takeLabel(t) }}</span>
        <span class="hint">{{ fmtTime(t.created_at) }}</span>
        <span v-if="t.prompt" class="hint take-prompt" :title="t.prompt">{{ t.prompt.slice(0, 40) }}…</span>
        <span v-if="t.error" class="warn-text" :title="t.error">{{ t.error.slice(0, 50) }}</span>
        <span class="spacer" style="flex:1"></span>
        <span v-if="takes.current === t.id" class="ok-text" style="font-size:12px">✓ 当前使用</span>
        <button v-else-if="t.status === 'succeeded'" class="small" @click="useTake(t)">用这个</button>
        <button v-if="t.status === 'succeeded'" class="small" @click="openPreview(t)">预览</button>
        <button v-if="canCompare(t)" class="small" title="与参考视频并排对比" @click="openCompare(t)">对比</button>
        <button class="small danger" @click="removeTake(t)">删除</button>
      </div>
    </div>

    <!-- 参考视频库管理 -->
    <TemplateLibraryModal v-if="tplLibOpen"
                          @close="tplLibOpen = false"
                          @changed="loadTemplates" />

    <!-- AI 生成首帧：选参考集 -->
    <div v-if="ffGenOpen" class="tk-mask" @click.self="ffGenOpen = false">
      <div class="ffgen-box">
        <h3 style="margin:0 0 12px;font-size:15px">AI 生成首帧</h3>
        <div class="field" style="margin-bottom:12px"><label>参考首帧集（姿势模板）</label>
          <select v-model="ffGenSetId" style="width:100%">
            <option v-for="s in ffGenSets" :key="s.id" :value="s.id">
              {{ s.name }}（{{ s.frames.length }} 张{{ s.group ? ` · ${s.group}` : '' }}）</option>
          </select></div>
        <p class="hint" style="margin:0 0 12px">
          用本精灵已标记的立绘（正/背面）+ 参考集中同动作的首帧生成；
          立绘在「首帧图库」里上传并点图片左下角标记。</p>
        <div class="row" style="justify-content:flex-end;gap:10px">
          <button class="primary" @click="runFfGen">生成</button>
          <button @click="ffGenOpen = false">取消</button>
        </div>
      </div>
    </div>

    <!-- 首帧参考图库（精灵级共享） -->
    <FirstFrameLibraryModal v-if="ffLibOpen"
                            :sprite-id="store.currentSprite.id"
                            :current-action-id="store.sessionId"
                            @close="ffLibOpen = false"
                            @applied="onFfApplied" />

    <!-- 模板预览弹层 -->
    <div v-if="tplPreview" class="tk-mask" @click.self="tplPreview = false">
      <div class="tk-box">
        <div class="tk-head"><b>动作模板预览</b><span class="spacer" style="flex:1"></span>
          <button class="small" @click="tplPreview = false">✕ 关闭</button></div>
        <video :src="api.templateVideoUrl(tplSelected)"
               controls autoplay loop style="width:100%;max-height:60vh;background:#000"></video>
      </div>
    </div>

    <!-- 参考视频预览弹层 -->
    <div v-if="rvPreview" class="tk-mask" @click.self="rvPreview = false">
      <div class="tk-box">
        <div class="tk-head"><b>参考视频</b><span class="spacer" style="flex:1"></span>
          <button class="small" @click="rvPreview = false">✕ 关闭</button></div>
        <video :src="api.referenceVideoUrl(store.sessionId, Date.now())"
               controls autoplay loop style="width:100%;max-height:60vh;background:#000"></video>
      </div>
    </div>

    <!-- 生成结果 × 参考视频 对比弹层 -->
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
            <video v-if="!cmpRefErr" ref="cmpRefEl" :src="cmpRefUrl(compareTake)"
                   autoplay loop muted @error="cmpRefErr = true"></video>
            <div v-else class="cmp-missing">参考视频不可用（可能已被删除或替换）</div>
          </div>
          <div class="cmp-cell">
            <div class="cmp-label">生成结果
              <span class="hint" v-if="compareTake.seed != null">seed {{ compareTake.seed }}</span></div>
            <video v-if="!cmpGenErr" ref="cmpGenEl" :src="api.takeVideoUrl(store.sessionId, compareTake.id)"
                   autoplay loop muted @error="cmpGenErr = true"></video>
            <div v-else class="cmp-missing">该视频编码浏览器不支持预览</div>
          </div>
        </div>
        <p class="hint" style="margin:8px 0 0">
          两侧循环播放；时长不同会渐渐错位，点「同步重播」重新对齐。检查动作节奏、角色朝向与位置是否一致。</p>
      </div>
    </div>

    <!-- take 视频预览弹层 -->
    <div v-if="previewTake" class="tk-mask" @click.self="previewTake = null">
      <div class="tk-box">
        <div class="tk-head">
          <b>{{ takeLabel(previewTake) || previewTake.id }}</b>
          <span v-if="previewTake.prompt" class="hint tk-prompt" :title="previewTake.prompt">
            {{ previewTake.prompt.slice(0, 60) }}</span>
          <span class="spacer" style="flex:1"></span>
          <button v-if="takes.current !== previewTake.id" class="small"
                  @click="useTake(previewTake); previewTake = null">用这个</button>
          <button class="small" @click="previewTake = null">✕ 关闭</button>
        </div>
        <video v-if="!previewErr" :src="api.takeVideoUrl(store.sessionId, previewTake.id)"
               controls autoplay loop style="width:100%;max-height:60vh;background:#000"
               @error="previewErr = true"></video>
        <div v-else class="hint" style="padding:30px;text-align:center">
          该视频编码浏览器不支持预览（不影响抽帧与后续处理）</div>
      </div>
    </div>

    <div class="section-title" style="margin-top:14px"><h2>上传视频</h2></div>

    <div
      class="upload-drop"
      :class="{ dragover: dragOver }"
      @dragover.prevent="dragOver = true"
      @dragleave="dragOver = false"
      @drop.prevent="dragOver = false; onFile($event.dataTransfer.files[0])"
      @click="fileInput.click()"
    >
      <div v-if="!uploading">点击或拖拽视频文件到此处<br><span class="hint">支持 mp4 / mov / avi / mkv / webm 等（浏览器可播放的编码可直接预览）</span></div>
      <div v-else>上传中...</div>
      <input ref="fileInput" type="file" accept="video/*" style="display:none" @change="e => onFile(e.target.files[0])" />
    </div>

    <div v-if="store.videoInfo" style="margin-top: 14px">
      <div class="grid2">
        <div>
          <div class="preview-box" style="min-height: 260px">
            <video
              v-if="videoUrl && !videoErr && codecPlayable(store.videoInfo?.codec)"
              ref="videoEl" :src="videoUrl"
              controls style="max-width:100%; max-height:420px"
              @error="onVideoError"
            ></video>
            <div v-else class="video-unsupported">
              <div style="font-size:26px">🎞️</div>
              <p>{{ videoErr || `该视频编码（${store.videoInfo?.codec || '未知'}）浏览器不支持预览。不影响抽帧与后续处理——抽帧后在底部「帧管理」查看画面即可。` }}</p>
            </div>
          </div>
        </div>
        <div>
          <h3 style="margin-top:0">视频信息</h3>
          <table class="tbl">
            <tr><th>分辨率</th><td>{{ store.videoInfo.width }} x {{ store.videoInfo.height }}</td></tr>
            <tr><th>帧率</th><td>{{ store.videoInfo.fps.toFixed(2) }} fps</td></tr>
            <tr><th>总帧数</th><td>{{ store.videoInfo.frame_count }}</td></tr>
            <tr><th>时长</th><td>{{ store.videoInfo.duration.toFixed(2) }} s</td></tr>
            <tr><th>编码</th><td>{{ store.videoInfo.codec }}</td></tr>
          </table>
        </div>
      </div>

      <h3 style="margin-top:16px">2. 抽帧设置</h3>
      <div class="row">
        <div class="field inline"><label>开始(s)</label><input type="number" v-model.number="startTime" :min="0" :max="store.videoInfo.duration" step="0.1" /></div>
        <div class="field inline"><label>结束(s)</label><input type="number" v-model.number="endTime" :min="0" :max="store.videoInfo.duration" step="0.1" /></div>
        <div class="field inline"><label>FPS</label><input type="number" v-model.number="fps" :min="0.1" :max="60" step="0.5" /></div>
        <button class="small" @click="startTime = 0; endTime = store.videoInfo.duration">全部</button>
        <button class="small" @click="startTime = 0; endTime = store.videoInfo.duration / 2">前50%</button>
        <button class="small" @click="startTime = store.videoInfo.duration / 2; endTime = store.videoInfo.duration">后50%</button>
        <span class="hint">预计抽帧: {{ estimate }} 帧</span>
      </div>
      <div class="row" style="align-items:center">
        <button class="primary" @click="extract">提取帧</button>
        <template v-if="ruleTpl">
          <span v-if="activeRule && ruleApplied" class="rule-chip">
            ✓ 已沿用模板规则「{{ ruleTpl.variant || ruleTpl.key }}」<template
              v-if="activeRule.keep">· 自动保留 {{ activeRule.keep.length }}/{{ activeRule.total }} 帧</template></span>
          <button v-else-if="activeRule" class="small"
                  @click="applyExtractRule">
            沿用模板规则（{{ activeRule.start }}–{{ activeRule.end }}s @{{ activeRule.fps
            }}{{ activeRule.keep ? ` · 留${activeRule.keep.length}帧` : '' }}）</button>
          <SaveRuleButton @saved="refreshTplsAfterRuleSave" />
        </template>
      </div>    </div>
  </div>
</template>

<style scoped>
.gen-box {
  background: var(--bg-input); border: 1px solid var(--border); border-radius: 6px;
  padding: 12px 14px; margin-bottom: 6px;
}
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
.ff-empty { font-size: 11px; color: var(--text-dim); text-align: center; line-height: 1.6; }
.warn-text { color: var(--warn); font-size: 12px; }
.ok-text { color: var(--ok); }
.rule-chip {
  font-size: 12px; color: var(--ok); background: #4caf5018;
  padding: 3px 10px; border-radius: 10px;
}
.take-list { margin-top: 4px; }
.take-row {
  display: flex; align-items: center; gap: 10px; padding: 7px 12px;
  background: var(--bg-input); border: 1px solid var(--border); border-radius: 5px;
  margin-bottom: 5px; font-size: 13px;
}
.take-row.current { border-color: var(--accent); }
.take-badge {
  font-size: 11px; padding: 1px 8px; border-radius: 8px;
  background: var(--bg-hover); color: var(--text-dim);
}
.take-badge.running { background: #ff980033; color: var(--warn); }
.take-badge.failed { background: #ef535033; color: var(--err); }
.take-badge.succeeded { background: #4caf5022; color: var(--ok); }
.take-name { font-weight: 600; }
.take-prompt { max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tk-mask {
  position: fixed; inset: 0; background: rgba(0,0,0,.65); z-index: 95;
  display: flex; align-items: center; justify-content: center;
}
.tk-box {
  width: 640px; max-width: 92vw; background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px;
}
.tk-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; font-size: 13px; }
.ffgen-box {
  width: 380px; max-width: 92vw; background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: 8px; padding: 18px 20px;
}
.cmp-box {
  width: 980px; max-width: 96vw; background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px;
}
.cmp-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.cmp-cell { min-width: 0; }
.cmp-label {
  font-size: 12px; color: var(--text-dim); margin-bottom: 6px;
  display: flex; align-items: center; gap: 8px;
}
.cmp-cell video { width: 100%; max-height: 56vh; background: #000; display: block; border-radius: 4px; }
.cmp-missing {
  display: flex; align-items: center; justify-content: center; min-height: 200px;
  color: var(--text-dim); font-size: 12px; background: var(--bg-input); border-radius: 4px;
}
.tk-prompt { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.danger { border-color: var(--err); color: var(--err); }
.video-unsupported {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 8px; min-height: 240px; padding: 20px; text-align: center;
  color: var(--text-dim); font-size: 13px; line-height: 1.7;
}
.video-unsupported p { max-width: 340px; margin: 0; }
</style>
