<script setup>
import { ref, computed, onMounted, nextTick } from 'vue'
import { useStore, openAction, toast, askConfirm } from '../stores'
import { currentTab } from '../nav'
import api from '../api'
import BatchGenerateModal from '../components/BatchGenerateModal.vue'
import FirstFrameLibraryModal from '../components/FirstFrameLibraryModal.vue'
import BatchExtractModal from '../components/BatchExtractModal.vue'
import BatchFfGenModal from '../components/BatchFfGenModal.vue'

const store = useStore()
const actions = ref([])
const commonNames = ref([])
const legacy = ref([])
const templatesById = ref({})
const loading = ref(true)
const creating = ref(false)
const claiming = ref(false)
const newName = ref('')
const coverV = ref(Date.now() % 100000)
const batchGenOpen = ref(false)
const ffLibOpen = ref(false)
const batchExtOpen = ref(false)
const ffGenOpen = ref(false)

const STATUS_LABEL = { new: '未开始', active: '进行中', final: '已定稿' }

// 精灵摘要条：分类/五阶段进度/立绘就绪
const spriteInfo = ref(null)         // 来自精灵库列表（含 progress/tags）
const refs = ref([])                 // 首帧图库（找立绘标记）
const hasFront = computed(() => refs.value.some(r => r.role === 'front'))
const hasBack = computed(() => refs.value.some(r => r.role === 'back'))
function pct(sp) {
  const total = sp?.action_count || 0
  if (!total) return 0
  const p = sp.progress || {}
  return Math.round(((p.generated || 0) + (p.extracted || 0) + (p.processed || 0)
    + (p.exported || 0) + (p.final || 0)) / (total * 5) * 100)
}
const editing = ref(null)            // {name, tags}
function startEdit() {
  editing.value = { name: store.currentSprite.name, tags: (spriteInfo.value?.tags || []).join(', ') }
}
async function saveEdit() {
  const e = editing.value
  if (!e?.name.trim()) return
  const tags = e.tags.split(/[,，、;；\s]+/).map(t => t.trim()).filter(Boolean)
  const sp = await api.patchSprite(store.currentSprite.id, { name: e.name.trim(), tags })
  store.currentSprite = { ...store.currentSprite, name: sp.name, tags: sp.tags }
  editing.value = null
  toast('已保存')
  await load()
}

// 多选：勾选后工具栏的批量操作直接带入选择
const selected = ref({})
const selectedIds = computed(() => actions.value.filter(a => selected.value[a.id]).map(a => a.id))
const allChecked = computed(() => actions.value.length > 0 && selectedIds.value.length === actions.value.length)
function toggleAll(e) {
  for (const a of actions.value) selected.value[a.id] = e.target.checked
}
const preselect = computed(() => selectedIds.value.length ? selectedIds.value : null)

// 工序 chip 直达工作台对应页签
const STAGE_TAB = { ff: 'firstframe', gen: 'generate', frm: 'extract', mat: 'background', exp: 'export' }
async function openAt(a, stageKey) {
  await openAction(store.currentSprite.id, a.id)
  currentTab.value = STAGE_TAB[stageKey] || 'firstframe'
}

async function load() {
  loading.value = true
  try {
    const [a, s, l, t, r] = await Promise.all([
      api.actions(store.currentSprite.id),
      api.sprites(),
      api.legacySessions(),
      api.templates().catch(() => ({ templates: [] })),
      api.spriteRefs(store.currentSprite.id).catch(() => ({ refs: [] })),
    ])
    actions.value = a.actions
    commonNames.value = s.common_action_names || []
    spriteInfo.value = s.sprites.find(x => x.id === store.currentSprite.id) || null
    legacy.value = l.sessions
    templatesById.value = {}
    for (const x of t.templates) templatesById.value[x.id] = x
    refs.value = r.refs
    coverV.value = Date.now() % 100000   // 首帧/帧有更新时封面绕过缓存
    // 已删除的动作从选择中剔除
    const ids = new Set(actions.value.map(x => x.id))
    for (const k of Object.keys(selected.value)) if (!ids.has(k)) delete selected.value[k]
  } finally {
    loading.value = false
  }
}

