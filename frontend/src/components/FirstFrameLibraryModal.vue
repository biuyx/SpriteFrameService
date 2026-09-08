<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { toast, askConfirm } from '../stores'
import api from '../api'

// currentActionId：工作台模式（只有参考图库，应用到当前动作）
// actions：看板模式（两个页签：动作首帧总览 / 参考图库；元素含 id/name/first_frame/summary）
const props = defineProps({
  spriteId: { type: String, required: true },
  currentActionId: { type: String, default: null },
  actions: { type: Array, default: () => [] },
})
const emit = defineEmits(['close', 'applied'])

const boardMode = computed(() => !props.currentActionId && props.actions.length > 0)
const tab = ref('overview')       // overview | refs（工作台模式固定 refs）
const changed = ref(false)        // 有过改动 → 关闭时通知父级刷新

// ---- 参考图库 ----
const refs = ref([])
const loading = ref(true)
const selected = ref('')          // 选中的参考图 id
const imgV = ref(Date.now() % 100000)
const fileInput = ref(null)
const uploading = ref(false)
const checked = ref({})           // 看板模式：action_id -> bool
const applying = ref(false)

// ---- 动作首帧总览 ----
const ffV = ref({})               // action_id -> 缓存戳（重生成/上传后刷新图）
const localFf = ref({})           // action_id -> true（本弹窗内新产生的首帧）
const localKind = ref({})         // action_id -> 来源 kind（本弹窗内更新）
const sets = ref([])
const setId = ref('')
const gen = ref({})               // action_id -> {job_id, message}
const preview = ref(null)         // 预览中的动作
const ffUpload = ref(null)        // 单动作上传 input
const ffUploadTarget = ref(null)

const KIND_TXT = { ai_generated: 'AI 生成', batch_import: '导入', sprite_ref: '图库',
                   upload: '上传', action_frame: '取帧', legacy_session: '旧会话' }

function hasFf(a) { return !!(localFf.value[a.id] || a.summary?.has_first_frame) }
function kindOf(a) { return localKind.value[a.id] || a.first_frame?.kind || '' }
const ffCount = computed(() => props.actions.filter(hasFf).length)

async function load() {
  loading.value = true
  try {
    const r = await api.spriteRefs(props.spriteId)
    refs.value = r.refs
    if (!selected.value && refs.value.length) selected.value = refs.value[0].id
  } finally {
    loading.value = false
  }
}

async function loadSets() {
  try {
    const s = await api.ffsets()
    sets.value = s.sets
    if (!setId.value && sets.value.length) setId.value = sets.value[0].id
  } catch { /* 无参考集则 AI 生成不可用 */ }
}

// ---- 参考图库操作 ----
async function onFiles(files) {
  if (!files?.length) return
  uploading.value = true
  let ok = 0, dup = 0
  const before = new Set(refs.value.map(r => r.id))
  for (const f of files) {
    try {
      const rec = await api.uploadSpriteRef(props.spriteId, f)
      before.has(rec.id) ? dup++ : ok++
      selected.value = rec.id
    } catch (e) {
      toast(`${f.name}: ${e.message}`)
    }
  }
  uploading.value = false
  if (fileInput.value) fileInput.value.value = ''
  changed.value = true
  await load()
  toast(dup ? `入库 ${ok} 张，${dup} 张已存在（内容相同）` : `已入库 ${ok} 张`)
}

const ROLE_TXT = { front: '正面立绘', back: '背面立绘', '': '' }
async function cycleRole(r) {
  const next = { '': 'front', front: 'back', back: '' }[r.role || '']
  await api.patchSpriteRef(props.spriteId, r.id, { role: next })
  r.role = next
  changed.value = true          // 看板摘要条的立绘状态要随之刷新
  toast(next ? `已标记为${ROLE_TXT[next]}` : '已取消标记')
}

async function removeRef(r) {
  if (!(await askConfirm(`删除参考图「${r.name}」？已应用到动作的首帧不受影响。`, { danger: true }))) return
  await api.deleteSpriteRef(props.spriteId, r.id)
  if (selected.value === r.id) selected.value = ''
  changed.value = true
  await load()
}

const checkedIds = computed(() =>
  props.actions.filter(a => checked.value[a.id]).map(a => a.id))

