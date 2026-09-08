<script setup>
import { ref, computed, onMounted } from 'vue'
import { useStore, probeFirstFrame, markFirstFrame, toast, askConfirm } from '../stores'
import { startJob } from '../jobs'
import { currentTab } from '../nav'
import api from '../api'
import FirstFrameLibraryModal from '../components/FirstFrameLibraryModal.vue'

// 工序 1：首帧参考图——角色一致性的锚点，来源：上传 / 精灵首帧图库 / AI 生成
const store = useStore()
const ffInput = ref(null)
const ffLibOpen = ref(false)
const KIND_TXT = { ai_generated: 'AI 生成', batch_import: '批量导入', sprite_ref: '首帧图库',
                   upload: '上传', action_frame: '取自其它动作的帧', legacy_session: '旧会话' }
const kind = computed(() => store.currentAction?.first_frame?.kind || '')
const ffMeta = computed(() => store.currentAction?.first_frame || {})

async function onFirstFrameFile(file) {
  if (!file) return
  try {
    await api.uploadFirstFrame(store.sessionId, file)
    markFirstFrame()
    if (store.currentAction) store.currentAction.first_frame = { kind: 'upload', filename: file.name }
    toast('首帧参考图已设置（已同步入精灵首帧图库）')
  } catch (e) {
    toast(`上传失败: ${e.message}`)
  } finally {
    if (ffInput.value) ffInput.value.value = ''
  }
}

function onFfApplied() {
  markFirstFrame()
  if (store.currentAction) store.currentAction.first_frame = { kind: 'sprite_ref' }
  toast('已从图库设置首帧参考图')
}

// 把本动作首帧设为精灵的正面/背面立绘（AI 生成其它动作首帧的输入）
async function setAsArt(role) {
  try {
    await api.actionFirstFrameAsRef(store.currentSprite.id, store.sessionId, role)
    toast(`已设为精灵的${role === 'front' ? '正面' : '背面'}立绘`)
  } catch (e) {
    toast(`设置失败: ${e.message}`)
  }
}

// ---- AI 生成首帧（立绘 + 参考首帧集 → Seedream）----
const SOURCE_TXT = { action: '动作记忆', template: '模板绑定', key: 'key 绑定',
                     group: '分组绑定', global: '全局默认', builtin: '内置', manual: '手动选用' }
const ffGenOpen = ref(false)
const ffGenSets = ref([])
const ffGenSetId = ref('')
const ffPrompt = ref('')
const ffPromptMatch = ref(null)
const ffPromptLib = ref([])
const ffPromptSel = ref('')
const ffPromptDirty = ref(false)
const ffRemember = ref(true)
const genBusy = ref(false)

function libText(rec) {
  const v = rec.versions.find((x) => x.v === rec.current) || rec.versions[rec.versions.length - 1]
  return (v?.text || '').replace('{action}', store.currentAction?.name || '')
}

async function openFfGen() {
  try {
    const [s, r, lib] = await Promise.all([
      api.ffsets(),
      api.resolvePrompt('first_frame', store.currentSprite.id, store.sessionId),
      api.prompts('first_frame'),
    ])
    ffGenSets.value = s.sets
    if (!s.sets.length) return toast('还没有参考首帧集——先到顶栏「资源库 › 参考首帧库」导入或归档')
    const mem = store.currentAction?.gen_prefs?.first_frame
    if (mem?.set_id && s.sets.some((x) => x.id === mem.set_id)) ffGenSetId.value = mem.set_id
    if (!ffGenSetId.value || !s.sets.some((x) => x.id === ffGenSetId.value)) ffGenSetId.value = s.sets[0].id
    ffPrompt.value = r.text
    ffPromptMatch.value = r
    ffPromptSel.value = r.prompt_id || ''
    ffPromptDirty.value = false
    ffPromptLib.value = lib.prompts
    ffGenOpen.value = true
  } catch (e) { toast(`加载失败: ${e.message}`) }
}

function pickFfPrompt() {
  const rec = ffPromptLib.value.find((x) => x.id === ffPromptSel.value)
  if (!rec) return
  ffPrompt.value = libText(rec)
  ffPromptMatch.value = { prompt_id: rec.id, version: rec.current, name: rec.name, source: 'manual' }
  ffPromptDirty.value = false
}

