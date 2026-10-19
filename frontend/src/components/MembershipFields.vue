<script setup>
import { computed } from 'vue'
import { typeLabel } from '../libraryModel'
const props = defineProps({ catalog: Object, modelValue: Object })
const emit = defineEmits(['update:modelValue'])
const types = computed(() => props.catalog.types.filter(t => t.project_id === props.modelValue.project_id))
function project(e) { emit('update:modelValue', { project_id: e.target.value || null, sprite_type_id: null }) }
function type(e) { emit('update:modelValue', { ...props.modelValue, sprite_type_id: e.target.value || null }) }
</script>
<template>
  <div class="membership-fields">
    <label>所属项目 <select :value="modelValue.project_id || ''" @change="project"><option value="">待归类（保留旧数据）</option><option v-for="p in catalog.projects" :key="p.id" :value="p.id">{{ p.name }}</option></select></label>
    <label>精灵类型 <select :value="modelValue.sprite_type_id || ''" :disabled="!modelValue.project_id" @change="type"><option value="">未指定类型</option><option v-for="t in types" :key="t.id" :value="t.id">{{ typeLabel(catalog, t) }}</option></select></label>
  </div>
</template>
<style scoped>.membership-fields{display:flex;gap:12px;flex-wrap:wrap}.membership-fields label{display:flex;gap:8px;align-items:center;margin:8px 0}.membership-fields select{max-width:320px;min-width:150px}</style>
