<script setup>
import { ref } from 'vue'
import { useStore, refreshFrames, toast, askConfirm } from '../stores'
import api from '../api'

// 帧包导出/导入：把当前动作的帧打包给外部工具（PS/批处理）加工，再按序号导回
const emit = defineEmits(['close'])
const store = useStore()

const expType = ref('raw')         // raw 原图 | processed 处理图
const exporting = ref(false)
const impMode = ref('processed')   // processed 回填处理图 | replace 整组替换原始帧
const importing = ref(false)
const result = ref(null)
const fileInput = ref(null)

async function doExport() {
  exporting.value = true
  try {
    const blob = await api.exportFramesZip(store.sessionId, expType.value)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${store.currentAction?.name || 'frames'}_${expType.value}_帧包.zip`
    a.click()
    URL.revokeObjectURL(url)
    toast(`已导出（${(blob.size / 1048576).toFixed(1)}MB）`)
  } catch (e) {
    toast(`导出失败: ${e.message}`)
  } finally {
    exporting.value = false
  }
}

async function doImport(file) {
  if (!file) return
  if (impMode.value === 'replace' &&
      !(await askConfirm(`整组替换将删除当前全部 ${store.frameCount} 帧（含处理图），以包内图片作为新的原始帧。继续？`,
                         { danger: true }))) {
    if (fileInput.value) fileInput.value.value = ''
    return
  }
  importing.value = true
  result.value = null
  try {
    result.value = await api.importFramesZip(store.sessionId, file, impMode.value)
    await refreshFrames()
    toast(impMode.value === 'processed'
      ? `已回填 ${result.value.updated} 帧处理图`
      : `已替换为 ${result.value.imported} 帧`)
  } catch (e) {
    toast(`导入失败: ${e.message}`)
  } finally {
    importing.value = false
    if (fileInput.value) fileInput.value.value = ''
  }
}
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <div class="modal-head">
        <h3>帧包 导出 / 导入（{{ store.currentAction?.name }}）</h3>
        <span class="spacer"></span>
        <button class="small" @click="emit('close')">✕ 关闭</button>
      </div>

      <div class="sec">
        <h4>导出（给外部工具加工）</h4>
        <div class="row" style="align-items:center;gap:12px">
          <label class="radio"><input type="radio" value="raw" v-model="expType" /> 原图</label>
          <label class="radio"><input type="radio" value="processed" v-model="expType" /> 处理图（仅含已抠图的帧）</label>
          <button class="primary small" :disabled="exporting || !store.frameCount" @click="doExport">
            {{ exporting ? '打包中…' : `下载 zip（${store.frameCount} 帧）` }}</button>
        </div>
        <p class="hint" style="margin:6px 0 0">
          包内文件按帧序号命名（0000.png…），外部加工时保持文件名不变即可导回。</p>
      </div>

      <div class="sec">
        <h4>导入（加工完导回）</h4>
        <div class="row" style="align-items:center;gap:12px;flex-wrap:wrap">
          <label class="radio"><input type="radio" value="processed" v-model="impMode" />
            回填为处理图（按序号匹配现有帧）</label>
          <label class="radio"><input type="radio" value="replace" v-model="impMode" />
            整组替换原始帧</label>
          <button class="small" :disabled="importing" @click="fileInput.click()">
            {{ importing ? '导入中…' : '选择帧包 (.zip)' }}</button>
          <input ref="fileInput" type="file" accept=".zip,application/zip" style="display:none"
                 @change="e => doImport(e.target.files[0])" />
        </div>
        <p class="hint" style="margin:6px 0 0">
          回填模式：外部抠图/修图后的结果作为对应帧的处理图，直接进入导出工序；缺少的序号保持原样。</p>
        <div v-if="result" class="result-box">
          <template v-if="result.mode === 'processed'">
            ✅ 回填 {{ result.updated }} 帧
            <span v-if="result.missing">｜{{ result.missing }} 个序号无对应帧</span>
            <span v-if="result.bad">｜{{ result.bad }} 张无法解码</span>
          </template>
          <template v-else>
            ✅ 已替换为 {{ result.imported }} 帧
            <span v-if="result.bad">｜{{ result.bad }} 张无法解码</span>
          </template>
          <span v-if="result.ignored">｜忽略 {{ result.ignored }} 个非帧文件</span>
        </div>
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
  width: 560px; max-width: 94vw; background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: 8px; padding: 16px 20px 18px;
}
.modal-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.modal-head h3 { margin: 0; font-size: 15px; }
.spacer { flex: 1; }
.sec {
  border: 1px solid var(--border); border-radius: 6px; padding: 12px 14px;
  margin-bottom: 12px; background: var(--bg-input);
}
.sec h4 { margin: 0 0 8px; font-size: 13px; }
.radio { display: flex; align-items: center; gap: 5px; font-size: 13px; color: var(--text); cursor: pointer; }
.result-box {
  margin-top: 8px; padding: 8px 10px; background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: 5px; font-size: 12px;
}
</style>