async function runFfGen() {
  if (!ffGenSetId.value) return toast('请选择参考首帧集')
  if (!ffPrompt.value.trim()) return toast('请填写提示词')
  const warn = store.firstFrame.available ? '当前首帧将被覆盖。' : ''
  if (!(await askConfirm(`AI 生成本动作首帧？（生图按张计费）${warn}`))) return
  ffGenOpen.value = false
  genBusy.value = true
  const m = ffPromptDirty.value ? {} : (ffPromptMatch.value || {})
  try {
    await startJob(() => api.genFirstFrame(store.currentSprite.id, store.sessionId, {
      set_id: ffGenSetId.value, prompt: ffPrompt.value.trim(),
      prompt_id: m.prompt_id || null, prompt_version: m.version || null,
      prompt_name: m.name || null, remember: ffRemember.value,
    }), {
      title: 'AI 生成首帧',
      onDone: () => {
        markFirstFrame()
        if (store.currentAction) store.currentAction.first_frame = { kind: 'ai_generated', ref_set: ffGenSetId.value }
      },
    })
  } finally {
    genBusy.value = false
  }
}

onMounted(probeFirstFrame)
</script>

<template>
  <div class="panel">
    <div class="section-title"><h2>1. 首帧参考图</h2>
      <span class="hint">生成视频时角色一致性的锚点；同一角色的多个动作可共用一张（首帧图库）</span></div>

    <div class="ff-wrap">
      <div class="ff-big" :class="{ empty: !store.firstFrame.available }" @click="ffInput.click()"
           :title="store.firstFrame.available ? '点击更换' : '点击上传'">
        <img v-if="store.firstFrame.available" :src="api.firstFrameUrl(store.sessionId, store.firstFrame.version)" alt="" />
        <div v-else class="ff-empty">
          <div style="font-size:30px">🖼️</div>
          <div>还没有首帧参考图</div>
          <div class="hint">上传 / 从图库选 / AI 生成</div>
        </div>
      </div>

      <div class="ff-side">
        <div class="ff-status">
          <span v-if="store.firstFrame.available" class="ok-text">✓ 首帧就绪</span>
          <span v-else class="warn-text">未就绪——视频生成需要首帧</span>
          <span v-if="kind" class="hint">· 来源：{{ KIND_TXT[kind] || kind }}
            <template v-if="ffMeta.ref_key">（姿势参考 {{ ffMeta.ref_key }}）</template></span>
        </div>
        <div class="row" style="gap:8px;flex-wrap:wrap">
          <button class="small" @click="ffInput.click()">{{ store.firstFrame.available ? '上传替换' : '上传图片' }}</button>
          <button class="small" title="从精灵首帧图库选择（一张图可用于多个动作）" @click="ffLibOpen = true">从图库选</button>
          <button class="primary small" :disabled="genBusy"
                  title="立绘 + 参考首帧集 → AI 生成本动作首帧（按张计费）" @click="openFfGen">
            {{ genBusy ? '生成中…' : (store.firstFrame.available ? 'AI 重新生成' : 'AI 生成首帧') }}</button>
        </div>
        <div v-if="store.firstFrame.available" class="row" style="gap:8px;margin-top:8px;align-items:center">
          <span class="hint">这张首帧作为本精灵的立绘：</span>
          <button class="small" title="设为正面立绘（AI 生成其它正面动作首帧时使用）" @click="setAsArt('front')">设为正面立绘</button>
          <button class="small" title="设为背面立绘（key 含 back 的动作使用）" @click="setAsArt('back')">设为背面立绘</button>
        </div>
        <p class="hint" style="margin:8px 0 0;line-height:1.7">
          AI 生成：用精灵的「正面/背面立绘」+ 参考首帧集中同动作的姿势图生成；
          立绘可在此把某个动作的首帧直接设为，或在首帧图库里上传并标记。
          提示词按提示词库自动匹配，可在弹窗里改。不满意直接重新生成即可（抽卡）。</p>
        <div v-if="store.firstFrame.available" style="margin-top:14px">
          <button class="primary" @click="currentTab = 'generate'">下一步：视频生成 →</button>
        </div>
        <input ref="ffInput" type="file" accept="image/*" style="display:none"
               @change="e => onFirstFrameFile(e.target.files[0])" />
      </div>
    </div>

    <!-- AI 生成首帧：选参考集 + 提示词 -->
    <div v-if="ffGenOpen" class="tk-mask" @click.self="ffGenOpen = false">
      <div class="ffgen-box">
        <h3 style="margin:0 0 12px;font-size:15px">AI 生成首帧</h3>
        <div class="field" style="margin-bottom:10px"><label>参考首帧集（姿势模板）</label>
          <select v-model="ffGenSetId" style="width:100%">
            <option v-for="s in ffGenSets" :key="s.id" :value="s.id">
              {{ s.name }}（{{ s.frames.length }} 张{{ s.group ? ` · ${s.group}` : '' }}）</option>
          </select></div>
        <div class="row prompt-bar" style="margin-bottom:4px">
          <span class="hint">提示词：</span>
          <span v-if="ffPromptMatch" class="rule-chip">
            {{ ffPromptMatch.name }}<template v-if="ffPromptMatch.version"> v{{ ffPromptMatch.version }}</template>
            · {{ SOURCE_TXT[ffPromptMatch.source] || ffPromptMatch.source }}</span>
          <span v-if="ffPromptDirty" class="warn-text">已手改</span>
          <select v-model="ffPromptSel" style="max-width:170px" @change="pickFfPrompt">
            <option value="">— 从库选用 —</option>
            <option v-for="p in ffPromptLib" :key="p.id" :value="p.id">{{ p.name }} v{{ p.current }}</option>
          </select>
        </div>
        <textarea v-model="ffPrompt" rows="4" style="width:100%;resize:vertical;margin-bottom:8px"
                  @input="ffPromptDirty = true"></textarea>
        <p class="hint" style="margin:0 0 10px">图1=姿势参考，图2=立绘；立绘在「从图库选」的图库里上传并标记正面/背面。</p>
        <div class="row" style="align-items:center;gap:10px">
          <label style="display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer">
            <input type="checkbox" v-model="ffRemember" /> 记住为此动作设定</label>
          <span class="spacer" style="flex:1"></span>
          <button class="primary" @click="runFfGen">生成</button>
          <button @click="ffGenOpen = false">取消</button>
        </div>
      </div>
    </div>

    <FirstFrameLibraryModal v-if="ffLibOpen"
                            :sprite-id="store.currentSprite.id"
                            :current-action-id="store.sessionId"
                            @close="ffLibOpen = false"
                            @applied="onFfApplied" />
  </div>
