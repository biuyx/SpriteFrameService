<script setup>
import { ref, computed, onMounted } from 'vue'
import { toast, askConfirm } from '../stores'
import api from '../api'

// currentActionId：工作台模式（应用到当前动作）
// actions：看板模式（勾选多个动作批量应用；元素含 id/name/summary）
const props = defineProps({
  spriteId: { type: String, required: true },
  currentActionId: { type: String, default: null },
  actions: { type: Array, default: () => [] },
})
const emit = defineEmits(['close', 'applied'])

const refs = ref([])
const loading = ref(true)
const selected = ref('')          // 选中的参考图 id
const imgV = ref(Date.now() % 100000)
const fileInput = ref(null)
const uploading = ref(false)
const checked = ref({})           // 看板模式：action_id -> bool
const applying = ref(false)

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
  await load()
  toast(dup ? `入库 ${ok} 张，${dup} 张已存在（内容相同）` : `已入库 ${ok} 张`)
}

// 立绘朝向标记：正面/背面立绘是首帧生图的必要输入（点击循环切换）
const ROLE_TXT = { front: '正面立绘', back: '背面立绘', '': '' }
async function cycleRole(r) {
  const next = { '': 'front', front: 'back', back: '' }[r.role || '']
  await api.patchSpriteRef(props.spriteId, r.id, { role: next })
  r.role = next
  toast(next ? `已标记为${ROLE_TXT[next]}` : '已取消标记')
}

async function removeRef(r) {
  if (!(await askConfirm(`删除参考图「${r.name}」？已应用到动作的首帧不受影响。`, { danger: true }))) return
  await api.deleteSpriteRef(props.spriteId, r.id)
  if (selected.value === r.id) selected.value = ''
  await load()
}

const checkedIds = computed(() =>
  props.actions.filter(a => checked.value[a.id]).map(a => a.id))

function selectNoFf() {
  for (const a of props.actions) checked.value[a.id] = !a.summary?.has_first_frame
}
function selectNone() { for (const a of props.actions) checked.value[a.id] = false }

async function apply() {
  if (!selected.value) return toast('请先选择一张参考图')
  const ids = props.currentActionId ? [props.currentActionId] : checkedIds.value
  if (!ids.length) return toast('请勾选要应用的动作')
  // 覆盖已有首帧时提醒
  if (!props.currentActionId) {
    const overwrite = props.actions.filter(a => checked.value[a.id] && a.summary?.has_first_frame)
    if (overwrite.length &&
        !(await askConfirm(`其中 ${overwrite.length} 个动作已有首帧，将被覆盖。继续？`))) return
  }
  applying.value = true
  try {
    const r = await api.applySpriteRef(props.spriteId, selected.value, ids)
    toast(r.skipped?.length
      ? `已应用 ${r.applied.length} 个，跳过 ${r.skipped.length} 个`
      : `已应用到 ${r.applied.length} 个动作`)
    emit('applied')
    emit('close')
  } catch (e) {
    toast(`应用失败: ${e.message}`)
  } finally {
    applying.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <div class="modal-head">
        <h3>首帧参考图库 <span class="count" v-if="!loading">{{ refs.length }}</span></h3>
        <span class="spacer"></span>
        <button class="small" :disabled="uploading" @click="fileInput.click()">
          {{ uploading ? '上传中…' : '上传图片（可多选）' }}</button>
        <button class="small" @click="emit('close')">✕ 关闭</button>
        <input ref="fileInput" type="file" accept="image/*" multiple style="display:none"
               @change="e => onFiles([...e.target.files])" />
      </div>
      <p class="hint" style="margin:0 0 10px">
        精灵级共享图库——一张首帧图可应用到多个动作。动作里上传的首帧会自动入库（内容去重）。</p>

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
      <template v-if="!props.currentActionId && props.actions.length">
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
            <span v-if="a.summary?.has_first_frame" class="hint">已有首帧（将覆盖）</span>
            <span v-else class="warn-text">缺首帧</span>
          </label>
        </div>
      </template>

      <div class="modal-foot">
        <button class="primary" :disabled="applying || !selected"
                @click="apply">
          {{ applying ? '应用中…'
             : props.currentActionId ? '设为当前动作首帧'
             : `应用到选中动作（${checkedIds.length}）` }}</button>
        <button @click="emit('close')">取消</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-mask {
  position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 92;
  display: flex; align-items: center; justify-content: center;
}
.modal {
  width: 680px; max-width: 94vw; max-height: 86vh; overflow-y: auto;
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 20px 18px;
}
.modal-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.modal-head h3 { margin: 0; font-size: 16px; }
.count { font-size: 13px; color: var(--text-dim); font-weight: 400; }
.spacer { flex: 1; }

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
  border-radius: 8px; background: rgba(0,0,0,.55); color: var(--text-dim);
  cursor: pointer; opacity: 0; transition: opacity .12s;
}
.ref-item:hover .ref-role { opacity: 1; }
.ref-role.marked { opacity: 1; background: #1e88e5cc; color: #fff; }
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
</style>
