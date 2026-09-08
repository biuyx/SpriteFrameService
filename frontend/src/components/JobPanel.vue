<script setup>
defineProps({
  jobs: { type: Array, required: true },
})
defineEmits(['cancel', 'close', 'jump'])

const STATUS_TXT = { queued: '排队', running: '', done: '完成', error: '失败', cancelled: '已取消' }
</script>

<template>
  <div class="panel job-panel">
    <div class="row2" style="align-items:center; margin-bottom:6px">
      <h3 style="margin:0">后台任务</h3>
      <span class="hint">{{ jobs.filter(j => j.status === 'running').length }} 个进行中</span>
      <button class="small" title="隐藏任务栏" @click="$emit('close')">✕</button>
    </div>
    <div class="job-list">
      <div v-for="j in jobs" :key="j.id" class="job-item" :class="{ error: j.status === 'error' }">
        <div class="row2">
          <span class="job-title">{{ j.title }}</span>
          <span>{{ STATUS_TXT[j.status] ?? j.status }}{{ j.status === 'running' ? Math.round(j.progress) + '%' : '' }}</span>
        </div>
        <!-- 上下文：属于哪个精灵/动作，点击直达（当前就在该动作时不可点） -->
        <div v-if="j.actionId" class="job-ctx" :title="'打开 ' + j.spriteName + ' / ' + j.actionName"
             @click="$emit('jump', j)">
          {{ j.spriteName }} / {{ j.actionName }} ↗
        </div>
        <div class="bar"><div :style="{ width: j.progress + '%' }"></div></div>
        <div class="row2"><span class="job-msg" :title="j.error || j.message">{{ j.message }}</span>
          <button v-if="j.status === 'running' || j.status === 'queued'" class="small"
                  @click="$emit('cancel', j.id)">取消</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.job-panel {
  position: fixed; right: 16px; bottom: 16px; width: 340px; z-index: 50;
  box-shadow: 0 6px 24px rgba(0,0,0,.5);
}
.job-title { font-weight: 600; }
.job-ctx {
  font-size: 11px; color: var(--accent-hover); cursor: pointer; margin: 1px 0 2px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.job-ctx:hover { text-decoration: underline; }
.job-msg { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 250px; }
</style>
