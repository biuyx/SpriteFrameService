<script setup>
import { ref, computed, onMounted } from 'vue'
import { toast } from '../stores'
import api from '../api'

const emit = defineEmits(['close', 'imported'])

const sprites = ref([])
const loading = ref(true)

// 导出
const scope = ref('all')           // all | project
const project = ref('')
const includeWork = ref(false)
const exporting = ref(false)

// 导入
const fileInput = ref(null)
const importing = ref(false)
const importResult = ref(null)

const categories = computed(() => {
  const set = new Set()
  for (const sp of sprites.value) for (const t of sp.tags || []) set.add(t)
  return [...set].sort()
})

const exportCount = computed(() => {
  if (scope.value === 'project')
    return sprites.value.filter(sp => (sp.tags || []).includes(project.value)).length
  return sprites.value.length
})

async function load() {
  loading.value = true
  try {
    const r = await api.sprites()
    sprites.value = r.sprites
    if (categories.value.length) project.value = categories.value[0]
  } finally {
    loading.value = false
  }
}

async function doExport() {
  if (!exportCount.value) return toast('没有匹配的精灵可导出')
  exporting.value = true
  try {
    const blob = await api.exportProject({
      project: scope.value === 'project' ? project.value : null,
      include_workdata: includeWork.value,
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, '')
    a.href = url
    a.download = `sprite_project_${scope.value === 'project' ? project.value : '全部'}_${stamp}.zip`
    a.click()
    URL.revokeObjectURL(url)
    toast(`已导出 ${exportCount.value} 个精灵（${(blob.size / 1048576).toFixed(1)}MB）`)
  } catch (e) {
    toast(`导出失败: ${e.message}`)
  } finally {
    exporting.value = false
  }
}

async function doImport(file) {
  if (!file) return
  importing.value = true
  importResult.value = null
  try {
    importResult.value = await api.importProject(file)
    toast('项目包导入完成')
    emit('imported')
    await load()
  } catch (e) {
    toast(`导入失败: ${e.message}`)
  } finally {
    importing.value = false
    if (fileInput.value) fileInput.value.value = ''
  }
}

onMounted(load)
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <div class="modal-head">
        <h3>项目包 导出 / 导入</h3>
        <span class="spacer"></span>
        <button class="small" @click="emit('close')">✕ 关闭</button>
      </div>

      <div class="sec">
        <h4>导出（分享给其他人）</h4>
        <div class="row" style="align-items:center;gap:12px;flex-wrap:wrap">
          <label class="radio"><input type="radio" value="all" v-model="scope" /> 全部精灵（{{ sprites.length }}）</label>
          <label class="radio" :class="{ off: !categories.length }">
            <input type="radio" value="project" v-model="scope" :disabled="!categories.length" /> 按项目分类</label>
          <select v-if="scope === 'project'" v-model="project">
            <option v-for="c in categories" :key="c" :value="c">{{ c }}</option>
          </select>
        </div>
        <label class="radio" style="margin-top:8px">
          <input type="checkbox" v-model="includeWork" />
          包含工作数据（帧 / 抠图 / 生成视频 / 导出，体积可能很大）</label>
        <p class="hint" style="margin:6px 0 8px">
          默认精简包：精灵档案 + 首帧 + 参考图库 + 引用到的动作模板。对方导入后即可直接批量生成。</p>
        <button class="primary" :disabled="exporting || !exportCount" @click="doExport">
          {{ exporting ? '打包中…' : `导出（${exportCount} 个精灵）` }}</button>
      </div>

      <div class="sec">
        <h4>导入（来自别人的项目包）</h4>
        <p class="hint" style="margin:0 0 8px">
          模板按 key+变体 合并去重，动作绑定自动对齐；已存在的同一精灵（同 id）会跳过。</p>
        <button class="small" :disabled="importing" @click="fileInput.click()">
          {{ importing ? '导入中…' : '选择项目包 (.zip)' }}</button>
        <input ref="fileInput" type="file" accept=".zip,application/zip" style="display:none"
               @change="e => doImport(e.target.files[0])" />
        <div v-if="importResult" class="result-box">
          <p>✅ 精灵：导入 {{ importResult.sprites_imported }} · 跳过 {{ importResult.sprites_skipped }}
             ｜ 动作 {{ importResult.actions_imported }}
             ｜ 模板：新增 {{ importResult.templates_imported }} · 复用 {{ importResult.templates_reused }}</p>
          <p v-for="(e, i) in importResult.errors" :key="i" class="warn-text">⚠ {{ e }}</p>
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
.modal {
  width: 520px; max-width: 94vw; max-height: 86vh; overflow-y: auto;
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 20px 18px;
}
.modal-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.modal-head h3 { margin: 0; font-size: 16px; }
.spacer { flex: 1; }
.sec {
  border: 1px solid var(--border); border-radius: 6px; padding: 12px 14px;
  margin-bottom: 12px; background: var(--bg-input);
}
.sec h4 { margin: 0 0 8px; font-size: 13px; }
.radio { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text); cursor: pointer; }
.radio.off { opacity: .5; cursor: default; }
.result-box {
  margin-top: 10px; padding: 8px 10px; background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: 5px; font-size: 12px;
}
.result-box p { margin: 2px 0; }
.warn-text { color: var(--warn); }
</style>
