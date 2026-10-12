<script setup>
// 「选本机文件夹 / 传 zip」→ 上传到服务器暂存区 → emit('done', { dir, label, files, ... })
// 拿到的 dir 是服务器上的目录，直接填给原来那几个「按目录导入」的接口。
import { ref, computed } from 'vue'
import { toast } from '../stores'
import api from '../api'
import { IMAGE_EXTS, VIDEO_EXTS } from '../fileTypes'

const props = defineProps({
  label: { type: String, default: '选择文件夹' },
  // 只传这些扩展名的文件：文件夹里常混着 PSD、源文件，没必要整个传上去
  accept: { type: Array, default: () => [...IMAGE_EXTS, ...VIDEO_EXTS] },
  disabled: { type: Boolean, default: false },
  zip: { type: Boolean, default: true },
})
const emit = defineEmits(['done'])

const dirInput = ref(null)
const zipInput = ref(null)
const progress = ref(null)        // null = 空闲；0~1 = 上传中
const busy = computed(() => progress.value !== null)

function fmtMB(n) { return (n / 1024 / 1024).toFixed(n > 100 * 1024 * 1024 ? 0 : 1) + ' MB' }

async function send(files, label) {
  progress.value = 0
  try {
    const r = await api.uploadStaging(files, (p) => { progress.value = p })
    emit('done', { ...r, label: r.name || label })
  } catch (e) {
    toast(`上传失败: ${e.message}`)
  } finally {
    progress.value = null
  }
}

async function onDir(ev) {
  const all = [...(ev.target.files || [])]
  ev.target.value = ''               // 同一个文件夹选第二次也要触发
  if (!all.length) return
  const keep = all.filter((f) => props.accept.some((x) => f.name.toLowerCase().endsWith(x)))
  const folder = (all[0].webkitRelativePath || '').split('/')[0] || '文件夹'
  if (!keep.length) {
    return toast(`「${folder}」里没有可导入的文件（只认 ${props.accept.join(' ')}）`)
  }
  const size = keep.reduce((s, f) => s + f.size, 0)
  if (keep.length < all.length) {
    toast(`「${folder}」：上传 ${keep.length} 个文件（${fmtMB(size)}），`
          + `跳过 ${all.length - keep.length} 个无关文件`)
  }
  await send(keep, folder)
}

async function onZip(ev) {
  const f = ev.target.files?.[0]
  ev.target.value = ''
  if (!f) return
  await send([f], f.name.replace(/\.zip$/i, ''))
}
</script>

<template>
  <span class="folder-pick">
    <input ref="dirInput" type="file" webkitdirectory directory multiple
           style="display:none" @change="onDir" />
    <input ref="zipInput" type="file" accept=".zip" style="display:none" @change="onZip" />
    <button class="small" :disabled="disabled || busy"
            title="选择你电脑上的文件夹，连同子目录一起上传到服务器"
            @click="dirInput.click()">
      {{ busy ? `上传中 ${Math.round(progress * 100)}%` : label }}</button>
    <button v-if="zip && !busy" class="small" :disabled="disabled"
            title="浏览器选不了文件夹时，把文件夹压成 zip 传上来，效果一样"
            @click="zipInput.click()">zip</button>
  </span>
</template>

<style scoped>
.folder-pick { display: inline-flex; gap: 4px; align-items: center; }
</style>