function selectNoFf() { for (const a of props.actions) checked.value[a.id] = !hasFf(a) }
function selectNone() { for (const a of props.actions) checked.value[a.id] = false }

async function apply() {
  if (!selected.value) return toast('请先选择一张参考图')
  const ids = props.currentActionId ? [props.currentActionId] : checkedIds.value
  if (!ids.length) return toast('请勾选要应用的动作')
  if (!props.currentActionId) {
    const overwrite = props.actions.filter(a => checked.value[a.id] && hasFf(a))
    if (overwrite.length &&
        !(await askConfirm(`其中 ${overwrite.length} 个动作已有首帧，将被覆盖。继续？`))) return
  }
  applying.value = true
  try {
    const r = await api.applySpriteRef(props.spriteId, selected.value, ids)
    for (const aid of r.applied) {
      localFf.value[aid] = true
      localKind.value[aid] = 'sprite_ref'
      ffV.value[aid] = Date.now()
    }
    changed.value = true
    toast(r.skipped?.length
      ? `已应用 ${r.applied.length} 个，跳过 ${r.skipped.length} 个`
      : `已应用到 ${r.applied.length} 个动作`)
    if (props.currentActionId) { emit('applied'); emit('close') }
    else { selectNone(); tab.value = 'overview' }
  } catch (e) {
    toast(`应用失败: ${e.message}`)
  } finally {
    applying.value = false
  }
}

// ---- 动作首帧总览操作 ----
function useRefFor(a) {
  selectNone()
  checked.value[a.id] = true
  tab.value = 'refs'
}

async function genOne(a) {
  if (!setId.value) return toast('还没有参考首帧集——先到精灵库「参考首帧库」导入或归档')
  if (hasFf(a) && !(await askConfirm(`重新生成「${a.name}」的首帧？当前首帧将被覆盖（生图按张计费）。`))) return
  try {
    const r = await api.genFirstFrame(props.spriteId, a.id, { set_id: setId.value })
    gen.value[a.id] = { job_id: r.job_id, message: '排队中' }
    startPolling()
  } catch (e) {
    toast(`提交失败: ${e.message}`)
  }
}

let timer = null
function startPolling() { if (!timer) { timer = setInterval(poll, 2000); poll() } }
function stopPolling() { if (timer) { clearInterval(timer); timer = null } }
onUnmounted(stopPolling)

async function poll() {
  const pending = Object.entries(gen.value)
  if (!pending.length) { stopPolling(); return }
  for (const [aid, g] of pending) {
    try {
      const j = await api.job(g.job_id)
      g.message = j.message || j.status
      if (j.status === 'done') {
        delete gen.value[aid]
        localFf.value[aid] = true
        localKind.value[aid] = 'ai_generated'
        ffV.value[aid] = Date.now()
        changed.value = true
      } else if (j.status === 'error' || j.status === 'cancelled') {
        delete gen.value[aid]
        toast(`首帧生成失败: ${(j.error || '').split('\n')[0].slice(0, 80)}`)
      }
    } catch (e) {
      if (e.status === 404) { delete gen.value[aid]; toast('任务记录不存在（服务可能已重启）') }
    }
  }
  if (!Object.keys(gen.value).length) stopPolling()
}

function pickUpload(a) {
  ffUploadTarget.value = a
  ffUpload.value?.click()
}

// 把动作现有首帧设为精灵的正面/背面立绘（入参考图库并标记，同朝向排他）
async function setAsArt(a, role) {
  try {
    await api.actionFirstFrameAsRef(props.spriteId, a.id, role)
    changed.value = true
    await load()
    toast(`已把「${a.name}」的首帧设为${role === 'front' ? '正面' : '背面'}立绘`)
  } catch (e) {
    toast(`设置失败: ${e.message}`)
  }
}
const artOf = computed(() => ({
  front: refs.value.find(r => r.role === 'front')?.name,
  back: refs.value.find(r => r.role === 'back')?.name,
}))

