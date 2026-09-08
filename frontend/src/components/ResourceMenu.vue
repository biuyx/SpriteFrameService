<script setup>
import { ref, onMounted, onUnmounted } from 'vue'

// 顶栏「资源库」下拉：全局资源（参考视频 / 参考首帧 / 提示词 / 项目包）的统一入口
const emit = defineEmits(['open'])
const open = ref(false)
const root = ref(null)

const ITEMS = [
  { key: 'tpl', label: '参考视频库', hint: '动作模板视频、分组、抽帧规则' },
  { key: 'ffset', label: '参考首帧库', hint: '完成角色的全套动作首帧' },
  { key: 'prompt', label: '提示词库', hint: '多版本、绑定匹配、动作记忆' },
  { key: 'transfer', label: '项目包 导出 / 导入', hint: '分享给其他人或导入别人的' },
]

function pick(key) {
  open.value = false
  emit('open', key)
}
function onDocClick(e) {
  if (open.value && root.value && !root.value.contains(e.target)) open.value = false
}
onMounted(() => document.addEventListener('mousedown', onDocClick))
onUnmounted(() => document.removeEventListener('mousedown', onDocClick))
</script>

<template>
  <span ref="root" class="res-menu">
    <button class="small" :class="{ on: open }" @click="open = !open">资源库 ▾</button>
    <div v-if="open" class="res-drop">
      <button v-for="it in ITEMS" :key="it.key" class="res-item" @click="pick(it.key)">
        <span class="res-label">{{ it.label }}</span>
        <span class="res-hint">{{ it.hint }}</span>
      </button>
    </div>
  </span>
</template>

<style scoped>
.res-menu { position: relative; display: inline-block; }
.res-menu > button.on { background: var(--bg-hover); }
.res-drop {
  position: absolute; top: calc(100% + 4px); left: 0; z-index: 80; min-width: 230px;
  background: var(--bg-panel); border: 1px solid var(--border); border-radius: 6px;
  box-shadow: 0 6px 20px rgba(0,0,0,.45); padding: 4px;
}
.res-item {
  display: flex; flex-direction: column; align-items: flex-start; gap: 1px; width: 100%;
  text-align: left; background: transparent; border: none; border-radius: 4px; padding: 7px 10px;
}
.res-item:hover { background: var(--bg-hover); }
.res-label { font-size: 13px; color: var(--text); }
.res-hint { font-size: 11px; color: var(--text-dim); }
</style>
