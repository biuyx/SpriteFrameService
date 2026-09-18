// 动作对应的参考视频模板（抽帧规则就存在它上面）。
//
// 抽帧页和动作分析页都要用，此前各自解析了一份，还各自把错误吞掉：
// 结果是「改了一层，另一层还拦着」，按钮照样消失且不说原因。统一到这里。
//
// 查找顺序：当前视频版本(take)生成时所用的模板 → 动作绑定的模板。
import { ref, computed, watch, onMounted } from 'vue'
import { useStore } from './stores'
import api from './api'

export function useRuleTemplate() {
  const store = useStore()
  const all = ref([])
  const loading = ref(true)
  const failed = ref(false)        // 请求失败 ≠ 没绑模板，两者要分开

  async function reload() {
    loading.value = true
    failed.value = false
    try {
      all.value = (await api.templates()).templates
    } catch {
      all.value = []
      failed.value = true          // 不静默隐藏，界面上给可重试的提示
    } finally {
      loading.value = false
    }
  }

  const boundId = computed(() => {
    const cur = (store.takes.takes || []).find((t) => t.id === store.takes.current)
    return cur?.template_id || store.currentAction?.template_id || ''
  })

  const tpl = computed(() =>
    all.value.find((x) => x.id === boundId.value) || null)

  // 没绑模板（区别于「取不到模板库」）
  const unbound = computed(() => !failed.value && !loading.value && !boundId.value)

  onMounted(reload)
  // 切动作 / 换视频版本后，绑定关系可能变了，重新取一次
  watch(() => [store.sessionId, store.currentAction?.template_id], reload)

  return { all, tpl, boundId, unbound, failed, loading, reload }
}
