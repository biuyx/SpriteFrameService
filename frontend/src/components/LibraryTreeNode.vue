<script setup>
import {ref} from 'vue'
const expanded = ref(true)
defineProps({ node: Object, active: String, counts: Object })
defineEmits(['pick'])
</script>
<template>
  <div>
    <div class="node-row"><button v-if="node.children.length" class="toggle" :aria-label="`${expanded?'折叠':'展开'} ${node.name}`" :aria-expanded="expanded" @click="expanded=!expanded">{{expanded?'▾':'▸'}}</button><button class="tree-link" :class="{active:active===node.id}" @click="$emit('pick',node.id)">{{ node.kind==='category'?'▣':'◇' }} {{ node.name }} <span>{{ counts[node.id] || 0 }}</span></button></div>
    <div v-if="node.children.length && expanded" class="children"><LibraryTreeNode v-for="child in node.children" :key="child.id" :node="child" :active="active" :counts="counts" @pick="$emit('pick',$event)" /></div>
  </div>
</template>
<style scoped>.node-row{display:flex;align-items:center}.toggle{padding:4px;border:0;background:transparent}.tree-link{display:flex;text-align:left;gap:8px;justify-content:space-between;width:100%;border:0;background:transparent;padding:8px;border-radius:5px;white-space:normal;overflow-wrap:anywhere}.tree-link.active{background:var(--bg-input);color:var(--accent-hover)}.tree-link span{color:var(--text-dim)}.children{padding-left:12px;border-left:1px solid var(--border);margin-left:8px}</style>
