<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { useStore, gotoSprite, toast, askConfirm } from '../stores'
import api from '../api'
import BatchImportModal from '../components/BatchImportModal.vue'

const store = useStore()
const sprites = ref([])
const legacyCount = ref(0)
const loading = ref(true)
const batchOpen = ref(false)
// 全局资源（项目包导入等）变动后重载列表
watch(() => store.libraryVersion, () => load())

// 工具栏状态（筛选/排序记在本机，下次进来保持）
const PREF_KEY = 'sfs.library.prefs'
let prefs = {}
try { prefs = JSON.parse(localStorage.getItem(PREF_KEY) || '{}') } catch { prefs = {} }
const keyword = ref(prefs.keyword || '')
const category = ref(prefs.category || '')          // '' = 全部分类
const sortBy = ref(prefs.sortBy || 'created_desc')
watch([keyword, category, sortBy], ([k, c, s]) => {
  try { localStorage.setItem(PREF_KEY, JSON.stringify({ keyword: k, category: c, sortBy: s })) } catch { /* ignore */ }
})

// 新建
const creating = ref(false)
const newName = ref('')
const newTags = ref('')
// 以已有角色为模板新建（复制动作骨架/模板绑定/提示词与参数记忆/工艺预设）
const fromSprite = ref('')
const copyFirstFrames = ref(true)
const copyRefs = ref(false)
const creatingBusy = ref(false)
const fromSpriteObj = computed(() => sprites.value.find(s => s.id === fromSprite.value) || null)

// 编辑（改名 / 分类）
const editing = ref(null)         // {id, name, tags: 'a, b'}

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

// ---- 进度：五阶段（生成/抽帧/抠图/导出/定稿）平均完成度 ----
function pct(sp) {
  const total = sp.action_count || 0
  if (!total) return 0
  const p = sp.progress || {}
  const sum = (p.generated || 0) + (p.extracted || 0) + (p.processed || 0)
            + (p.exported || 0) + (p.final || 0)
  return Math.round(sum / (total * 5) * 100)
}

const allCategories = computed(() => {
  const set = new Set()
  for (const sp of sprites.value) for (const t of sp.tags || []) set.add(t)
  return [...set].sort()
})

const shown = computed(() => {
  let list = sprites.value
  const kw = keyword.value.trim().toLowerCase()
  if (kw) list = list.filter(sp => sp.name.toLowerCase().includes(kw))
  if (category.value) list = list.filter(sp => (sp.tags || []).includes(category.value))
  const by = sortBy.value
  return [...list].sort((a, b) => {
    if (by === 'created_desc') return (b.created_at || 0) - (a.created_at || 0)
    if (by === 'created_asc') return (a.created_at || 0) - (b.created_at || 0)
    if (by === 'progress_desc') return pct(b) - pct(a)
    if (by === 'progress_asc') return pct(a) - pct(b)
    return a.name.localeCompare(b.name, 'zh')
  })
})

function fmtTime(ts) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function parseTags(s) {
  return s.split(/[,，、;；\s]+/).map(t => t.trim()).filter(Boolean)
}

async function createSprite() {
  const name = newName.value.trim()
  if (!name || creatingBusy.value) return
  creatingBusy.value = true
  try {
    // 分类留空且用了模板角色时传 null：继承模板角色的分类
    const tags = parseTags(newTags.value)
    const sp = await api.createSprite({
      name, tags: (tags.length || !fromSprite.value) ? tags : null,
      from_sprite_id: fromSprite.value || null,
      copy_first_frames: copyFirstFrames.value,
      copy_refs: copyRefs.value,
    })
    newName.value = ''
    newTags.value = ''
    creating.value = false
    if (sp.forked_from) {
      const c = sp.copied || {}
      toast(`已以「${sp.forked_from}」为模板创建「${sp.name}」：` +
            `动作 ${c.actions} 个` + (c.first_frames ? ` · 首帧 ${c.first_frames} 张` : '') +
            (c.refs ? ` · 参考图 ${c.refs} 张` : ''))
    } else {
      toast(`已创建精灵「${sp.name}」`)
    }
    gotoSprite(sp)
  } catch (e) {
    toast(`创建失败: ${e.message}`)
  } finally {
    creatingBusy.value = false
  }
}

function startEdit(sp) {
  editing.value = { id: sp.id, name: sp.name, tags: (sp.tags || []).join(', ') }
}

async function saveEdit() {
  const e = editing.value
  if (!e || !e.name.trim()) return
  await api.patchSprite(e.id, { name: e.name.trim(), tags: parseTags(e.tags) })
  editing.value = null
  toast('已保存')
  await load()
}

