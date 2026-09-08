<script setup>
import { ref, onMounted } from 'vue'
import { useStore, toast, askConfirm } from '../stores'
import api from '../api'

// 「保存为模板规则」：把最近一次抽帧参数 + 当前保留的帧（删帧后的结果）
// 存到该动作视频版本所用的模板上；工作台与动作分析页共用。
const emit = defineEmits(['saved'])
const store = useStore()
const tpl = ref(null)
const busy = ref(false)

onMounted(async () => {
  try {
    const [tks, tpls] = await Promise.all([
      api.takes(store.sessionId), api.templates(),
    ])
    const cur = (tks.takes || []).find((t) => t.id === tks.current)
    const tid = cur?.template_id || store.currentAction?.template_id
    tpl.value = tpls.templates.find((x) => x.id === tid) || null
  } catch { /* 无模板则不显示按钮 */ }
})

async function save() {
  if (!tpl.value) return
  if (!store.frameCount) return toast('先抽帧并完成删帧/选帧，再保存规则')
  const name = tpl.value.variant || tpl.value.key
  const overwrite = tpl.value.extract_rule ? '\n将覆盖模板已有规则。' : ''
  if (!(await askConfirm(
    `把最近一次抽帧的参数与当前保留的 ${store.frameCount} 帧，保存为模板「${name}」的参考规则？\n` +
    `之后同模板生成的视频（单个/批量）抽帧时自动沿用，包括删帧结果。${overwrite}`))) return
  busy.value = true
  try {
    const r = await api.saveExtractRule(store.sessionId, tpl.value.id)
    tpl.value = { ...tpl.value, extract_rule: r.rule }
    const keepTxt = r.rule.keep ? `，保留 ${r.kept}/${r.total} 帧` : ''
    const skipTxt = r.unmapped ? `（${r.unmapped} 个补帧/特殊帧未计入）` : ''
    toast(`已保存规则：${r.rule.start}–${r.rule.end}s @${r.rule.fps}${keepTxt}${skipTxt}`)
    emit('saved')
  } catch (e) {
    toast(`保存失败: ${e.message}`)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <button v-if="tpl" class="small" :disabled="busy"
          :title="`把当前抽帧参数与保留帧存为模板「${tpl.variant || tpl.key}」的参考规则`"
          @click="save">{{ busy ? '保存中…' : '保存为模板规则' }}</button>
</template>