function tplLabel(a) {
  const t = templatesById.value[a.template_id]
  if (!t) return ''
  return `${t.variant || t.key}${t.duration_hint ? ` · ${t.duration_hint}s` : ''}`
}

// 工序阶段：首帧 → 视频 → 抽帧 → 抠图 → 导出
function stages(a) {
  const s = a.summary || {}
  return [
    { key: 'ff', label: '首帧', done: !!s.has_first_frame, text: '' },
    { key: 'gen', label: '视频', done: !!(s.generated_count || s.has_video),
      text: s.generating_count ? '生成中' : (s.generated_count > 1 ? `${s.generated_count}版` : ''),
      busy: !!s.generating_count },
    { key: 'frm', label: '抽帧', done: !!s.frame_count, text: s.frame_count ? `${s.frame_count}` : '' },
    { key: 'mat', label: '抠图', done: !!s.processed_count, text: s.processed_count ? `${s.processed_count}` : '' },
    { key: 'exp', label: '导出', done: !!s.export_count, text: s.export_count ? `${s.export_count}` : '' },
  ]
}

const finalCount = computed(() => actions.value.filter(a => a.status === 'final').length)

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

// 从工作台返回时定位并高亮刚离开的动作行
const highlightId = ref(null)
onMounted(async () => {
  await load()
  if (store.lastActionId) {
    highlightId.value = store.lastActionId
    store.lastActionId = null
    await nextTick()
    document.getElementById(`act-${highlightId.value}`)?.scrollIntoView({ block: 'center' })
    setTimeout(() => { highlightId.value = null }, 2500)
  }
})
</script>

