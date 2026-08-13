<script setup>
import { ref, onMounted } from 'vue'
import { toast, askConfirm } from '../stores'
import api from '../api'

const emit = defineEmits(['close'])

const loading = ref(true)
const saving = ref(false)
const current = ref(null)      // GET /settings 结果
const newKey = ref('')         // 留空 = 不修改
const concurrent = ref(5)

async function load() {
  loading.value = true
  try {
    current.value = await api.settings()
    concurrent.value = current.value.generate_max_concurrent
  } catch (e) {
    toast(`读取设置失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  try {
    const patch = {}
    if (newKey.value.trim()) patch.ark_api_key = newKey.value.trim()
    if (concurrent.value !== current.value.generate_max_concurrent)
      patch.generate_max_concurrent = concurrent.value
    if (!Object.keys(patch).length) { emit('close'); return }
    current.value = await api.saveSettings(patch)
    newKey.value = ''
    toast('设置已保存并生效')
    emit('close')
  } catch (e) {
    toast(`保存失败: ${e.message}`)
  } finally {
    saving.value = false
  }
}

async function clearKey() {
  if (!(await askConfirm('清除配置文件中的密钥？（若系统环境变量 ARK_API_KEY 存在，将回退使用它）'))) return
  saving.value = true
  try {
    current.value = await api.saveSettings({ ark_api_key: '' })
    toast('已清除')
  } catch (e) {
    toast(`操作失败: ${e.message}`)
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <div class="modal-head">
        <h3>设置</h3>
        <button class="small" @click="emit('close')">✕</button>
      </div>

      <div v-if="loading" class="hint" style="padding:24px;text-align:center">加载中...</div>

      <template v-else-if="current">
        <div class="set-group">
          <label class="set-label">Ark 视频生成密钥</label>
          <div class="set-row">
            <span class="key-now">
              当前：{{ current.ark_api_key_masked || '未配置' }}
              <em v-if="current.ark_api_key_source === 'env'">（来自系统环境变量）</em>
              <em v-else-if="current.ark_api_key_source === 'file'">（来自配置文件）</em>
            </span>
            <button v-if="current.ark_api_key_source === 'file'" class="small"
                    :disabled="saving" @click="clearKey">清除</button>
          </div>
          <input v-model="newKey" type="password" placeholder="输入新密钥（留空则不修改）"
                 autocomplete="off" style="width:100%" />
          <p class="hint">密钥保存到 backend\.env（不入 git、不进安装包），立即生效。</p>
        </div>

        <div class="set-group">
          <label class="set-label">视频生成并发数</label>
          <div class="set-row">
            <input type="number" v-model.number="concurrent" :min="1" :max="10" style="width:80px" />
            <span class="hint">同时进行中的生成任务上限（1~10，默认 5）；超出的任务排队等待</span>
          </div>
        </div>

        <div class="modal-foot">
          <button class="primary" :disabled="saving" @click="save">
            {{ saving ? '保存中...' : '保存' }}</button>
          <button :disabled="saving" @click="emit('close')">取消</button>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.modal-mask {
  position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 90;
  display: flex; align-items: center; justify-content: center;
}
.modal {
  width: 480px; max-width: 94vw; background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: 8px; padding: 16px 20px 18px;
}
.modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.modal-head h3 { margin: 0; font-size: 16px; }
.set-group { margin-bottom: 16px; }
.set-label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.set-row { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.key-now { font-size: 12px; color: var(--text-dim); font-family: Consolas, monospace; }
.key-now em { font-style: normal; color: var(--warn); }
.modal-foot { display: flex; gap: 10px; justify-content: flex-end; margin-top: 8px; }
.hint { font-size: 11px; color: var(--text-dim); margin: 4px 0 0; }
</style>