async function removeSprite(sp) {
  if (!(await askConfirm(`删除精灵「${sp.name}」及其全部 ${sp.action_count} 个动作？
此操作不可恢复。`, { danger: true }))) return
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
    <div class="lib-toolbar">
      <h2>精灵库 <span class="count" v-if="!loading">{{ shown.length }}<template v-if="shown.length !== sprites.length"> / {{ sprites.length }}</template></span></h2>
      <input v-model="keyword" class="search" placeholder="搜索名称…" />
      <select v-model="category" title="项目分类">
        <option value="">全部分类</option>
        <option v-for="c in allCategories" :key="c" :value="c">{{ c }}</option>
      </select>
      <select v-model="sortBy" title="排序">
        <option value="created_desc">最近创建</option>
        <option value="created_asc">最早创建</option>
        <option value="progress_desc">进度 高→低</option>
        <option value="progress_asc">进度 低→高</option>
        <option value="name">名称</option>
      </select>
      <span class="spacer"></span>
      <span class="hint" v-if="legacyCount">{{ legacyCount }} 个旧会话待认领</span>
      <button class="small" @click="batchOpen = true">批量导入</button>
      <button class="primary" @click="creating = true">+ 新建精灵</button>
    </div>

    <div v-if="creating" class="create-bar">
      <div class="row" style="gap:8px;align-items:center;flex-wrap:wrap;margin:0">
        <input v-model="newName" placeholder="精灵名称，如：厨师" style="width:180px" @keyup.enter="createSprite" autofocus />
        <input v-model="newTags" placeholder="项目分类（可选，逗号分隔）" style="width:200px" @keyup.enter="createSprite" />
        <div class="field inline"><label>模板角色</label>
          <select v-model="fromSprite" style="max-width:190px" title="以已有角色为模板：复制动作、模板绑定、提示词与参数记忆、工艺/导出预设">
            <option value="">不使用（空白新建）</option>
            <option v-for="s in sprites" :key="s.id" :value="s.id">{{ s.name }}（{{ s.action_count }} 动作）</option>
          </select></div>
        <button class="primary" :disabled="creatingBusy" @click="createSprite">
          {{ creatingBusy ? '创建中…' : '创建' }}</button>
        <button :disabled="creatingBusy" @click="creating = false">取消</button>
      </div>
      <div v-if="fromSprite" class="row" style="gap:12px;align-items:center;margin:6px 0 0">
        <label class="chk"><input type="checkbox" v-model="copyFirstFrames" /> 复制首帧图</label>
        <label class="chk" title="含正面/背面立绘标记；换角色时通常不勾"><input type="checkbox" v-model="copyRefs" /> 复制参考图库</label>
        <span class="hint">
          将复制「{{ fromSpriteObj?.name }}」的 {{ fromSpriteObj?.action_count }} 个动作骨架
          （动作名、模板绑定、提示词与参数记忆、工艺/导出预设）；素材视频、帧、抠图与导出不复制。</span>
      </div>
    </div>

    <div v-if="loading" class="hint" style="padding:40px;text-align:center">加载中...</div>

    <div v-else-if="!sprites.length" class="empty">
      <div class="empty-icon">🎨</div>
      <p>还没有精灵。精灵 = 一个角色，包含它的所有动作（走路、待机、攻击…）。</p>
      <button class="primary" @click="creating = true">创建第一个精灵</button>
    </div>

    <div v-else class="tbl-wrap">
      <table class="lib-tbl">
        <thead>
          <tr>
            <th style="width:26%">精灵</th>
            <th style="width:16%">分类</th>
            <th style="width:8%">动作</th>
            <th style="width:24%">完成进度</th>
            <th style="width:13%">创建时间</th>
            <th style="width:13%">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="sp in shown" :key="sp.id" class="sp-row" @click="open(sp)">
            <td>
              <div class="sp-name">
                <span class="avatar">{{ sp.name.slice(0, 2) }}</span>
                <b>{{ sp.name }}</b>
              </div>
            </td>
            <td>
              <span v-if="!sp.tags?.length" class="hint">—</span>
              <span v-for="t in sp.tags" :key="t" class="tag">{{ t }}</span>
            </td>
            <td>{{ sp.action_count }}</td>
            <td>
              <div class="prog">
                <div class="prog-bar"><div class="prog-fill" :style="{ width: pct(sp) + '%' }"></div></div>
                <span class="prog-pct">{{ pct(sp) }}%</span>
              </div>
              <div class="prog-detail" v-if="sp.action_count">
                生成 {{ sp.progress?.generated || 0 }} · 抽帧 {{ sp.progress?.extracted || 0 }}
                · 抠图 {{ sp.progress?.processed || 0 }} · 导出 {{ sp.progress?.exported || 0 }}
                · <span :class="{ 'ok-text': sp.progress?.final }">定稿 {{ sp.progress?.final || 0 }}/{{ sp.action_count }}</span>
              </div>
            </td>
            <td class="time">{{ fmtTime(sp.created_at) }}</td>
            <td class="ops" @click.stop>
              <button class="small" @click="open(sp)">打开</button>
              <button class="small" @click="startEdit(sp)">编辑</button>
              <button class="small danger" @click="removeSprite(sp)">删除</button>
            </td>
          </tr>
          <tr v-if="!shown.length">
            <td colspan="6" class="hint" style="text-align:center;padding:30px">没有匹配的精灵</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 编辑名称 / 分类 -->
  <div v-if="editing" class="edit-mask" @click.self="editing = null">
    <div class="edit-box">
      <h3>编辑精灵</h3>
      <div class="field"><label>名称</label>
        <input v-model="editing.name" @keyup.enter="saveEdit" /></div>
      <div class="field"><label>项目分类（逗号分隔，可多个）</label>
        <input v-model="editing.tags" placeholder="如：项目A, 主角" @keyup.enter="saveEdit" /></div>
      <div class="edit-ops">
        <button class="primary" @click="saveEdit">保存</button>
        <button @click="editing = null">取消</button>
      </div>
    </div>
  </div>

  <BatchImportModal v-if="batchOpen" @close="batchOpen = false"
                    @done="load()" />
</template>

<style scoped>
.library { padding: 16px 24px; height: 100%; overflow-y: auto; }
.lib-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }
.lib-toolbar h2 { font-size: 17px; margin: 0 6px 0 0; }
.count { font-size: 13px; color: var(--text-dim); font-weight: 400; }
.search { width: 170px; }
.spacer { flex: 1; }
.chk { display: flex; align-items: center; gap: 5px; font-size: 12px; cursor: pointer; }
.create-bar {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  background: var(--bg-panel); border: 1px solid var(--border); border-radius: 5px;
  padding: 10px 12px; margin-bottom: 12px;
}
.empty { text-align: center; padding: 60px 20px; color: var(--text-dim); }
.empty-icon { font-size: 42px; margin-bottom: 12px; }
.empty p { margin-bottom: 16px; }

