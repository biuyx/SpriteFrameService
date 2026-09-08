<script setup>
import { ref, onMounted } from 'vue'
import { toast, askConfirm } from '../stores'
import api from '../api'

// 参考首帧库：完成角色的全套动作首帧集合，供新角色首帧生图作姿势参考
const emit = defineEmits(['close'])

const sets = ref([])
const sprites = ref([])
const loading = ref(true)
const preview = ref(null)          // 预览中的参考集

const impDir = ref('')
const impName = ref('')
const impGroup = ref('')
const importing = ref(false)

const archSprite = ref('')
const archiving = ref(false)

async function load() {
  loading.value = true
  try {
    const [s, sp] = await Promise.all([api.ffsets(), api.sprites()])
    sets.value = s.sets
    sprites.value = sp.sprites
    if (!archSprite.value && sprites.value.length) archSprite.value = sprites.value[0].id
  } finally {
    loading.value = false
  }
}

function fmtTime(ts) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

async function runImport() {
  if (!impDir.value.trim()) return toast('请填写首帧图片目录')
  importing.value = true
  try {
    const r = await api.ffsetImportDir(impDir.value.trim(), impName.value.trim(), impGroup.value.trim())
    toast(`已导入参考集「${r.name}」（${r.frames.length} 个动作首帧）`)
    impDir.value = ''
    impName.value = ''
    await load()
  } catch (e) {
    toast(`导入失败: ${e.message}`)
  } finally {
    importing.value = false
  }
}

async function runArchive() {
  if (!archSprite.value) return toast('请选择要归档的精灵')
  archiving.value = true
  try {
    const r = await api.ffsetArchiveSprite(archSprite.value, null, null)
    toast(`已归档「${r.name}」（${r.frames.length} 个动作首帧）`)
    await load()
  } catch (e) {
    toast(`归档失败: ${e.message}`)
  } finally {
    archiving.value = false
  }
}

async function remove(s) {
  if (!(await askConfirm(`删除参考集「${s.name}」（${s.frames.length} 张）？`, { danger: true }))) return
  await api.deleteFfset(s.id)
  toast('已删除')
  await load()
}

onMounted(load)
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <div class="modal-head">
        <h3>参考首帧库 <span class="count" v-if="!loading">{{ sets.length }}</span></h3>
        <span class="spacer"></span>
        <button class="small" @click="emit('close')">✕ 关闭</button>
      </div>
      <p class="hint" style="margin:0 0 10px">
        每个参考集 = 一个完成角色的全套动作首帧。新角色只需正/背面立绘，
        其余动作首帧以参考集为姿势模板由 AI 生成。</p>

      <div class="ops-box">
        <div class="row" style="align-items:center;gap:8px;flex-wrap:wrap">
          <input v-model="impDir" placeholder="目录导入：如 D:\素材\deliverable\artisan" style="flex:1;min-width:220px" />
          <input v-model="impName" placeholder="名称（可空）" style="width:110px" />
          <input v-model="impGroup" placeholder="分组（可空）" style="width:100px" />
          <button class="small" :disabled="importing" @click="runImport">
            {{ importing ? '导入中…' : '目录导入' }}</button>
        </div>
        <div class="row" style="align-items:center;gap:8px;margin-top:6px">
          <span class="hint">或把库内精灵的动作首帧归档为参考集：</span>
          <select v-model="archSprite" style="min-width:140px">
            <option v-for="sp in sprites" :key="sp.id" :value="sp.id">{{ sp.name }}</option>
          </select>
          <button class="small" :disabled="archiving || !sprites.length" @click="runArchive">
            {{ archiving ? '归档中…' : '从精灵归档' }}</button>
        </div>
      </div>

      <div v-if="loading" class="hint" style="padding:26px;text-align:center">加载中...</div>
      <div v-else-if="!sets.length" class="hint" style="padding:26px;text-align:center">
        还没有参考集——目录导入或从精灵归档。</div>

      <table v-else class="set-tbl">
        <thead><tr>
          <th>名称</th><th style="width:100px">分组</th>
          <th style="width:70px">首帧数</th><th style="width:90px">创建</th>
          <th style="width:110px">操作</th>
        </tr></thead>
        <tbody>
          <tr v-for="s in sets" :key="s.id">
            <td><b>{{ s.name }}</b></td>
            <td><span v-if="s.group" class="grp">{{ s.group }}</span><span v-else class="dim">—</span></td>
            <td>{{ s.frames.length }}</td>
            <td class="dim">{{ fmtTime(s.created_at) }}</td>
            <td class="ops">
              <button class="small" @click="preview = s">预览</button>
              <button class="small danger" @click="remove(s)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 参考集预览 -->
  <div v-if="preview" class="modal-mask inner" @click.self="preview = null">
    <div class="prev-box">
      <div class="modal-head">
        <b>{{ preview.name }}（{{ preview.frames.length }} 张）</b>
        <span class="spacer"></span>
        <button class="small" @click="preview = null">✕ 关闭</button>
      </div>
      <div class="prev-grid">
        <div v-for="f in preview.frames" :key="f.key" class="prev-item">
          <img :src="api.ffsetFrameUrl(preview.id, f.key)" alt="" loading="lazy" />
          <div class="prev-key" :title="f.key">{{ f.key }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-mask {
  position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 90;
  display: flex; align-items: center; justify-content: center;
}
.modal-mask.inner { z-index: 95; }
.modal {
  width: 680px; max-width: 94vw; max-height: 86vh; overflow-y: auto;
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 20px 18px;
}
.modal-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.modal-head h3 { margin: 0; font-size: 16px; }
.count { font-size: 13px; color: var(--text-dim); font-weight: 400; }
.spacer { flex: 1; }
.ops-box {
  background: var(--bg-input); border: 1px solid var(--border); border-radius: 6px;
  padding: 10px 12px; margin-bottom: 10px;
}
.set-tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.set-tbl th {
  text-align: left; font-weight: 500; color: var(--text-dim); font-size: 12px;
  padding: 7px 10px; border-bottom: 1px solid var(--border); background: var(--bg-input);
}
.set-tbl td { padding: 7px 10px; border-bottom: 1px solid var(--border); }
.set-tbl tbody tr:last-child td { border-bottom: none; }
.set-tbl tbody tr:hover { background: var(--bg-hover); }
.grp {
  font-size: 11px; padding: 1px 8px; background: var(--bg-input); border-radius: 8px;
  color: var(--text-dim);
}
.dim { color: var(--text-dim); font-size: 12px; }
.ops button { margin-right: 5px; }
.danger { border-color: var(--err); color: var(--err); }

.prev-box {
  width: 760px; max-width: 94vw; max-height: 86vh; overflow-y: auto;
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 14px 16px;
}
.prev-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 8px; }
.prev-item {
  border: 1px solid var(--border); border-radius: 5px; overflow: hidden;
  background: repeating-conic-gradient(#2a2a2a 0% 25%, #202020 0% 50%) 0 0 / 12px 12px;
}
.prev-item img { width: 100%; height: 100px; object-fit: contain; display: block; }
.prev-key {
  font-size: 10px; color: var(--text-dim); padding: 2px 6px; background: var(--bg-input);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
</style>
