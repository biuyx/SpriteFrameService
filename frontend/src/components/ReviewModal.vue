<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useStore, openAction, toast, askConfirm } from '../stores'
import { startJob } from '../jobs'
import { currentTab } from '../nav'
import api from '../api'

// 素材速览：把全角色的首帧图或素材视频铺成一张网格，肉眼一次扫完。
//
// 之前要检查 20 个动作的视频，只能逐个打开工作台，看完退出再进下一个。
// 这里的取舍：
//   - 视频只在滚进视口时才加载（IntersectionObserver），20 段同时加载会卡
//   - 静音循环自动播，动画类素材要看动起来才看得出问题
//   - 有问题的当场点「去重做」直达该动作，不用记下来事后找
const props = defineProps({
  actions: { type: Array, default: () => [] },   // 看板传入，用于首帧模式
  mode: { type: String, default: 'video' },      // video | frame
})
const emit = defineEmits(['close'])
const store = useStore()

const mode = ref(props.mode)
const rows = ref([])
const loading = ref(true)
const onlyProblem = ref(false)
const imgV = Date.now() % 100000
const zoom = ref(null)          // 放大查看的条目

async function load() {
  loading.value = true
  try {
    if (mode.value === 'video') {
      rows.value = (await api.spriteReview(store.currentSprite.id, 'video')).items
    } else {
      rows.value = (await api.spriteReview(store.currentSprite.id, 'frame')).items
    }
    // 提示词可就地改：草稿与服务端解析出来的那条分开存，改过才算数
    draft.value = {}
    for (const r of rows.value) draft.value[r.action_id] = r.prompt?.text || ''
  } catch (e) {
    toast(`加载失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

function missing(r) {
  return mode.value === 'video' ? !r.take_id : !r.has_first_frame
}
const visible = computed(() =>
  onlyProblem.value ? rows.value.filter(missing) : rows.value)
const missingCount = computed(() => rows.value.filter(missing).length)

function videoUrl(r) {
  return api.takeVideoUrl(r.action_id, r.take_id)
}
function frameUrl(r) {
  // 重生成后文件名不变，带个戳绕开浏览器缓存
  return api.actionFirstFrameUrl(store.currentSprite.id, r.action_id,
                                 bump.value[r.action_id] || imgV)
}

// 视口内才加载：20 段视频一起拉会把页面拖垮
const seen = ref({})
let io = null
function observe(el, id) {
  if (!el || !io) return
  el.dataset.id = id
  io.observe(el)
}
onMounted(async () => {
  io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) {
        seen.value[e.target.dataset.id] = true
        io.unobserve(e.target)
      }
    }
  }, { rootMargin: '200px' })
  window.addEventListener('keydown', onKey)
  await load()
})
onUnmounted(() => {
  io?.disconnect()
  window.removeEventListener('keydown', onKey)
})

async function switchMode(m) {
  if (mode.value === m) return
  mode.value = m
  seen.value = {}
  zoom.value = null
  await load()
}

// 放大后用左右键连续翻，不必退出再点下一个
function step(d) {
  const list = visible.value
  const i = list.findIndex((r) => r.action_id === zoom.value?.action_id)
  if (i < 0) return
  const n = i + d
  if (n >= 0 && n < list.length) zoom.value = list[n]
}
function onKey(e) {
  if (e.key === 'Escape') return zoom.value ? (zoom.value = null) : emit('close')
  if (!zoom.value) return
  if (e.key === 'ArrowRight') { e.preventDefault(); step(1) }
  if (e.key === 'ArrowLeft') { e.preventDefault(); step(-1) }
}

async function openIn(r) {
  await openAction(store.currentSprite.id, r.action_id)
  currentTab.value = mode.value === 'video' ? 'generate' : 'firstframe'
  emit('close')
}

// ---- 就地重生成：改完提示词不必跳去工作台 ----
const draft = ref({})          // action_id -> 正在编辑的提示词
const busy = ref({})           // action_id -> {message, progress}
const remember = ref(true)     // 把改过的提示词记为该动作的设定
const bump = ref({})           // action_id -> 缓存戳，重生成后强制刷新媒体

function dirty(r) {
  return (draft.value[r.action_id] || '') !== (r.prompt?.text || '')
}
const dirtyCount = computed(() => rows.value.filter(dirty).length)
function resetPrompt(r) {
  draft.value[r.action_id] = r.prompt?.text || ''
}

async function regen(r) {
  const aid = r.action_id
  if (busy.value[aid]) return
  const text = (draft.value[aid] || '').trim()
  if (!text) return toast('提示词不能为空')
  const changed = dirty(r)
  const what = mode.value === 'video' ? '视频' : '首帧'
  const lines = [`重新生成「${r.name}」的${what}？${what === '视频' ? '按次' : '按张'}计费。`]
  if (changed) lines.push('使用你改过的提示词。')
  if (changed && remember.value) lines.push('并把这条提示词记为该动作的设定。')
  if (!(await askConfirm(lines.join('\n')))) return

  busy.value = { ...busy.value, [aid]: { message: '提交中…', progress: 0 } }
  const call = mode.value === 'video'
    ? () => api.batchGenerate(store.currentSprite.id, {
        action_ids: [aid], prompt: text, remember: changed && remember.value })
        .then((res) => {
          const one = (res.submitted || [])[0]
          if (!one) throw new Error((res.skipped?.[0]?.reason) || '未能提交')
          return { job_id: one.job_id }
        })
    : () => api.genFirstFrame(store.currentSprite.id, aid,
        { prompt: text, remember: changed && remember.value })

  try {
    await startJob(call, {
      title: `重新生成${what}·${r.name}`,
      onProgress: (j) => {
        busy.value = { ...busy.value,
                       [aid]: { message: j.message, progress: j.progress } }
      },
      onDone: async () => {
        const b = { ...busy.value }; delete b[aid]; busy.value = b
        bump.value = { ...bump.value, [aid]: Date.now() }
        await load()
        toast(`「${r.name}」已重新生成`)
      },
      onError: () => {
        const b = { ...busy.value }; delete b[aid]; busy.value = b
      },
    })
  } catch {
    const b = { ...busy.value }; delete b[aid]; busy.value = b
  }
}

function fmtSize(n) {
  if (!n) return ''
  return n < 1048576 ? `${(n / 1024).toFixed(0)}KB` : `${(n / 1048576).toFixed(1)}MB`
}
function meta(r) {
  const v = r.video
  if (!v) return ''
  return [v.duration ? `${v.duration}s` : '', v.fps ? `${v.fps}fps` : '',
          v.resolution, fmtSize(v.bytes)].filter(Boolean).join(' · ')
}
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <div class="modal-head">
        <h3>素材速览（{{ store.currentSprite?.name }}）</h3>
        <div class="tabs">
          <button class="tab" :class="{ on: mode === 'frame' }" @click="switchMode('frame')">首帧图</button>
          <button class="tab" :class="{ on: mode === 'video' }" @click="switchMode('video')">素材视频</button>
        </div>
        <span class="spacer"></span>
        <label v-if="missingCount" class="chk">
          <input type="checkbox" v-model="onlyProblem" />
          只看缺的（{{ missingCount }}）</label>
        <label class="chk" title="重生成时若改过提示词，把它记为该动作的设定，下次沿用">
          <input type="checkbox" v-model="remember" /> 改动记为设定</label>
        <button class="small" @click="load">刷新</button>
        <button class="small" @click="emit('close')">✕ 关闭</button>
      </div>

      <p class="hint" style="margin:0 0 8px">
        {{ mode === 'video' ? '各动作「当前使用」的素材视频，滚到哪里加载哪里，静音循环播放。'
                            : '各动作当前的首帧图。' }}
        点任意一项放大，放大后用 ← → 连续翻看。
        下面是该动作会用到的提示词，可就地改再「重新生成」，不必跳去工作台。
        <span v-if="dirtyCount" class="warn-text">（{{ dirtyCount }} 条已改动，未重生成前不会生效）</span>
      </p>

      <div v-if="loading" class="hint" style="padding:30px;text-align:center">加载中…</div>
      <div v-else-if="!visible.length" class="hint" style="padding:30px;text-align:center">
        没有可显示的条目</div>

      <div v-else class="rv-grid">
        <div v-for="r in visible" :key="r.action_id" class="rv-item"
             :ref="(el) => observe(el, r.action_id)">
          <div class="rv-box" @click="zoom = r">
            <template v-if="mode === 'video'">
              <video v-if="r.take_id && seen[r.action_id]" :src="videoUrl(r)"
                     muted loop autoplay playsinline preload="metadata"></video>
              <span v-else-if="r.take_id" class="rv-hold">滚动到此处加载</span>
              <span v-else class="rv-none">
                {{ r.generating ? '生成中…' : '无素材视频' }}</span>
            </template>
            <template v-else>
              <img v-if="r.has_first_frame" :src="frameUrl(r)" alt="" loading="lazy" />
              <span v-else class="rv-none">缺首帧</span>
            </template>
          </div>
          <div class="rv-name" :title="r.name">
            {{ r.name }}
            <span v-if="r.takes > 1" class="rv-badge" :title="`共 ${r.takes} 个版本`">{{ r.takes }}版</span>
          </div>
          <div class="rv-meta">{{ meta(r) }}</div>

          <!-- 当前提示词：可就地改，改完直接重生成 -->
          <textarea v-model="draft[r.action_id]" class="rv-prompt" rows="3"
                    :class="{ dirty: dirty(r) }"
                    :placeholder="r.prompt ? '' : '没有解析到提示词'"
                    :title="r.prompt ? `来源：${r.prompt.name}` : ''"
                    @click.stop></textarea>
          <div class="rv-act">
            <span v-if="dirty(r)" class="rv-tag warn-text" title="与当前设定不同">已改</span>
            <span v-else-if="r.prompt" class="rv-tag dim"
                  :title="`提示词来源：${r.prompt.name}`">{{ r.prompt.name }}</span>
            <span class="spacer" style="flex:1"></span>
            <button v-if="dirty(r)" class="small" title="还原成当前设定"
                    @click.stop="resetPrompt(r)">还原</button>
            <button class="small" :disabled="!!busy[r.action_id]"
                    @click.stop="regen(r)">
              {{ busy[r.action_id] ? '生成中…' : '重新生成' }}</button>
            <button class="small" title="打开该动作的工作台" @click.stop="openIn(r)">打开</button>
          </div>
          <div v-if="busy[r.action_id]" class="rv-prog"
               :title="busy[r.action_id].message">
            <div class="rv-fill" :style="{ width: busy[r.action_id].progress + '%' }"></div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- 放大查看 -->
  <div v-if="zoom" class="modal-mask inner" @click.self="zoom = null">
    <div class="zoom-box">
      <div class="modal-head" style="margin-bottom:8px">
        <b>{{ zoom.name }}</b>
        <span class="hint">{{ meta(zoom) }}</span>
        <span class="spacer"></span>
        <button class="small" title="上一个（←）" @click="step(-1)">‹</button>
        <button class="small" title="下一个（→）" @click="step(1)">›</button>
        <button class="small" :disabled="!!busy[zoom.action_id]"
                @click="regen(zoom)">重新生成</button>
        <button class="small" @click="openIn(zoom)">打开</button>
        <button class="small" @click="zoom = null">✕</button>
      </div>
      <video v-if="mode === 'video' && zoom.take_id" :key="zoom.action_id"
             :src="videoUrl(zoom)" controls muted loop autoplay playsinline></video>
      <img v-else-if="mode === 'frame' && zoom.has_first_frame" :src="frameUrl(zoom)" alt="" />
      <p v-else class="hint" style="padding:40px;text-align:center">该动作还没有素材</p>
    </div>
  </div>
</template>

<style scoped>
.modal-mask {
  position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 90;
  display: flex; align-items: center; justify-content: center;
}
.modal-mask.inner { z-index: 96; background: rgba(0,0,0,.72); }
.modal {
  width: 1060px; max-width: 96vw; height: 82vh; display: flex; flex-direction: column;
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 14px 18px 16px;
}
.modal-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.modal-head h3 { margin: 0; font-size: 16px; }
.spacer { flex: 1; }
.chk { display: flex; align-items: center; gap: 5px; font-size: 12px; cursor: pointer; }
.tabs { display: flex; gap: 2px; margin-left: 6px; }
.tab {
  border: none; background: none; color: var(--text-dim); cursor: pointer;
  padding: 4px 10px; font-size: 13px; border-bottom: 2px solid transparent;
}
.tab.on { color: var(--text); border-bottom-color: var(--accent); background: var(--bg-input); }

.rv-grid {
  flex: 1; min-height: 0; overflow-y: auto; display: grid; gap: 12px;
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); align-content: start;
}
.rv-item { position: relative; }
.rv-box {
  aspect-ratio: 1; background: var(--bg-input); border: 1px solid var(--border);
  border-radius: 5px; overflow: hidden; cursor: zoom-in;
  display: flex; align-items: center; justify-content: center;
}
.rv-box video, .rv-box img { width: 100%; height: 100%; object-fit: contain; }
.rv-hold, .rv-none { font-size: 12px; color: var(--text-dim); }
.rv-none { color: var(--warn); }
.rv-name {
  margin-top: 5px; font-size: 13px; display: flex; align-items: center; gap: 6px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.rv-badge {
  font-size: 10px; padding: 0 5px; border-radius: 7px;
  background: var(--bg-input); color: var(--text-dim);
}
.rv-meta {
  font-size: 11px; color: var(--text-dim); height: 15px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.rv-redo { position: absolute; right: 4px; top: 4px; opacity: 0; transition: opacity .15s; }
.rv-item:hover .rv-redo { opacity: 1; }

.zoom-box {
  width: 760px; max-width: 94vw; background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px;
}
.zoom-box video, .zoom-box img {
  width: 100%; max-height: 68vh; object-fit: contain; background: #000; border-radius: 4px;
}
.rv-prompt {
  width: 100%; margin-top: 4px; font-size: 11.5px; line-height: 1.5;
  resize: vertical; min-height: 46px;
}
.rv-prompt.dirty { border-color: var(--warn); }
.rv-act { display: flex; align-items: center; gap: 4px; margin-top: 3px; }
.rv-tag {
  font-size: 10.5px; max-width: 86px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
.rv-tag.dim { color: var(--text-dim); }
.rv-prog { height: 3px; background: var(--bg-input); border-radius: 2px; margin-top: 4px; }
.rv-fill { height: 100%; background: var(--accent); border-radius: 2px; transition: width .3s; }
</style>