async function onFfUpload(file) {
  const a = ffUploadTarget.value
  if (!file || !a) return
  try {
    await api.uploadFirstFrame(a.id, file)
    localFf.value[a.id] = true
    localKind.value[a.id] = 'upload'
    ffV.value[a.id] = Date.now()
    changed.value = true
    toast(`「${a.name}」首帧已更新（已同步入参考图库）`)
    await load()
  } catch (e) {
    toast(`上传失败: ${e.message}`)
  } finally {
    if (ffUpload.value) ffUpload.value.value = ''
    ffUploadTarget.value = null
  }
}

function close() {
  emit('close')
  if (changed.value) emit('applied')
}

onMounted(async () => {
  if (!boardMode.value) tab.value = 'refs'
  await Promise.all([load(), boardMode.value ? loadSets() : Promise.resolve()])
})
</script>

<template>
  <div class="modal-mask" @click.self="close">
    <div class="modal" :class="{ wide: boardMode }">
      <div class="modal-head">
        <h3 v-if="boardMode">首帧图库</h3>
        <h3 v-else>首帧参考图库 <span class="count" v-if="!loading">{{ refs.length }}</span></h3>
        <div v-if="boardMode" class="tabs">
          <button class="tab" :class="{ on: tab === 'overview' }" @click="tab = 'overview'">
            动作首帧总览 <span class="count">{{ ffCount }}/{{ props.actions.length }}</span></button>
          <button class="tab" :class="{ on: tab === 'refs' }" @click="tab = 'refs'">
            参考图库 <span class="count">{{ refs.length }}</span></button>
        </div>
        <span class="spacer"></span>
        <template v-if="tab === 'overview'">
          <div class="field inline"><label>AI 生成用参考集</label>
            <select v-model="setId" style="max-width:190px">
              <option v-if="!sets.length" value="">（无参考首帧集）</option>
              <option v-for="s in sets" :key="s.id" :value="s.id">
                {{ s.name }}（{{ s.frames.length }}）</option>
            </select></div>
        </template>
        <button v-else class="small" :disabled="uploading" @click="fileInput.click()">
          {{ uploading ? '上传中…' : '上传图片（可多选）' }}</button>
        <button class="small" @click="close">✕ 关闭</button>
        <input ref="fileInput" type="file" accept="image/*" multiple style="display:none"
               @change="e => onFiles([...e.target.files])" />
        <input ref="ffUpload" type="file" accept="image/*" style="display:none"
               @change="e => onFfUpload(e.target.files[0])" />
      </div>

      <!-- ============ 动作首帧总览 ============ -->
      <template v-if="tab === 'overview'">
        <p class="hint" style="margin:0 0 10px">
          每个动作当前的首帧图。可单张「AI 生成/重生成」「上传替换」「用参考图」，
          或把某个动作的首帧「设为正面/背面立绘」（AI 生成首帧的输入）；批量生成请用看板的「生成首帧」。
          <span v-if="artOf.front || artOf.back" class="ok-text">
            当前立绘：正面 {{ artOf.front || '—' }} · 背面 {{ artOf.back || '—' }}</span></p>
        <div class="ff-grid">
          <div v-for="a in props.actions" :key="a.id" class="ff-item" :class="{ missing: !hasFf(a) }">
            <div class="ff-img" @click="hasFf(a) && (preview = a)">
              <img v-if="hasFf(a)" :src="api.actionFirstFrameUrl(props.spriteId, a.id, ffV[a.id] || imgV)"
                   alt="" />
              <span v-else class="ff-empty">缺首帧</span>
              <span v-if="gen[a.id]" class="ff-busy">⏳ {{ gen[a.id].message }}</span>
              <span v-else-if="kindOf(a)" class="ff-kind">{{ KIND_TXT[kindOf(a)] || kindOf(a) }}</span>
            </div>
            <div class="ff-name" :title="a.name">{{ a.name }}</div>
            <div class="ff-ops">
              <button class="small" :disabled="!!gen[a.id] || !sets.length"
                      :title="sets.length ? '' : '先建参考首帧集'" @click="genOne(a)">
                {{ hasFf(a) ? 'AI 重生成' : 'AI 生成' }}</button>
              <button class="small" :disabled="!!gen[a.id]" @click="pickUpload(a)">上传</button>
              <button class="small" :disabled="!!gen[a.id]" title="从参考图库选一张应用到此动作"
                      @click="useRefFor(a)">用参考图</button>
              <button v-if="hasFf(a)" class="small" title="把此动作首帧设为精灵的正面立绘"
                      @click="setAsArt(a, 'front')">设为正面</button>
              <button v-if="hasFf(a)" class="small" title="把此动作首帧设为精灵的背面立绘"
                      @click="setAsArt(a, 'back')">设为背面</button>
            </div>
          </div>
        </div>
        <div class="modal-foot">
          <button @click="close">关闭</button>
        </div>
      </template>

      <!-- ============ 参考图库 ============ -->
      <template v-else>
        <p class="hint" style="margin:0 0 10px">
          精灵级共享图库——一张首帧图可应用到多个动作；点图片左下角可标记 正面/背面立绘（AI 生成首帧的输入）。
          动作里上传的首帧会自动入库（内容去重）。</p>

        <div v-if="loading" class="hint" style="padding:30px;text-align:center">加载中...</div>
        <div v-else-if="!refs.length" class="hint" style="padding:30px;text-align:center">
          图库是空的——点「上传图片」添加角色首帧参考图。</div>

        <div v-else class="ref-grid">
          <div v-for="r in refs" :key="r.id" class="ref-item"
               :class="{ sel: selected === r.id }" @click="selected = r.id">
            <img :src="api.spriteRefImageUrl(props.spriteId, r.id, imgV)" alt="" />
            <div class="ref-name" :title="r.name">{{ r.name }}</div>
            <button class="small danger ref-del" @click.stop="removeRef(r)" title="删除">✕</button>
            <span v-if="selected === r.id" class="ref-check">✓</span>
            <span class="ref-role" :class="{ marked: r.role }"
                  :title="'立绘朝向标记（AI 生成首帧的输入）：点击切换 正面→背面→无'"
                  @click.stop="cycleRole(r)">{{ ROLE_TXT[r.role || ''] || '标记立绘' }}</span>
          </div>
        </div>

        <!-- 看板模式：勾选动作 -->
        <template v-if="boardMode">
          <div class="row" style="gap:8px;margin:10px 0 6px;align-items:center">
            <b style="font-size:13px">应用到动作</b>
            <button class="small" @click="selectNoFf">选缺首帧的</button>
            <button class="small" @click="selectNone">全不选</button>
            <span class="hint">已勾选 {{ checkedIds.length }} 个</span>
          </div>
          <div class="act-list">
            <label v-for="a in props.actions" :key="a.id" class="act-row">
              <input type="checkbox" v-model="checked[a.id]" />
              <b>{{ a.name }}</b>
              <span class="spacer" style="flex:1"></span>
              <span v-if="hasFf(a)" class="hint">已有首帧（将覆盖）</span>
              <span v-else class="warn-text">缺首帧</span>
            </label>
          </div>
        </template>

        <div class="modal-foot">
          <button class="primary" :disabled="applying || !selected" @click="apply">
            {{ applying ? '应用中…'
               : props.currentActionId ? '设为当前动作首帧'
               : `应用到选中动作（${checkedIds.length}）` }}</button>
          <button @click="close">{{ boardMode ? '关闭' : '取消' }}</button>
        </div>
      </template>
    </div>
  </div>

  <!-- 首帧大图预览 -->
  <div v-if="preview" class="modal-mask inner" @click.self="preview = null">
    <div class="prev-box">
      <div class="modal-head" style="margin-bottom:8px">
        <b>{{ preview.name }}</b>
        <span class="hint">{{ KIND_TXT[kindOf(preview)] || '' }}</span>
        <span class="spacer"></span>
        <button class="small" @click="preview = null">✕ 关闭</button>
      </div>
      <img :src="api.actionFirstFrameUrl(props.spriteId, preview.id, ffV[preview.id] || imgV)" alt="" />
    </div>
  </div>
