<script setup>
import { ref } from 'vue'
import { toast } from '../stores'
import api from '../api'

const emit = defineEmits(['close', 'done'])

const framesDir = ref('')
const templatesDir = ref('')
const scanning = ref(false)
const importing = ref(false)
const preview = ref(null)          // batch-scan 结果
const checked = ref({})            // dir_name -> bool
const result = ref(null)           // batch-import 结果

async function scan() {
  if (!framesDir.value.trim()) return toast('请填写角色首帧根目录')
  scanning.value = true
  result.value = null
  try {
    preview.value = await api.batchScan(framesDir.value.trim(), templatesDir.value.trim() || null)
    checked.value = {}
    for (const s of preview.value.sprites) checked.value[s.dir_name] = true
  } catch (e) {
    preview.value = null
    toast(`扫描失败: ${e.message}`)
  } finally {
    scanning.value = false
  }
}

const totalActions = () =>
  (preview.value?.sprites || []).filter(s => checked.value[s.dir_name])
    .reduce((n, s) => n + s.actions.length, 0)

async function runImport() {
  const sprites = (preview.value?.sprites || [])
    .filter(s => checked.value[s.dir_name])
    .map(s => ({ dir_name: s.dir_name, actions: s.actions.map(a => a.key) }))
  if (!sprites.length) return toast('请至少勾选一个角色')
  importing.value = true
  try {
    result.value = await api.batchImport({
      frames_dir: framesDir.value.trim(),
      templates_dir: templatesDir.value.trim() || null,
      sprites,
    })
    toast('批量导入完成')
    emit('done')
  } catch (e) {
    toast(`导入失败: ${e.message}`)
  } finally {
    importing.value = false
  }
}
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <div class="modal-head">
        <h3>批量导入（角色 × 动作）</h3>
        <button class="small" @click="emit('close')">✕</button>
      </div>

      <div class="set-group">
        <label class="set-label">角色首帧根目录（每个子目录 = 一个角色，图片名 = 动作）</label>
        <input v-model="framesDir" style="width:100%"
               placeholder="例如 D:\JAVA\kozy\art\批量动作\角色动作首帧\deliverable" />
      </div>
      <div class="set-group">
        <label class="set-label">动作模板视频目录（可选，导入为全局模板并按动作名自动关联）</label>
        <input v-model="templatesDir" style="width:100%"
               placeholder="例如 D:\JAVA\kozy\art\批量动作\动作模板2.0" />
      </div>
      <div class="set-row">
        <button class="primary" :disabled="scanning" @click="scan">
          {{ scanning ? '扫描中...' : '扫描预览' }}</button>
      </div>

      <template v-if="preview">
        <div class="scan-summary">
          <span>共 {{ preview.sprites.length }} 个角色</span>
          <span v-if="preview.templates">
            · 模板视频 {{ preview.templates.found.length }} 个（新 {{ preview.templates.new_count }}）</span>
          <span>· 勾选后将建 {{ totalActions() }} 个动作</span>
        </div>
        <div class="sprite-list">
          <label v-for="s in preview.sprites" :key="s.dir_name" class="sprite-row">
            <input type="checkbox" v-model="checked[s.dir_name]" />
            <b>{{ s.dir_name }}</b>
            <span class="hint">{{ s.actions.length }} 个动作</span>
            <span v-if="s.existing_sprite_id" class="warn-text">已存在，仅补缺失动作</span>
          </label>
        </div>
        <div class="modal-foot">
          <button class="primary" :disabled="importing" @click="runImport">
            {{ importing ? '导入中...' : `执行导入（${totalActions()} 个动作）` }}</button>
          <button :disabled="importing" @click="emit('close')">关闭</button>
        </div>
      </template>

      <div v-if="result" class="result-box">
        <p>✅ 精灵：新建 {{ result.sprites_created }} · 复用 {{ result.sprites_reused }}
           ｜ 动作：新建 {{ result.actions_created }} · 跳过 {{ result.actions_skipped }}
           <template v-if="result.templates">｜ 模板：新增 {{ result.templates.imported_count }}</template>
        </p>
        <p v-for="(e, i) in result.errors" :key="i" class="warn-text">⚠ {{ e }}</p>
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
  width: 560px; max-width: 94vw; max-height: 86vh; overflow-y: auto;
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 20px 18px;
}
.modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.modal-head h3 { margin: 0; font-size: 16px; }
.set-group { margin-bottom: 10px; }
.set-label { display: block; font-size: 12px; font-weight: 600; margin-bottom: 5px; }
.set-row { display: flex; gap: 10px; margin-bottom: 10px; }
.scan-summary { font-size: 12px; color: var(--text-dim); margin: 8px 0; display: flex; gap: 6px; flex-wrap: wrap; }
.sprite-list {
  border: 1px solid var(--border); border-radius: 5px; max-height: 260px; overflow-y: auto;
  margin-bottom: 12px;
}
.sprite-row {
  display: flex; align-items: center; gap: 10px; padding: 6px 12px;
  border-bottom: 1px solid var(--border); cursor: pointer; font-size: 13px;
}
.sprite-row:last-child { border-bottom: none; }
.sprite-row:hover { background: var(--bg-hover); }
.warn-text { color: var(--warn); font-size: 11px; }
.modal-foot { display: flex; gap: 10px; justify-content: flex-end; }
.result-box {
  margin-top: 12px; padding: 10px 12px; background: var(--bg-input);
  border: 1px solid var(--border); border-radius: 5px; font-size: 12px;
}
.result-box p { margin: 2px 0; }
</style>
