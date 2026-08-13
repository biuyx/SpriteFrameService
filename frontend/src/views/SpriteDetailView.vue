<script setup>
import { ref, onMounted } from 'vue'
import { useStore, gotoLibrary, openAction, toast, askConfirm } from '../stores'
import api from '../api'

const store = useStore()
const actions = ref([])
const commonNames = ref([])
const legacy = ref([])
const loading = ref(true)
const creating = ref(false)
const claiming = ref(false)
const newName = ref('')
const coverV = ref(Date.now() % 100000)

const STATUS_LABEL = { new: '未开始', active: '进行中', final: '已定稿' }

async function load() {
  loading.value = true
  try {
    const [a, s, l] = await Promise.all([
      api.actions(store.currentSprite.id),
      api.sprites(),
      api.legacySessions(),
    ])
    actions.value = a.actions
    commonNames.value = s.common_action_names || []
    legacy.value = l.sessions
  } finally {
    loading.value = false
  }
}

async function createAction(name) {
  const n = (name || newName.value).trim()
  if (!n) return
  const act = await api.createAction(store.currentSprite.id, n)
  newName.value = ''
  creating.value = false
  toast(`已创建动作「${act.name}」`)
  await open(act)
}

async function open(act) {
  await openAction(store.currentSprite.id, act.id)
}

async function removeAction(act) {
  if (!(await askConfirm(`删除动作「${act.name}」？其帧数据与导出将一并删除。`, { danger: true }))) return
  await api.deleteAction(store.currentSprite.id, act.id)
  toast('已删除')
  await load()
}

async function markFinal(act) {
  await api.patchAction(store.currentSprite.id, act.id, { status: act.status === 'final' ? 'active' : 'final' })
  await load()
}

async function claim(sess) {
  const name = await askConfirm(`把旧会话 ${sess.id.slice(0, 8)}（${sess.frame_count} 帧）认领为动作`, { input: { placeholder: '动作名称', initial: 'imported' } })
  if (!name) return
  await api.claimSession(store.currentSprite.id, sess.id, name)
  toast('已认领')
  claiming.value = false
  await load()
}

onMounted(load)
</script>

<template>
  <div class="detail">
    <div class="det-head">
      <button class="small" @click="gotoLibrary">← 精灵库</button>
      <h2>{{ store.currentSprite?.name }}</h2>
      <span class="spacer"></span>
      <button v-if="legacy.length" class="small" @click="claiming = !claiming">
        认领旧会话 ({{ legacy.length }})
      </button>
      <button class="primary" @click="creating = !creating">+ 新建动作</button>
    </div>

    <div v-if="creating" class="create-bar">
      <input v-model="newName" placeholder="动作名称" style="width:160px" @keyup.enter="createAction()" autofocus />
      <button class="primary" @click="createAction()">创建</button>
      <span class="hint">常用：</span>
      <button v-for="n in commonNames" :key="n" class="small" @click="createAction(n)">{{ n }}</button>
    </div>

    <div v-if="claiming && legacy.length" class="create-bar">
      <span class="hint">旧会话：</span>
      <button v-for="s in legacy" :key="s.id" class="small" @click="claim(s)">
        {{ s.id.slice(0, 8) }} · {{ s.frame_count }}帧{{ s.has_video ? ' · 有视频' : '' }}
      </button>
    </div>

    <div v-if="loading" class="hint" style="padding:40px;text-align:center">加载中...</div>

    <div v-else-if="!actions.length && !creating" class="empty">
      <div class="empty-icon">🎬</div>
      <p>「{{ store.currentSprite?.name }}」还没有动作。每个动作（走路/待机/攻击…）独立走完 素材→抽帧→精修→导出。</p>
      <button class="primary" @click="creating = true">创建第一个动作</button>
    </div>

    <div v-else class="grid">
      <div v-for="a in actions" :key="a.id" class="card" @click="open(a)">
        <div class="card-cover">
          <img :src="`/api/sprites/${store.currentSprite.id}/actions/${a.id}/cover?v=${coverV}`"
               @error="$event.target.style.display = 'none'" alt="" />
          <span class="cover-fallback">{{ a.name }}</span>
        </div>
        <div class="card-body">
          <div class="card-name">
            {{ a.name }}
            <span class="badge" :class="a.status">{{ STATUS_LABEL[a.status] || a.status }}</span>
          </div>
          <div class="card-meta">
            <template v-if="a.summary?.frame_count">
              {{ a.summary.frame_count }} 帧
              <span v-if="a.summary.processed_count"> · 已抠 {{ a.summary.processed_count }}</span>
              <span v-if="a.summary.export_count" class="ok-text"> · {{ a.summary.export_count }} 次导出</span>
            </template>
            <template v-else-if="a.summary?.has_video">已有视频，未抽帧</template>
            <template v-else>尚无素材</template>
          </div>
        </div>
        <div class="card-ops">
          <button class="small" @click.stop="markFinal(a)">{{ a.status === 'final' ? '取消定稿' : '定稿' }}</button>
          <button class="small danger" @click.stop="removeAction(a)">删除</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.detail { padding: 20px 24px; height: 100%; overflow-y: auto; }
.det-head { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
.det-head h2 { font-size: 18px; margin: 0; }
.spacer { flex: 1; }
.create-bar {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  background: var(--bg-panel); border: 1px solid var(--border); border-radius: 5px;
  padding: 10px 12px; margin-bottom: 14px;
}
.empty { text-align: center; padding: 60px 20px; color: var(--text-dim); }
.empty-icon { font-size: 42px; margin-bottom: 12px; }
.empty p { margin-bottom: 16px; max-width: 460px; margin-left: auto; margin-right: auto; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 14px; }
.card {
  background: var(--bg-panel); border: 1px solid var(--border); border-radius: 6px;
  overflow: hidden; cursor: pointer; transition: border-color .15s;
}
.card:hover { border-color: var(--accent); }
.card-cover {
  height: 130px; background: var(--bg-input); position: relative;
  display: flex; align-items: center; justify-content: center;
}
.card-cover img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; }
.cover-fallback { color: var(--text-dim); font-size: 16px; }
.card-body { padding: 10px 12px 6px; }
.card-name { font-weight: 600; display: flex; align-items: center; gap: 8px; }
.badge { font-size: 10px; padding: 1px 8px; border-radius: 8px; background: var(--bg-input); color: var(--text-dim); }
.badge.active { background: #1e88e533; color: var(--accent-hover); }
.badge.final { background: #4caf5033; color: var(--ok); }
.card-meta { font-size: 12px; color: var(--text-dim); margin-top: 4px; }
.ok-text { color: var(--ok); }
.card-ops { display: flex; gap: 6px; padding: 6px 12px 10px; }
.danger { border-color: var(--err); color: var(--err); }
</style>
