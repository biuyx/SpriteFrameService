<script setup>
import { ref, watch, onMounted } from 'vue'
import { useStore, toast, askConfirm } from '../stores'
import api from '../api'

// 「保存为模板规则」：把最近一次抽帧参数 + 当前保留的帧（删帧后的结果）
// 存到该动作视频版本所用的模板上；工作台与动作分析页共用。
const emit = defineEmits(['saved'])
const store = useStore()
const tpl = ref(null)
const busy = ref(false)
const failed = ref(false)       // 请求失败 ≠ 没绑模板，两者要分开
const resolvedFor = ref(null)   // 这份 tpl 是给哪个会话算的

async function resolveTpl() {
  const sid = store.sessionId
  if (!sid) { tpl.value = null; resolvedFor.value = null; return }
  // 换了动作就先清掉旧结果：重算期间宁可不显示，也不要挂着上个动作的模板
  if (resolvedFor.value !== sid) tpl.value = null
  failed.value = false
  try {
    const [tks, tpls] = await Promise.all([api.takes(sid), api.templates()])
    if (store.sessionId !== sid) return        // 期间又切了动作，这次结果作废
    const cur = (tks.takes || []).find((t) => t.id === tks.current)
    const tid = cur?.template_id || store.currentAction?.template_id
    tpl.value = tpls.templates.find((x) => x.id === tid) || null
    resolvedFor.value = sid
  } catch {
    // 取不到模板信息（服务重启/登录过期/网络瞬断）——显式标出并给重试，
    // 不要静默隐藏按钮，那会让人以为这个动作没绑模板
    tpl.value = null
    resolvedFor.value = null
    failed.value = true
  }
}

onMounted(resolveTpl)
// 切动作、切视频版本、改模板绑定都要重算：即便父视图没重建也不会用错模板
watch(() => [store.sessionId, store.currentAction?.template_id, store.videoVersion],
      resolveTpl)

async function save() {
  if (!tpl.value) return
  if (resolvedFor.value !== store.sessionId) {
    // 兜底：解析完成后又切过动作，先重算再让用户确认，避免写到别的模板上
    await resolveTpl()
    if (!tpl.value) return toast('动作已切换，模板信息已刷新，请再点一次')
  }
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
  <button v-else-if="failed" class="small warn-btn"
          title="没取到模板信息（服务可能刚重启或登录已过期）——点一下重试"
          @click="resolveTpl">模板信息获取失败，重试</button>
</template>

<style scoped>
.warn-btn { border-color: var(--warn); color: var(--warn); }
</style>