</template>

<style scoped>
.modal-mask {
  position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 92;
  display: flex; align-items: center; justify-content: center;
}
.modal-mask.inner { z-index: 96; }
.modal {
  width: 680px; max-width: 94vw; max-height: 88vh; overflow-y: auto;
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 20px 18px;
}
.modal.wide { width: 900px; }
.modal-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }
.modal-head h3 { margin: 0; font-size: 16px; }
.count { font-size: 12px; color: var(--text-dim); font-weight: 400; }
.spacer { flex: 1; }
.tabs { display: flex; gap: 2px; margin-left: 8px; }
.tab {
  background: transparent; border: 1px solid transparent; border-bottom: 2px solid transparent;
  border-radius: 4px 4px 0 0; padding: 4px 12px; font-size: 13px; color: var(--text-dim);
}
.tab.on { color: var(--text); border-bottom-color: var(--accent); background: var(--bg-input); }

/* 动作首帧总览 */
.ff-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; }
.ff-item {
  border: 1px solid var(--border); border-radius: 6px; overflow: hidden;
  background: var(--bg-input); transition: border-color .12s;
}
.ff-item:hover { border-color: var(--accent); }
.ff-item.missing { border-style: dashed; }
.ff-img {
  position: relative; height: 150px; cursor: pointer; display: flex;
  align-items: center; justify-content: center;
  background: repeating-conic-gradient(#2a2a2a 0% 25%, #202020 0% 50%) 0 0 / 14px 14px;
}
.ff-img img { width: 100%; height: 100%; object-fit: contain; display: block; }
.ff-empty { color: var(--text-dim); font-size: 12px; }
.ff-kind {
  position: absolute; top: 4px; left: 4px; font-size: 10px; padding: 1px 7px;
  border-radius: 8px; background: rgba(0,0,0,.6); color: var(--text-dim);
}
.ff-busy {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  background: rgba(0,0,0,.55); color: var(--warn); font-size: 12px; text-align: center; padding: 6px;
}
.ff-name {
  font-size: 12px; font-weight: 600; padding: 5px 8px 2px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ff-ops { display: flex; gap: 4px; padding: 2px 6px 7px; flex-wrap: wrap; }
.ff-ops button { padding: 2px 7px; font-size: 11px; }
.ok-text { color: var(--ok); }

/* 参考图库 */
.ref-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 10px; }
.ref-item {
  position: relative; border: 2px solid var(--border); border-radius: 6px;
  overflow: hidden; cursor: pointer; background:
  repeating-conic-gradient(#2a2a2a 0% 25%, #202020 0% 50%) 0 0 / 14px 14px;
  transition: border-color .12s;
}
.ref-item:hover { border-color: var(--text-dim); }
.ref-item.sel { border-color: var(--accent); }
.ref-item img { width: 100%; height: 110px; object-fit: contain; display: block; }
.ref-name {
  font-size: 11px; color: var(--text-dim); padding: 3px 6px; background: var(--bg-input);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ref-del { position: absolute; top: 4px; right: 4px; opacity: 0; transition: opacity .12s; }
.ref-item:hover .ref-del { opacity: 1; }
.ref-check {
  position: absolute; top: 4px; left: 4px; width: 20px; height: 20px; border-radius: 50%;
  background: var(--accent); color: #fff; font-size: 12px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.ref-role {
  position: absolute; bottom: 24px; left: 4px; font-size: 10px; padding: 1px 7px;
  border-radius: 8px; background: rgba(0,0,0,.6); color: var(--text-dim);
  cursor: pointer; opacity: .75; transition: opacity .12s; border: 1px dashed transparent;
}
.ref-item:hover .ref-role { opacity: 1; border-color: var(--text-dim); }
.ref-role.marked { opacity: 1; background: #1e88e5cc; color: #fff; border-color: transparent; }
.act-list {
  border: 1px solid var(--border); border-radius: 5px; max-height: 220px; overflow-y: auto;
  margin-bottom: 10px;
}
.act-row {
  display: flex; align-items: center; gap: 10px; padding: 5px 12px;
  border-bottom: 1px solid var(--border); font-size: 13px; cursor: pointer;
}
.act-row:last-child { border-bottom: none; }
.act-row:hover { background: var(--bg-hover); }
.warn-text { color: var(--warn); font-size: 12px; }
.modal-foot { display: flex; gap: 10px; justify-content: flex-end; margin-top: 12px; }
.danger { border-color: var(--err); color: var(--err); }

.prev-box {
  width: 640px; max-width: 92vw; background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px;
}
.prev-box img {
  width: 100%; max-height: 72vh; object-fit: contain; display: block; border-radius: 4px;
  background: repeating-conic-gradient(#2a2a2a 0% 25%, #202020 0% 50%) 0 0 / 16px 16px;
}
</style>
