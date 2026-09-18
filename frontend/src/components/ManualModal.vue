<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { MANUAL, GROUPS, TAB_TOPIC } from '../manual'
import { currentTab } from '../nav'

// 使用手册：左侧目录 + 右侧正文 + 搜索。
// 内容是结构化数据（见 manual.js），不引 markdown 渲染库。
const props = defineProps({
  // 从某道工序打开时定位到对应章节；不传则从第一条开始
  topic: { type: String, default: '' },
})
const emit = defineEmits(['close'])

const kw = ref('')
const activeId = ref(props.topic || TAB_TOPIC[currentTab.value] || MANUAL[0].id)

function plain(sec) {
  const parts = [sec.title]
  for (const b of sec.body) {
    if (b.text) parts.push(b.text)
    if (b.items) parts.push(b.items.join(' '))
    if (b.rows) parts.push(b.rows.map((r) => r.join(' ')).join(' '))
  }
  return parts.join(' ')
}

const hits = computed(() => {
  const k = kw.value.trim().toLowerCase()
  if (!k) return null
  return MANUAL.filter((s) => plain(s).toLowerCase().includes(k)).map((s) => s.id)
})

const visible = computed(() =>
  hits.value ? MANUAL.filter((s) => hits.value.includes(s.id)) : MANUAL)

const grouped = computed(() =>
  GROUPS.map((g) => ({ name: g, items: visible.value.filter((s) => s.group === g) }))
        .filter((g) => g.items.length))

const active = computed(() =>
  MANUAL.find((s) => s.id === activeId.value) || visible.value[0] || MANUAL[0])

function pick(id) {
  activeId.value = id
  document.querySelector('.man-body')?.scrollTo({ top: 0 })
}

function onKey(e) {
  if (e.key === 'Escape') emit('close')
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <div class="modal-head">
        <h3>使用手册</h3>
        <input v-model="kw" class="man-search" placeholder="搜索：如 描边、排队、Spine" />
        <span class="spacer"></span>
        <button class="small" @click="emit('close')">✕ 关闭</button>
      </div>

      <div class="man-wrap">
        <nav class="man-nav">
          <template v-for="g in grouped" :key="g.name">
            <div class="man-group">{{ g.name }}</div>
            <button v-for="s in g.items" :key="s.id" class="man-link"
                    :class="{ on: s.id === activeId }" @click="pick(s.id)">{{ s.title }}</button>
          </template>
          <p v-if="!grouped.length" class="hint" style="padding:10px">没有匹配的条目</p>
        </nav>

        <article class="man-body">
          <h2>{{ active.title }}</h2>
          <template v-for="(b, i) in active.body" :key="i">
            <h4 v-if="b.t === 'h'">{{ b.text }}</h4>
            <p v-else-if="b.t === 'p'">{{ b.text }}</p>
            <ul v-else-if="b.t === 'ul'"><li v-for="(x, j) in b.items" :key="j">{{ x }}</li></ul>
            <ol v-else-if="b.t === 'ol'"><li v-for="(x, j) in b.items" :key="j">{{ x }}</li></ol>
            <p v-else-if="b.t === 'note'" class="man-note">{{ b.text }}</p>
            <pre v-else-if="b.t === 'code'">{{ b.text }}</pre>
            <table v-else-if="b.t === 'kv'" class="man-kv">
              <tr v-for="(r, j) in b.rows" :key="j"><th>{{ r[0] }}</th><td>{{ r[1] }}</td></tr>
            </table>
          </template>
        </article>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-mask {
  position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 95;
  display: flex; align-items: center; justify-content: center;
}
.modal {
  width: 900px; max-width: 95vw; height: 76vh; display: flex; flex-direction: column;
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 14px 18px 16px;
}
.modal-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.modal-head h3 { margin: 0; font-size: 16px; }
.spacer { flex: 1; }
.man-search { width: 220px; }

.man-wrap { flex: 1; min-height: 0; display: flex; gap: 14px; }
.man-nav {
  width: 190px; flex: none; overflow-y: auto; padding-right: 8px;
  border-right: 1px solid var(--border);
}
.man-group {
  font-size: 11px; color: var(--text-dim); margin: 10px 0 4px; padding-left: 8px;
  letter-spacing: 1px;
}
.man-group:first-child { margin-top: 0; }
.man-link {
  display: block; width: 100%; text-align: left; border: none; background: none;
  color: var(--text); font-size: 13px; padding: 5px 8px; border-radius: 4px; cursor: pointer;
}
.man-link:hover { background: var(--bg-hover); }
.man-link.on { background: var(--bg-input); color: var(--accent-hover); font-weight: 600; }

.man-body { flex: 1; min-width: 0; overflow-y: auto; padding-right: 6px; line-height: 1.75; }
.man-body h2 { margin: 0 0 12px; font-size: 17px; }
.man-body h4 { margin: 18px 0 6px; font-size: 14px; color: var(--accent-hover); }
.man-body p { margin: 8px 0; font-size: 13px; }
.man-body ul, .man-body ol { margin: 8px 0; padding-left: 22px; font-size: 13px; }
.man-body li { margin: 4px 0; }
.man-note {
  padding: 8px 11px; font-size: 12.5px; color: #ffcc80;
  background: #ff980018; border-left: 2px solid var(--warn); border-radius: 3px;
}
.man-body pre {
  background: var(--bg-input); padding: 10px 12px; border-radius: 4px;
  font-size: 12px; overflow-x: auto; line-height: 1.6;
}
.man-kv { border-collapse: collapse; margin: 8px 0; font-size: 13px; width: 100%; }
.man-kv th {
  text-align: left; vertical-align: top; white-space: nowrap; font-weight: 600;
  padding: 6px 12px 6px 0; color: var(--text-dim); width: 1%;
}
.man-kv td { padding: 6px 0; border-bottom: 1px solid var(--border); }
.man-kv tr:last-child td { border-bottom: none; }
</style>
