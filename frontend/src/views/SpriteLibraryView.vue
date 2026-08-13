<script setup>
import { ref, onMounted } from 'vue'
import { useStore, gotoSprite, toast } from '../stores'
import api from '../api'

const store = useStore()
const sprites = ref([])
const legacyCount = ref(0)
const loading = ref(true)
const creating = ref(false)
const newName = ref('')

async function load() {
  loading.value = true
  try {
    const r = await api.sprites()
    sprites.value = r.sprites
    const l = await api.legacySessions()
    legacyCount.value = l.sessions.length
  } finally {
    loading.value = false
  }
}

async function createSprite() {
  const name = newName.value.trim()
  if (!name) return
  const sp = await api.createSprite(name)
  newName.value = ''
  creating.value = false
  toast(`已创建精灵「${sp.name}」`)
  gotoSprite(sp)
}

async function removeSprite(sp) {
  if (!confirm(`删除精灵「${sp.name}」及其全部 ${sp.action_count} 个动作？此操作不可恢复。`)) return
  await api.deleteSprite(sp.id)
  toast('已删除')
  await load()
}

function open(sp) {
  gotoSprite(sp)
}

onMounted(load)
</script>

<template>
  <div class="library">
    <div class="lib-head">
      <h2>精灵库</h2>
      <span class="hint" v-if="legacyCount">有 {{ legacyCount }} 个未归档的旧会话，进入任意精灵可认领</span>
      <span class="spacer"></span>
      <button class="primary" @click="creating = true" v-if="!creating">+ 新建精灵</button>
      <template v-else>
        <input v-model="newName" placeholder="精灵名称，如：厨师" @keyup.enter="createSprite"
               style="width:180px" autofocus />
        <button class="primary" @click="createSprite">创建</button>
        <button @click="creating = false">取消</button>
      </template>
    </div>

    <div v-if="loading" class="hint" style="padding:40px;text-align:center">加载中...</div>

    <div v-else-if="!sprites.length" class="empty">
      <div class="empty-icon">🎨</div>
      <p>还没有精灵。精灵 = 一个角色，包含它的所有动作（走路、待机、攻击…）。</p>
      <button class="primary" @click="creating = true">创建第一个精灵</button>
    </div>

    <div v-else class="grid">
      <div v-for="sp in sprites" :key="sp.id" class="card" @click="open(sp)">
        <div class="card-cover">{{ sp.name.slice(0, 2) }}</div>
        <div class="card-body">
          <div class="card-name">{{ sp.name }}</div>
          <div class="card-meta">
            <span>{{ sp.action_count }} 个动作</span>
            <span v-if="sp.final_count" class="ok-text">· {{ sp.final_count }} 已定稿</span>
          </div>
          <div class="card-tags" v-if="sp.tags?.length">
            <span v-for="t in sp.tags" :key="t" class="tag">{{ t }}</span>
          </div>
        </div>
        <button class="small danger card-del" @click.stop="removeSprite(sp)" title="删除精灵">✕</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.library { padding: 20px 24px; height: 100%; overflow-y: auto; }
.lib-head { display: flex; align-items: center; gap: 12px; margin-bottom: 18px; }
.lib-head h2 { font-size: 18px; margin: 0; }
.spacer { flex: 1; }
.empty { text-align: center; padding: 60px 20px; color: var(--text-dim); }
.empty-icon { font-size: 42px; margin-bottom: 12px; }
.empty p { margin-bottom: 16px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 14px; }
.card {
  position: relative; background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: 6px; overflow: hidden; cursor: pointer; transition: border-color .15s;
}
.card:hover { border-color: var(--accent); }
.card-cover {
  height: 110px; display: flex; align-items: center; justify-content: center;
  font-size: 34px; font-weight: 600; color: var(--text-dim);
  background: var(--bg-input);
}
.card-body { padding: 10px 12px; }
.card-name { font-weight: 600; margin-bottom: 4px; }
.card-meta { font-size: 12px; color: var(--text-dim); }
.ok-text { color: var(--ok); }
.card-tags { margin-top: 6px; display: flex; gap: 4px; flex-wrap: wrap; }
.tag { font-size: 10px; padding: 1px 7px; background: var(--bg-input); border-radius: 8px; color: var(--text-dim); }
.card-del { position: absolute; top: 6px; right: 6px; opacity: 0; transition: opacity .15s; }
.card:hover .card-del { opacity: 1; }
.danger { border-color: var(--err); color: var(--err); }
</style>
