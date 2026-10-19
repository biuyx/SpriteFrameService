<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { useStore, gotoSprite, toast, askConfirm } from '../stores'
import api from '../api'
import { startJob } from '../jobs'
import BatchImportModal from '../components/BatchImportModal.vue'
import MembershipFields from '../components/MembershipFields.vue'
import LibraryTreeNode from '../components/LibraryTreeNode.vue'
import LibraryCatalogModal from '../components/LibraryCatalogModal.vue'
import { emptyCatalog, catalogTree, categoryPath, membershipLabel, matchesScope } from '../libraryModel'

const store = useStore()
const sprites = ref([])
const legacyCount = ref(0)
const loading = ref(true)
const batchOpen = ref(false)
const catalog = ref(emptyCatalog())
const catalogOpen = ref(false)
const selected = ref([])
const assignment = ref(null)
const assignmentPreview = ref(null)
const assigning = ref(false)
// 全局资源（项目包导入等）变动后重载列表
watch(() => store.libraryVersion, () => load())

// 工具栏状态（筛选/排序记在本机，下次进来保持）
const PREF_KEY = 'sfs.library.prefs'
let prefs = {}
try { prefs = JSON.parse(localStorage.getItem(PREF_KEY) || '{}') } catch { prefs = {} }
const keyword = ref(prefs.keyword || '')
const category = ref(prefs.category || '')          // '' = 全部分类
const sortBy = ref(prefs.sortBy || 'created_desc')
const projectId = ref(prefs.projectId || '*'), scope = ref(prefs.scope || '')
watch([keyword, category, sortBy, projectId, scope], ([k, c, s, p, sc]) => {
  try { localStorage.setItem(PREF_KEY, JSON.stringify({ keyword: k, category: c, sortBy: s, projectId: p, scope: sc })) } catch { /* ignore */ }
})
watch(projectId, () => { scope.value = ''; selected.value = [] })
watch(scope, () => { selected.value = [] })
const tree = computed(() => catalogTree(catalog.value, projectId.value))
const projectObj = computed(() => catalog.value.projects.find(p => p.id === projectId.value))
const currentType = computed(() => catalog.value.types.find(t => t.id === scope.value))
const counts = computed(() => Object.fromEntries([...catalog.value.categories, ...catalog.value.types].map(n => [n.id, sprites.value.filter(sp => matchesScope(catalog.value, sp, projectId.value, n.id)).length])))
const breadcrumb = computed(() => {
  if (!projectObj.value) return projectId.value==='unassigned'?'待归类':'全部项目'
  const cat = catalog.value.categories.find(c => c.id === scope.value)
  return [projectObj.value.name, categoryPath(catalog.value, currentType.value?.category_id || cat?.id), currentType.value?.name, scope.value==='untyped'?'未指定类型':''].filter(Boolean).join(' / ')
})
const newMembership = ref({project_id:null,sprite_type_id:null})
function startCreate(){newMembership.value={project_id:projectObj.value?.id||null,sprite_type_id:currentType.value?.id||null};creating.value=true}
function startAssignment(){assignment.value={project_id:projectObj.value?.id||null,sprite_type_id:currentType.value?.id||null};assignmentPreview.value=null}
async function previewAssignment(){assigning.value=true;try{assignmentPreview.value=await api.previewAssignment({sprite_ids:selected.value,...assignment.value})}catch(e){toast(e.message)}finally{assigning.value=false}}
async function applyAssignment(){assigning.value=true;try{await api.applyAssignment({sprite_ids:assignmentPreview.value.items.map(r=>r.id),project_id:assignmentPreview.value.project_id,sprite_type_id:assignmentPreview.value.sprite_type_id,revisions:Object.fromEntries(assignmentPreview.value.items.map(r=>[r.id,r.revision]))});assignment.value=null;selected.value=[];await load();toast('归类完成，原素材和绑定已保留')}catch(e){toast(e.message);assignmentPreview.value=null}finally{assigning.value=false}}
function toggleSelection(e){selected.value=e.target.checked?shown.value.map(s=>s.id):[]}