.tbl-wrap { background: var(--bg-panel); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
.lib-tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.lib-tbl th {
  text-align: left; font-weight: 500; color: var(--text-dim); font-size: 12px;
  padding: 9px 14px; border-bottom: 1px solid var(--border); background: var(--bg-input);
}
.lib-tbl td { padding: 9px 14px; border-bottom: 1px solid var(--border); vertical-align: middle; }
.lib-tbl tbody tr:last-child td { border-bottom: none; }
.sp-row { cursor: pointer; transition: background .12s; }
.sp-row:hover { background: var(--bg-hover); }

.sp-name { display: flex; align-items: center; gap: 10px; }
.avatar {
  width: 34px; height: 34px; border-radius: 6px; background: var(--bg-input);
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 13px; font-weight: 600; color: var(--text-dim); flex: none;
}
.tag {
  font-size: 11px; padding: 1px 8px; background: var(--bg-input); border-radius: 8px;
  color: var(--text-dim); margin-right: 4px; display: inline-block;
}
.prog { display: flex; align-items: center; gap: 8px; }
.prog-bar { flex: 1; height: 6px; background: var(--bg-input); border-radius: 3px; overflow: hidden; }
.prog-fill { height: 100%; background: var(--accent); border-radius: 3px; transition: width .3s; }
.prog-pct { font-size: 12px; color: var(--text-dim); width: 34px; text-align: right; }
.prog-detail { font-size: 11px; color: var(--text-dim); margin-top: 3px; }
.ok-text { color: var(--ok); }
.time { color: var(--text-dim); font-size: 12px; white-space: nowrap; }
.ops { white-space: nowrap; }
.ops button { margin-right: 6px; }
.danger { border-color: var(--err); color: var(--err); }

.edit-mask {
  position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 90;
  display: flex; align-items: center; justify-content: center;
}
.edit-box {
  width: 360px; max-width: 92vw; background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: 8px; padding: 18px 20px;
}
.edit-box h3 { margin: 0 0 12px; font-size: 15px; }
.edit-box .field { margin-bottom: 12px; }
.edit-box input { width: 100%; }
.edit-ops { display: flex; gap: 10px; justify-content: flex-end; }
</style>