<template>
  <div class="detail">
    <!-- 精灵摘要条 -->
    <div class="sp-head" v-if="!loading">
      <div class="sp-title">
        <h2>{{ store.currentSprite?.name }}</h2>
        <span v-for="t in spriteInfo?.tags || []" :key="t" class="tag">{{ t }}</span>
        <button class="small" title="重命名 / 改分类" @click="startEdit">编辑</button>
      </div>
      <div class="sp-meta">
        <div class="prog" :title="`五阶段平均完成度 ${pct(spriteInfo)}%`">
          <div class="prog-bar"><div class="prog-fill" :style="{ width: pct(spriteInfo) + '%' }"></div></div>
          <span class="prog-pct">{{ pct(spriteInfo) }}%</span>
        </div>
        <span class="hint" v-if="spriteInfo">
          生成 {{ spriteInfo.progress?.generated || 0 }} · 抽帧 {{ spriteInfo.progress?.extracted || 0 }}
          · 抠图 {{ spriteInfo.progress?.processed || 0 }} · 导出 {{ spriteInfo.progress?.exported || 0 }}
          · 定稿 {{ finalCount }}/{{ actions.length }}</span>
        <span class="sep">|</span>
        <span class="hint">立绘：</span>
        <span :class="hasFront ? 'ok-text' : 'warn-text'" class="lnk" title="打开首帧图库标记立绘" @click="ffLibOpen = true">
          正面 {{ hasFront ? '✓' : '未标记' }}</span>
        <span :class="hasBack ? 'ok-text' : 'hint'" class="lnk" title="打开首帧图库标记立绘" @click="ffLibOpen = true">
          背面 {{ hasBack ? '✓' : '未标记' }}</span>
      </div>
    </div>

    <div class="det-toolbar">
      <h2>动作列表
        <span class="count" v-if="!loading">{{ actions.length }}<template v-if="selectedIds.length"> · 已选 {{ selectedIds.length }}</template></span>
      </h2>
      <span class="spacer"></span>
      <button v-if="legacy.length" class="small" @click="claiming = !claiming">
        认领旧会话 ({{ legacy.length }})
      </button>
      <button v-if="actions.length" class="small" @click="ffLibOpen = true">首帧图库</button>
      <button v-if="actions.length" class="small" :title="preselect ? `对已选 ${selectedIds.length} 个` : ''"
              @click="ffGenOpen = true">生成首帧{{ preselect ? `（${selectedIds.length}）` : '' }}</button>
      <button v-if="actions.length" class="small" :title="preselect ? `对已选 ${selectedIds.length} 个` : ''"
              @click="batchGenOpen = true">批量生成{{ preselect ? `（${selectedIds.length}）` : '' }}</button>
      <button v-if="actions.length" class="small" :title="preselect ? `对已选 ${selectedIds.length} 个` : ''"
              @click="batchExtOpen = true">批量抽帧{{ preselect ? `（${selectedIds.length}）` : '' }}</button>
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

    <div v-else class="tbl-wrap">
      <table class="act-tbl">
        <thead>
          <tr>
            <th style="width:34px"><input type="checkbox" :checked="allChecked" title="全选/全不选" @change="toggleAll" /></th>
            <th style="width:64px">封面</th>
            <th style="width:22%">动作</th>
            <th style="width:18%">动作模板</th>
            <th>工序进度 <span class="hint" style="font-weight:400">（点工序直达）</span></th>
            <th style="width:150px">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in actions" :key="a.id" :id="`act-${a.id}`" class="act-row"
              :class="{ hl: highlightId === a.id, sel: selected[a.id] }" @click="open(a)">
            <td @click.stop><input type="checkbox" v-model="selected[a.id]" /></td>
            <td>
              <div class="cover">
                <img :src="`/api/sprites/${store.currentSprite.id}/actions/${a.id}/cover?v=${coverV}`"
                     @error="$event.target.style.display = 'none'" alt="" />
              </div>
            </td>
            <td>
              <div class="act-name">
                <b>{{ a.name }}</b>
                <span class="badge" :class="a.status">{{ STATUS_LABEL[a.status] || a.status }}</span>
              </div>
            </td>
            <td>
              <span v-if="tplLabel(a)" class="tpl">{{ tplLabel(a) }}</span>
              <span v-else class="hint">—</span>
            </td>
            <td>
              <div class="stages" @click.stop>
                <template v-for="(st, i) in stages(a)" :key="st.key">
                  <span v-if="i" class="stage-arrow">›</span>
                  <span class="stage clickable" :class="{ done: st.done, busy: st.busy }"
                        :title="`进入工作台 · ${st.label}`" @click="openAt(a, st.key)">
                    {{ st.label }}<em v-if="st.text"> {{ st.text }}</em>
                  </span>
                </template>
              </div>
            </td>
            <td class="ops" @click.stop>
              <button class="small" @click="open(a)">打开</button>
              <button class="small" @click="markFinal(a)">{{ a.status === 'final' ? '取消定稿' : '定稿' }}</button>
              <button class="small danger" @click="removeAction(a)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>

  <BatchGenerateModal v-if="batchGenOpen" :actions="actions" :preselected="preselect"
                      @close="batchGenOpen = false; load()"
                      @done="load()" />
  <BatchExtractModal v-if="batchExtOpen" :actions="actions" :preselected="preselect"
                     @close="batchExtOpen = false; load()"
                     @done="load()" />
  <BatchFfGenModal v-if="ffGenOpen" :actions="actions" :preselected="preselect"
                   @close="ffGenOpen = false; load()"
                   @done="load()" />

  <!-- 精灵重命名 / 改分类 -->
  <div v-if="editing" class="edit-mask" @click.self="editing = null">
    <div class="edit-box">
      <h3>编辑精灵</h3>
      <div class="field"><label>名称</label><input v-model="editing.name" @keyup.enter="saveEdit" /></div>
      <div class="field"><label>项目分类（逗号分隔，可多个）</label>
        <input v-model="editing.tags" placeholder="如：项目A, 主角" @keyup.enter="saveEdit" /></div>
      <div class="edit-ops">
        <button class="primary" @click="saveEdit">保存</button>
        <button @click="editing = null">取消</button>
      </div>
    </div>
  </div>
  <FirstFrameLibraryModal v-if="ffLibOpen"
                          :sprite-id="store.currentSprite.id"
                          :actions="actions"
                          @close="ffLibOpen = false"
                          @applied="load()" />
