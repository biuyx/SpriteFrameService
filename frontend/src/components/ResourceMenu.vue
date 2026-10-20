<script setup>
// 顶栏直接展示全局资源入口，精灵库、动作看板与工作台共用。
const emit = defineEmits(['open'])

const ITEMS = [
  { key: 'tpl', label: '参考视频库', hint: '动作模板视频、分组、抽帧规则' },
  { key: 'ffset', label: '参考首帧库', hint: '完成角色的全套动作首帧' },
  { key: 'prompt', label: '提示词库', hint: '多版本、绑定匹配、动作记忆' },
  { key: 'transfer', label: '项目包 导出 / 导入', hint: '分享给其他人或导入别人的' },
]

</script>

<template>
  <div class="resource-nav" role="group" aria-label="资源入口">
    <button v-for="it in ITEMS" :key="it.key" class="resource-link" :title="it.hint"
            @click="emit('open', it.key)">{{ it.label }}</button>
  </div>
</template>

<style scoped>
.resource-nav { display: inline-flex; align-items: center; flex-wrap: wrap; gap: 4px; }
.resource-link { background: transparent; border: none; border-radius: 4px; padding: 5px 8px;
  font-size: 13px; color: var(--text-dim); white-space: nowrap; }
.resource-link:hover, .resource-link:focus-visible { color: var(--text); background: var(--bg-hover); }
</style>
