<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import api from '../api'
import { useStore, toast } from '../stores'
const props = defineProps({ actions: { type: Array, default: () => [] } })
const emit = defineEmits(['close', 'saved'])
const store = useStore()
const group = ref(''), setId = ref(''), shared = ref([])
const sets = ref([]), templates = ref([]), sprites = ref([]), source = ref('')
const loading = ref(true), busy = ref(false), error = ref('')
const matching = computed(() => sets.value.filter(s => (s.group || '').trim() === group.value.trim()))
const foreign = computed(() => templates.value.filter(t => (t.group || '').trim() !== group.value.trim()))
const groups = computed(() => [...new Set([...sets.value, ...templates.value].map(x => x.group).filter(Boolean))])
const byId = computed(() => Object.fromEntries(templates.value.map(t => [t.id, t])))
watch(group, () => { if (!matching.value.some(s => s.id === setId.value)) setId.value = '' })
onMounted(async () => {
  try {
    const [sp, fs, ts, list] = await Promise.all([api.sprite(store.currentSprite.id), api.ffsets(), api.templates(), api.sprites()])
    sets.value = fs.sets; templates.value = ts.templates; sprites.value = list.sprites
    const b = sp.template_binding || {}
    group.value = b.group || ''; shared.value = b.shared_template_ids || []; setId.value = b.first_frame_set_id || ''
    source.value = store.currentSprite.id
  } catch (e) { error.value = e.message } finally { loading.value = false }
})
async function archive() {
  if (!group.value.trim()) { error.value = '请先填写角色主分组'; return }
  busy.value = true; error.value = ''
  try {
    const fs = await api.ffsetArchiveSprite(source.value, null, group.value.trim())
    sets.value = (await api.ffsets()).sets; setId.value = fs.id
    toast('参考集已归档，保存后绑定到当前角色')
  } catch (e) { error.value = e.message } finally { busy.value = false }
}
async function save() {
  if (!group.value.trim()) { error.value = '请选择角色主分组'; return }
  busy.value = true; error.value = ''
  try {
    const sp = await api.patchSprite(store.currentSprite.id, { template_binding: {
      group: group.value.trim(), first_frame_set_id: setId.value || null,
      shared_template_ids: shared.value.filter(id => foreign.value.some(t => t.id === id)),
    } })
    store.currentSprite = { ...store.currentSprite, template_binding: sp.template_binding }
    toast('模板绑定已保存'); emit('saved'); emit('close')
  } catch (e) { error.value = e.message } finally { busy.value = false }
}
</script>
<template>
  <div class="modal-backdrop" @click.self="emit('close')"><section class="modal binding-modal">
    <h3>角色模板绑定 · {{ store.currentSprite.name }}</h3>
    <p>固定本角色的首帧集，主分组以外的参考动作须逐项授权共享。</p>
    <p v-if="loading">正在加载…</p>
    <template v-else>
      <label>角色主分组 <input v-model="group" list="binding-groups" placeholder="例如：店长" /></label>
      <datalist id="binding-groups"><option v-for="g in groups" :key="g" :value="g" /></datalist>
      <label>固定参考首帧集 <select v-model="setId"><option value="">尚未绑定（首帧生成将被阻止）</option>
        <option v-for="s in matching" :key="s.id" :value="s.id">{{ s.name }} · {{ s.frames.length }} 个动作</option>
      </select></label>
      <div class="binding-row"><select v-model="source"><option v-for="s in sprites" :key="s.id" :value="s.id">{{ s.name }}</option></select>
        <button :disabled="busy || !group.trim()" @click="archive">将该角色首帧归档到本组</button></div>
      <details open><summary>跨组共享参考动作（{{ shared.length }} 项）</summary>
        <label v-for="t in foreign" :key="t.id"><input v-model="shared" type="checkbox" :value="t.id" />{{ t.group || '未分组' }} / {{ t.key }} {{ t.variant || '' }}</label>
      </details>
      <details><summary>本角色动作与参考绑定</summary>
        <p v-for="a in props.actions" :key="a.id">{{ a.name }} → {{ byId[a.template_id]?.group || '未分组' }} / {{ byId[a.template_id]?.variant || byId[a.template_id]?.key || '未绑定' }}</p>
      </details>
      <p>已有动作使用其他分组时，保存前须明确勾选授权。已有素材不会被替换。</p>
    </template>
    <p v-if="error" class="binding-error">{{ error }}</p>
    <footer><button :disabled="busy" @click="emit('close')">关闭</button><button class="primary" :disabled="loading || busy" @click="save">保存绑定</button></footer>
  </section></div>
</template>
<style scoped>
.modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 100; display: flex; align-items: center; justify-content: center; }
.binding-modal { width: min(780px, 94vw); max-height: 88vh; overflow: auto; padding: 22px; background: var(--bg-panel); border: 1px solid var(--border); border-radius: 8px; }
label, .binding-row, footer { display: flex; gap: 10px; align-items: center; margin: 14px 0; }
footer { justify-content: flex-end; } details { margin: 16px 0; }
.binding-error { color: #ffb0a0; white-space: pre-wrap; }
</style>