</template>

<style scoped>
.ff-wrap { display: flex; gap: 18px; align-items: flex-start; }
.ff-big {
  width: 320px; height: 320px; flex-shrink: 0; cursor: pointer;
  border: 1px solid var(--border); border-radius: 6px; overflow: hidden;
  display: flex; align-items: center; justify-content: center;
  background: repeating-conic-gradient(#3a3a3a 0 25%, #2e2e2e 0 50%) 0 0/16px 16px;
}
.ff-big.empty { border-style: dashed; }
.ff-big:hover { border-color: var(--accent); }
.ff-big img { width: 100%; height: 100%; object-fit: contain; }
.ff-empty { text-align: center; color: var(--text-dim); font-size: 13px; line-height: 1.8; }
.ff-side { flex: 1; min-width: 0; }
.ff-status { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; font-size: 13px; }
.warn-text { color: var(--warn); font-size: 12px; }
.ok-text { color: var(--ok); }
.rule-chip { font-size: 12px; color: var(--ok); background: #4caf5018; padding: 3px 10px; border-radius: 10px; }
.prompt-bar { align-items: center; gap: 8px; margin-bottom: 4px; flex-wrap: wrap; }
.tk-mask {
  position: fixed; inset: 0; background: rgba(0,0,0,.65); z-index: 95;
  display: flex; align-items: center; justify-content: center;
}
.ffgen-box {
  width: 520px; max-width: 92vw; background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: 8px; padding: 18px 20px;
}
</style>
