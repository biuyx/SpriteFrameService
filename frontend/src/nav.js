import { ref } from 'vue'

export const currentTab = ref('firstframe')

export function go(tab) {
  currentTab.value = tab
}
