<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { useStore, refreshFrames, refreshSession, loadTakes, toast } from '../stores'
import { startJob } from '../jobs'
import { currentTab } from '../nav'
import api from '../api'
import SaveRuleButton from '../components/SaveRuleButton.vue'

// 工序 3：抽帧——当前素材视频 → 帧序列；参数可沿用/保存为模板规则
const store = useStore()
const startTime = ref(0)
const endTime = ref(10)
const fps = ref(10)
const videoErr = ref('')
const tplAll = ref([])

const videoUrl = computed(() =>
  store.sessionId ? `/api/sessions/${store.sessionId}/video?v=${store.videoVersion}` : '')

// 浏览器 <video> 能解码的常见编码；cv2 能解析 ≠ 浏览器能播放
const BROWSER_CODECS = ['avc1', 'h264', 'vp08', 'vp09', 'vp8', 'vp9', 'av01', 'hev1', 'hvc1']
function codecPlayable(codec) {
  if (!codec) return true
  const c = String(codec).toLowerCase()
  return BROWSER_CODECS.some((k) => c.includes(k))
}
function onVideoError() {
  const codec = store.videoInfo?.codec || '未知'
  videoErr.value = `该视频编码（${codec}）浏览器不支持预览。不影响抽帧与后续处理——抽帧后在底部「帧管理」查看画面即可。`
}

const estimate = computed(() => {
  const dur = Math.max(0, endTime.value - startTime.value)
  return Math.max(0, Math.round(dur * fps.value))
})

// ---- 参考抽帧规则：存于模板，同模板生成的视频节奏一致 ----
const ruleApplied = ref(false)
const ruleTpl = computed(() => {
  const cur = (store.takes.takes || []).find((t) => t.id === store.takes.current)
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
    tplAll.value = (await api.templates()).templates
    ruleApplied.value = true
  } catch { /* ignore */ }
}

function initParamsFromVideo() {
  if (!store.videoInfo) return
  endTime.value = store.videoInfo.duration
  fps.value = Math.min(60, Math.max(0.1, store.videoInfo.fps || 10))
  videoErr.value = ''
}

async function extract() {
  if (!store.videoInfo) return toast('还没有可用视频')
  if (startTime.value >= endTime.value) return toast('开始时间必须小于结束时间')
  await startJob(
    () => api.extract(store.sessionId, {
      start_time: startTime.value, end_time: endTime.value, fps: fps.value,
      template_rule_id: ruleApplied.value && ruleTpl.value ? ruleTpl.value.id : null,
    }),
    { title: `抽帧 (${fps.value} fps)`, onDone: async () => { await refreshFrames() } },
  )
}

// 视频版本变化（生成完成/切版本）时重置参数并重新沿用规则
watch(() => store.videoVersion, () => {
  initParamsFromVideo()
  ruleApplied.value = false
  if (!store.frameCount) applyExtractRule()
})

onMounted(async () => {
  await refreshSession()
  initParamsFromVideo()
  try { tplAll.value = (await api.templates()).templates } catch { /* ignore */ }
  await loadTakes()
  if (store.videoInfo && !store.frameCount) applyExtractRule()
})
</script>

<template>
  <div class="panel">
    <div class="section-title"><h2>3. 抽帧</h2>
      <span class="hint">从当前素材视频抽出帧序列；同模板的动作可沿用/保存抽帧规则</span></div>

    <div v-if="!store.videoInfo" class="ready-bar warn">
      还没有可用的素材视频。
      <button class="small" @click="currentTab = 'generate'">← 去视频生成</button>
    </div>

    <template v-else>
      <div class="grid2">
        <div>
          <div class="preview-box" style="min-height: 260px">
            <video v-if="videoUrl && !videoErr && codecPlayable(store.videoInfo?.codec)"
                   :src="videoUrl" controls style="max-width:100%; max-height:420px" @error="onVideoError"></video>
            <div v-else class="video-unsupported">
              <div style="font-size:26px">🎞️</div>
              <p>{{ videoErr || `该视频编码（${store.videoInfo?.codec || '未知'}）浏览器不支持预览。不影响抽帧与后续处理。` }}</p>
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
          <p class="hint" style="margin-top:8px">素材来自「视频生成」里标为「当前使用」的版本；换版本请回上一步。</p>
        </div>
      </div>

      <h3 style="margin-top:16px">抽帧设置</h3>
      <div class="row">
        <div class="field inline"><label>开始(s)</label><input type="number" v-model.number="startTime" :min="0" :max="store.videoInfo.duration" step="0.1" /></div>
        <div class="field inline"><label>结束(s)</label><input type="number" v-model.number="endTime" :min="0" :max="store.videoInfo.duration" step="0.1" /></div>
        <div class="field inline"><label>FPS</label><input type="number" v-model.number="fps" :min="0.1" :max="60" step="0.5" /></div>
        <button class="small" @click="startTime = 0; endTime = store.videoInfo.duration">全部</button>
        <button class="small" @click="startTime = 0; endTime = store.videoInfo.duration / 2">前50%</button>
        <button class="small" @click="startTime = store.videoInfo.duration / 2; endTime = store.videoInfo.duration">后50%</button>
        <span class="hint">预计抽帧: {{ estimate }} 帧</span>
        <span v-if="store.frameCount" class="hint">（当前已有 {{ store.frameCount }} 帧，重抽会清空）</span>
      </div>
      <div class="row" style="align-items:center">
        <button class="primary" @click="extract">提取帧</button>
        <template v-if="ruleTpl">
          <span v-if="activeRule && ruleApplied" class="rule-chip">
            ✓ 已沿用模板规则「{{ ruleTpl.variant || ruleTpl.key }}」<template
              v-if="activeRule.keep">· 自动保留 {{ activeRule.keep.length }}/{{ activeRule.total }} 帧</template></span>
          <button v-else-if="activeRule" class="small" @click="applyExtractRule">
            沿用模板规则（{{ activeRule.start }}–{{ activeRule.end }}s @{{ activeRule.fps }}{{ activeRule.keep ? ` · 留${activeRule.keep.length}帧` : '' }}）</button>
          <SaveRuleButton @saved="refreshTplsAfterRuleSave" />
        </template>
        <span class="spacer" style="flex:1"></span>
        <button v-if="store.frameCount" class="small" @click="currentTab = 'background'">下一步：背景抠图 →</button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ready-bar {
  display: flex; align-items: center; gap: 10px; padding: 8px 12px; margin-bottom: 10px;
  border-radius: 5px; font-size: 13px; background: var(--bg-input); border: 1px solid var(--border);
}
.ready-bar.warn { border-color: #ff980066; color: var(--warn); }
.rule-chip { font-size: 12px; color: var(--ok); background: #4caf5018; padding: 3px 10px; border-radius: 10px; }
.video-unsupported {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 8px; min-height: 240px; padding: 20px; text-align: center; color: var(--text-dim); font-size: 13px; line-height: 1.7;
}
.video-unsupported p { max-width: 340px; margin: 0; }
</style>