// 新建
const creating = ref(false)
const newName = ref('')
const newTags = ref('')
// 以已有角色为模板新建（复制动作骨架/模板绑定/提示词与参数记忆/工艺预设）
const fromSprite = ref('')
watch(() => newMembership.value.project_id, () => { fromSprite.value = '' })
const copyFirstFrames = ref(false)
const copyRefs = ref(false)
const creatingBusy = ref(false)
const fromSpriteObj = computed(() => sprites.value.find(s => s.id === fromSprite.value) || null)

// 编辑（改名 / 分类）
const editing = ref(null)         // {id, name, tags: 'a, b'}

async function load() {
  loading.value = true
  try {
    const [r, c] = await Promise.all([api.sprites(), api.libraryCatalog()])
    catalog.value = c
    if (!['*','unassigned'].includes(projectId.value) && !c.projects.some(p=>p.id===projectId.value)) projectId.value='*'
    if(scope.value && scope.value!=='untyped' && ![...c.types,...c.categories].some(n=>n.id===scope.value && n.project_id===projectId.value))scope.value=''
    sprites.value = r.sprites
    const l = await api.legacySessions()
    legacyCount.value = l.sessions.length
  } catch(e) { toast(`精灵库加载失败：${e.message}`) } finally {
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
  list = list.filter(sp => matchesScope(catalog.value, sp, projectId.value, scope.value))
  const kw = keyword.value.trim().toLowerCase()
  if (kw) list = list.filter(sp => [sp.name, membershipLabel(catalog.value, sp), ...(sp.tags||[])].join(' ').toLowerCase().includes(kw))
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
      ...newMembership.value,
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

// ---- 复制精灵（完整副本用于 A/B 抽卡对比）----
const copying = ref(null)   // {src, name, copy_first_frames, copy_refs, copy_workdata}
function startCopy(sp) {
  copying.value = { src: sp, name: `${sp.name}_副本`,
                    copy_first_frames: true, copy_refs: true, copy_workdata: true }
}
async function runCopy() {
  const c = copying.value
  if (!c || !c.name.trim()) return
  const payload = { name: c.name.trim(), tags: null,
                    copy_first_frames: c.copy_first_frames, copy_refs: c.copy_refs,
                    copy_workdata: c.copy_workdata }
  const srcName = c.src.name
  copying.value = null
  await startJob(() => api.copySprite(c.src.id, payload), {
    title: `复制精灵 ${srcName}`,
    onDone: async (r) => {
      const mb = r?.bytes ? ` · 素材 ${(r.bytes / 1048576).toFixed(0)}MB` : ''
      toast(`已复制为「${r?.name}」：动作 ${r?.actions} 个${mb}`)
      await load()
    },
  })
}

function startEdit(sp) {
  editing.value = { id: sp.id, name: sp.name, tags: (sp.tags || []).join(', ') }
}

async function saveEdit() {
  const e = editing.value
  if (!e || !e.name.trim()) return
  try {
    await api.patchSprite(e.id, { name: e.name.trim(), tags: parseTags(e.tags) })
    editing.value = null
    toast('已保存'); await load()
  } catch(err) { toast(err.message) }
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
    <div class="project-toolbar"><label>项目 <select v-model="projectId"><option value="*">全部项目</option><option value="unassigned">待归类（旧数据）</option><option v-for="p in catalog.projects" :key="p.id" :value="p.id">{{p.name}}</option></select></label><span class="hint">{{breadcrumb}}</span><span class="spacer"></span><button @click="catalogOpen=true">管理项目 / 分类 / 类型</button></div>
    <div class="library-layout"><aside class="library-tree"><button class="tree-top" :class="{primary:!scope}" @click="scope=''">{{projectObj?'项目全部精灵':'全部精灵'}}</button><template v-if="projectObj"><LibraryTreeNode v-for="n in tree" :key="n.id" :node="n" :active="scope" :counts="counts" @pick="scope=$event"/><button class="tree-top" @click="scope='untyped'">未指定类型</button></template><p v-else class="hint">先选择项目，浏览多级分类与精灵类型。旧角色保留在待归类中。</p></aside><main class="library-main">
    <div class="scope-header"><h2>{{currentType?.name || breadcrumb}}</h2><p v-if="currentType" class="hint">默认参考：{{currentType.template_binding?.group || '未配置'}} · {{currentType.template_binding?.first_frame_set_id?'已配置首帧集':'未配置首帧集'}}。仅新建继承，现有角色绑定保持。</p></div>
    <div class="lib-toolbar">
      <h2>精灵库 <span class="count" v-if="!loading">{{ shown.length }}<template v-if="shown.length !== sprites.length"> / {{ sprites.length }}</template></span></h2>
      <input v-model="keyword" class="search" placeholder="搜索名称、类型或标签…" />
      <select v-model="category" title="标签（保留旧分类）">
        <option value="">全部标签</option>
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
      <button :disabled="!selected.length" @click="startAssignment">归类所选 {{selected.length||''}}</button>
      <button class="primary" @click="startCreate">+ 新建{{currentType?.name || '精灵'}}</button>
    </div>

    <div v-if="creating" class="create-bar">
      <div class="row" style="gap:8px;align-items:center;flex-wrap:wrap;margin:0">
        <input v-model="newName" placeholder="精灵名称，如：厨师" style="width:180px" @keyup.enter="createSprite" autofocus />
        <input v-model="newTags" placeholder="标签（可选，逗号分隔）" style="width:200px" @keyup.enter="createSprite" />
        <MembershipFields v-model="newMembership" :catalog="catalog" />
        <div class="field inline"><label>模板角色</label>
          <select v-model="fromSprite" style="max-width:190px" title="以已有角色为模板：复制动作、模板绑定、提示词与参数记忆、工艺/导出预设">
            <option value="">不使用（空白新建）</option>
            <option v-for="s in sprites.filter(s=>!newMembership.project_id || s.project_id===newMembership.project_id)" :key="s.id" :value="s.id">{{ s.name }}（{{ s.action_count }} 动作）</option>
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
      <button class="primary" @click="startCreate">创建第一个精灵</button>
    </div>

    <div v-else class="tbl-wrap">
      <table class="lib-tbl">
        <thead>
          <tr>
            <th><input type="checkbox" aria-label="选择当前筛选全部精灵" :checked="!!shown.length && shown.every(s=>selected.includes(s.id))" @change="toggleSelection" /></th>
            <th style="width:18%">精灵</th>
            <th style="width:20%">项目 / 类型 / 标签</th>
            <th style="width:8%">动作</th>
            <th style="width:24%">完成进度</th>
            <th style="width:13%">创建时间</th>
            <th style="width:13%">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="sp in shown" :key="sp.id" class="sp-row" @click="open(sp)">
            <td @click.stop><input v-model="selected" type="checkbox" :value="sp.id" :aria-label="`选择 ${sp.name}`" /></td>
            <td>
              <div class="sp-name">
                <span class="avatar">{{ sp.name.slice(0, 2) }}</span>
                <b>{{ sp.name }}</b>
              </div>
            </td>
            <td>
              <div class="membership-path">{{membershipLabel(catalog,sp)}}</div>
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
              <button class="small" title="复制为副本（可含全部素材，用于 A/B 抽卡对比）"
                      @click="startCopy(sp)">复制</button>
              <button class="small danger" @click="removeSprite(sp)">删除</button>
            </td>
          </tr>
          <tr v-if="!shown.length">
            <td colspan="7" class="hint" style="text-align:center;padding:30px">没有匹配的精灵。可在当前项目和类型中新建，或调整筛选。</td>
          </tr>
        </tbody>
      </table>
    </div>
    </main></div>
  </div>

  <!-- 编辑名称 / 分类 -->
  <div v-if="editing" class="edit-mask" @click.self="editing = null">
    <div class="edit-box">
      <h3>编辑精灵</h3>
      <div class="field"><label>名称</label>
        <input v-model="editing.name" @keyup.enter="saveEdit" /></div>
      <div class="field"><label>标签（逗号分隔，可多个）</label>
        <input v-model="editing.tags" placeholder="如：项目A, 主角" @keyup.enter="saveEdit" /></div>
      <div class="edit-ops">
        <button class="primary" @click="saveEdit">保存</button>
        <button @click="editing = null">取消</button>
      </div>
    </div>
  </div>

  <div v-if="assignment" class="edit-mask" @click.self="!assigning && (assignment=null)"><div class="edit-box assignment-box"><h3>明确归类 · {{selected.length}} 个精灵</h3><MembershipFields v-model="assignment" :catalog="catalog" @update:model-value="assignmentPreview=null" /><p class="hint">仅更改项目和类型，保留原标签、素材路径、参考绑定、动作与工艺；不套用类型默认配置。</p><template v-if="assignmentPreview"><p>将归入：{{membershipLabel(catalog,assignmentPreview)}}</p><ul><li v-for="r in assignmentPreview.items" :key="r.id">{{r.name}}</li></ul></template><div class="edit-ops"><button :disabled="assigning" @click="assignment=null">取消</button><button :disabled="assigning" @click="previewAssignment">预览变更</button><button v-if="assignmentPreview" class="primary" :disabled="assigning" @click="applyAssignment">确认归类</button></div></div></div>
  <LibraryCatalogModal v-if="catalogOpen" :catalog="catalog" :project-id="projectObj?.id||''" @close="catalogOpen=false" @saved="load" />

  <!-- 复制精灵 -->
  <div v-if="copying" class="edit-mask" @click.self="copying = null">
    <div class="edit-box">
      <h3>复制精灵「{{ copying.src.name }}」</h3>
      <div class="field"><label>副本名称</label>
        <input v-model="copying.name" @keyup.enter="runCopy" autofocus /></div>
      <div class="field">
        <label>复制内容</label>
        <label class="chk" style="margin-top:4px"><input type="checkbox" v-model="copying.copy_first_frames" /> 首帧图</label>
        <label class="chk"><input type="checkbox" v-model="copying.copy_refs" /> 参考图库（含正面/背面立绘标记）</label>
        <label class="chk"><input type="checkbox" v-model="copying.copy_workdata" /> 全部素材：素材版本(视频)、帧、抠图、导出</label>
      </div>
      <p class="hint" style="margin:0 0 12px;line-height:1.7">
        副本与原角色完全独立，动作骨架（模板绑定、提示词与参数记忆、工艺/导出预设）一并复制。
        <template v-if="copying.copy_workdata">勾选「全部素材」后可在副本上重新抽卡，与原角色做 A/B 对比；
        体积较大，复制在后台进行。</template>
        <template v-else>不含素材时副本只有骨架与首帧，需重新生成。</template>
        编辑历史不复制。</p>
      <div class="edit-ops">
        <button class="primary" @click="runCopy">开始复制</button>
        <button @click="copying = null">取消</button>
      </div>
    </div>
  </div>

  <BatchImportModal v-if="batchOpen" :catalog="catalog" :membership="{project_id:projectObj?.id||null,sprite_type_id:currentType?.id||null}" @close="batchOpen = false"
                    @done="load()" />
</template>

<style scoped>
.library { padding: 16px 24px; height: 100%; overflow-y: auto; }
.project-toolbar{display:flex;gap:18px;align-items:center;flex-wrap:wrap;padding:8px 0 18px}.project-toolbar label{display:flex;gap:10px;align-items:center}.library-layout{display:flex;gap:22px}.library-tree{width:215px;flex:none;border-right:1px solid var(--border);padding-right:14px;overflow-wrap:anywhere}.library-main{min-width:0;flex:1}.tree-top{width:100%;text-align:left;margin-bottom:10px}.scope-header h2{margin:0 0 8px;font-size:20px}.scope-header{margin-bottom:18px}.tbl-wrap{overflow-x:auto!important}.assignment-box{width:620px!important}.assignment-box ul{max-height:240px;overflow:auto}@media(max-width:800px){.library-layout{display:block}.library-tree{width:100%;border:0;margin-bottom:14px}.library{padding:12px}}
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
.membership-path{font-size:12px;line-height:1.6;margin-bottom:4px;overflow-wrap:anywhere;color:var(--text-dim)}
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