</template>

<style scoped>
.detail { padding: 16px 24px; height: 100%; overflow-y: auto; }
.sp-head {
  background: var(--bg-panel); border: 1px solid var(--border); border-radius: 6px;
  padding: 10px 14px; margin-bottom: 12px; display: flex; flex-direction: column; gap: 6px;
}
.sp-title { display: flex; align-items: center; gap: 8px; }
.sp-title h2 { font-size: 18px; margin: 0 4px 0 0; }
.tag { font-size: 11px; padding: 1px 8px; background: var(--bg-input); border-radius: 8px; color: var(--text-dim); }
.sp-meta { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 12px; }
.prog { display: flex; align-items: center; gap: 8px; width: 220px; }
.prog-bar { flex: 1; height: 6px; background: var(--bg-input); border-radius: 3px; overflow: hidden; }
.prog-fill { height: 100%; background: var(--accent); border-radius: 3px; transition: width .3s; }
.prog-pct { font-size: 12px; color: var(--text-dim); width: 34px; text-align: right; }
.sep { color: var(--border); }
.ok-text { color: var(--ok); }
.warn-text { color: var(--warn); }
.lnk { cursor: pointer; }
.lnk:hover { text-decoration: underline; }
.act-row.sel { background: #1e88e514; }
.stage.clickable { cursor: pointer; }
.stage.clickable:hover { outline: 1px solid var(--accent); }
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
.det-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
.det-toolbar h2 { font-size: 17px; margin: 0; }
.count { font-size: 13px; color: var(--text-dim); font-weight: 400; }
.spacer { flex: 1; }
.create-bar {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  background: var(--bg-panel); border: 1px solid var(--border); border-radius: 5px;
  padding: 10px 12px; margin-bottom: 12px;
}
.empty { text-align: center; padding: 60px 20px; color: var(--text-dim); }
.empty-icon { font-size: 42px; margin-bottom: 12px; }
.empty p { margin-bottom: 16px; max-width: 460px; margin-left: auto; margin-right: auto; }

.tbl-wrap { background: var(--bg-panel); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
.act-tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.act-tbl th {
  text-align: left; font-weight: 500; color: var(--text-dim); font-size: 12px;
  padding: 9px 14px; border-bottom: 1px solid var(--border); background: var(--bg-input);
}
.act-tbl td { padding: 7px 14px; border-bottom: 1px solid var(--border); vertical-align: middle; }
.act-tbl tbody tr:last-child td { border-bottom: none; }
.act-row { cursor: pointer; transition: background .12s; }
.act-row:hover { background: var(--bg-hover); }
.act-row.hl { background: #1e88e522; box-shadow: inset 3px 0 0 var(--accent); }

.cover {
  width: 46px; height: 46px; border-radius: 5px; overflow: hidden; background:
  repeating-conic-gradient(#2a2a2a 0% 25%, #202020 0% 50%) 0 0 / 12px 12px;
  display: flex; align-items: center; justify-content: center;
}
.cover img { width: 100%; height: 100%; object-fit: contain; }

.act-name { display: flex; align-items: center; gap: 8px; }
.badge { font-size: 10px; padding: 1px 8px; border-radius: 8px; background: var(--bg-input); color: var(--text-dim); flex: none; }
.badge.active { background: #1e88e533; color: var(--accent-hover); }
.badge.final { background: #4caf5033; color: var(--ok); }
.tpl { font-size: 12px; color: var(--text-dim); }

.stages { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.stage {
  font-size: 11px; padding: 2px 8px; border-radius: 9px;
  background: var(--bg-input); color: var(--text-dim); white-space: nowrap;
}
.stage.done { background: #4caf5026; color: var(--ok); }
.stage.busy { background: #ff980026; color: var(--warn); }
.stage em { font-style: normal; opacity: .85; }
.stage-arrow { color: var(--border); font-size: 12px; }

.ops { white-space: nowrap; }
.ops button { margin-right: 6px; }
.danger { border-color: var(--err); color: var(--err); }
</style>
