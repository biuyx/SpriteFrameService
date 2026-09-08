<script setup>
import { ref, computed, onMounted } from 'vue'
import { toast, askConfirm } from '../stores'
import api from '../api'

// 提示词库：按作用域分页签；每条多版本（编辑=新版本，可回滚）+ 绑定（全局/分组/key/模板）
const emit = defineEmits(['close', 'changed'])

const scopes = ref([])
const scopeLabels = ref({})
const scope = ref('video_ref')
const prompts = ref([])
const templates = ref([])
const loading = ref(true)
const changed = ref(false)

// 编辑器
const editor = ref(null)   // {mode:'create'|'edit', id, name, notes, text, note, versions, current, bindings:[{level,value}]}
const saving = ref(false)
const viewVersion = ref(null)

const LEVEL_TXT = { global: '全局默认', group: '分组', key: '动作 key', template: '模板' }

async function load() {
  loading.value = true
  try {
    const [p, t] = await Promise.all([api.prompts(), api.templates()])
    prompts.value = p.prompts
    scopes.value = p.scopes
    scopeLabels.value = p.scope_labels
    templates.value = t.templates
  } finally {
    loading.value = false
  }
}

const list = computed(() => prompts.value.filter(x => x.scope === scope.value)
  .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0)))
const groups = computed(() => [...new Set(templates.value.map(t => (t.group || '').trim()).filter(Boolean))].sort())
const keys = computed(() => [...new Set(templates.value.map(t => t.key))].sort())

function bindLabel(b) {
  if (b.level === 'global') return '全局默认'
  if (b.level === 'template') {
    const t = templates.value.find(x => x.id === b.value)
    return `模板: ${t ? (t.variant || t.key) : b.value}`
  }
  return `${LEVEL_TXT[b.level]}: ${b.value}`
}
function curText(rec) {
  return (rec.versions.find(v => v.v === rec.current) || rec.versions[rec.versions.length - 1] || {}).text || ''
}
function fmtTime(ts) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function openCreate() {
  editor.value = { mode: 'create', name: '', notes: '', text: '', note: '初始版本',
                   versions: [], current: null, bindings: [] }
}
function openEdit(rec) {
  editor.value = { mode: 'edit', id: rec.id, name: rec.name, notes: rec.notes || '',
                   text: curText(rec), origText: curText(rec), note: '',
                   versions: [...rec.versions].reverse(), current: rec.current,
                   bindings: rec.bindings.map(b => ({ ...b })) }
}

function addBinding() { editor.value.bindings.push({ level: 'key', value: '' }) }
function removeBinding(i) { editor.value.bindings.splice(i, 1) }

async function save() {
  const e = editor.value
  if (!e.text.trim()) return toast('提示词内容不能为空')
  const bindings = e.bindings.filter(b => b.level === 'global' || b.value)
  saving.value = true
  try {
    if (e.mode === 'create') {
      await api.createPrompt({ scope: scope.value, name: e.name, text: e.text,
                               notes: e.notes, note: e.note || '初始版本', bindings })
      toast('已创建')
    } else {
      if (e.text.trim() !== e.origText.trim()) {
        await api.addPromptVersion(e.id, e.text, e.note || '')
      }
      await api.patchPrompt(e.id, { name: e.name, notes: e.notes })
      await api.setPromptBindings(e.id, bindings)
      toast(e.text.trim() !== e.origText.trim() ? '已保存为新版本' : '已保存')
    }
    changed.value = true
    editor.value = null
    await load()
  } catch (err) {
    toast(`保存失败: ${err.message}`)
  } finally {
    saving.value = false
  }
}

async function rollback(v) {
  const e = editor.value
  if (!(await askConfirm(`回滚到 v${v.v}（${fmtTime(v.at)}）作为当前版本？`))) return
  await api.setPromptCurrent(e.id, v.v)
  changed.value = true
  await load()
  openEdit(prompts.value.find(x => x.id === e.id))
  toast(`已回滚到 v${v.v}`)
}

async function setGlobal(rec) {
  const others = rec.bindings.filter(b => b.level !== 'global')
  await api.setPromptBindings(rec.id, [{ level: 'global', value: '' }, ...others])
  changed.value = true
  await load()
  toast(`「${rec.name}」已设为${scopeLabels.value[scope.value]}的全局默认`)
}

async function remove(rec) {
  if (!(await askConfirm(`删除提示词「${rec.name}」（${rec.versions.length} 个版本）？已生成的记录不受影响。`,
                         { danger: true }))) return
  await api.deletePrompt(rec.id)
  changed.value = true
  await load()
}

function close() {
  emit('close')
  if (changed.value) emit('changed')
}

onMounted(load)
</script>

<template>
  <div class="modal-mask" @click.self="close">
    <div class="modal">
      <div class="modal-head">
        <h3>提示词库</h3>
        <div class="tabs">
          <button v-for="s in scopes" :key="s" class="tab" :class="{ on: scope === s }"
                  @click="scope = s">{{ scopeLabels[s] || s }}
            <span class="count">{{ prompts.filter(x => x.scope === s).length }}</span></button>
        </div>
        <span class="spacer"></span>
        <button class="small primary" @click="openCreate">+ 新建</button>
        <button class="small" @click="close">✕ 关闭</button>
      </div>
      <p class="hint" style="margin:0 0 10px">
        匹配优先级：动作记忆 › 模板 › 动作 key › 分组 › 全局默认 › 内置。编辑内容会生成新版本，可回滚；
        同一绑定位置只能挂一条（绑到新条目会自动从旧条目摘掉）。
        <template v-if="scope === 'video_i2v'">图生视频模板中的 <code>{action}</code> 会替换为动作名。</template></p>

      <div v-if="loading" class="hint" style="padding:26px;text-align:center">加载中...</div>
      <table v-else class="p-tbl">
        <thead><tr>
          <th style="width:22%">名称</th><th>绑定</th><th style="width:64px">版本</th>
          <th style="width:88px">更新</th><th style="width:170px">操作</th>
        </tr></thead>
        <tbody>
          <tr v-for="r in list" :key="r.id">
            <td><b>{{ r.name }}</b>
              <div class="dim" :title="curText(r)">{{ curText(r).slice(0, 48) }}{{ curText(r).length > 48 ? '…' : '' }}</div></td>
            <td>
              <span v-for="(b, i) in r.bindings" :key="i" class="chip" :class="b.level">{{ bindLabel(b) }}</span>
              <span v-if="!r.bindings.length" class="dim">未绑定（仅手动选用）</span>
            </td>
            <td>v{{ r.current }} <span class="dim">/{{ r.versions.length }}</span></td>
            <td class="dim">{{ fmtTime(r.updated_at) }}</td>
            <td class="ops">
              <button class="small" @click="openEdit(r)">编辑</button>
              <button v-if="!r.bindings.some(b => b.level === 'global')" class="small"
                      title="设为本作用域的全局默认" @click="setGlobal(r)">设默认</button>
              <button class="small danger" @click="remove(r)">删除</button>
            </td>
          </tr>
          <tr v-if="!list.length"><td colspan="5" class="dim" style="text-align:center;padding:20px">该作用域还没有提示词</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 编辑器 -->
  <div v-if="editor" class="modal-mask inner" @click.self="editor = null">
    <div class="editor">
      <div class="modal-head">
        <h3>{{ editor.mode === 'create' ? '新建提示词' : '编辑提示词' }}
          <span class="count">{{ scopeLabels[scope] }}</span></h3>
        <span class="spacer"></span>
        <button class="small" @click="editor = null">✕</button>
      </div>
      <div class="row" style="gap:10px">
        <div class="field" style="flex:1"><label>名称</label><input v-model="editor.name" placeholder="如：走路·参考视频" /></div>
        <div class="field" style="flex:2"><label>备注</label><input v-model="editor.notes" placeholder="可空" /></div>
      </div>
      <div class="field"><label>提示词内容{{ editor.mode === 'edit' ? `（当前 v${editor.current}；改动后保存即生成新版本）` : '' }}</label>
        <textarea v-model="editor.text" rows="6" style="width:100%;resize:vertical"></textarea></div>
      <div class="field" v-if="editor.mode === 'edit' && editor.text.trim() !== editor.origText.trim()">
        <label>版本说明（可空）</label><input v-model="editor.note" placeholder="这版改了什么" /></div>

      <div class="sec">
        <div class="row" style="align-items:center;margin-bottom:6px">
          <b style="font-size:13px">绑定（匹配动作）</b>
          <button class="small" @click="addBinding">+ 添加</button>
        </div>
        <div v-for="(b, i) in editor.bindings" :key="i" class="row" style="gap:8px;align-items:center;margin-bottom:6px">
          <select v-model="b.level" @change="b.value = ''">
            <option value="global">全局默认</option>
            <option value="group">分组</option>
            <option value="key">动作 key</option>
            <option value="template">模板</option>
          </select>
          <select v-if="b.level === 'group'" v-model="b.value" style="min-width:160px">
            <option value="">选择分组…</option>
            <option v-for="g in groups" :key="g" :value="g">{{ g }}</option>
          </select>
          <select v-else-if="b.level === 'key'" v-model="b.value" style="min-width:200px">
            <option value="">选择动作 key…</option>
            <option v-for="k in keys" :key="k" :value="k">{{ k }}</option>
          </select>
          <select v-else-if="b.level === 'template'" v-model="b.value" style="min-width:220px">
            <option value="">选择模板…</option>
            <option v-for="t in templates" :key="t.id" :value="t.id">{{ t.key }} · {{ t.variant || '—' }}</option>
          </select>
          <span v-else class="dim">本作用域无其它命中时使用</span>
          <button class="small danger" @click="removeBinding(i)">✕</button>
        </div>
        <p v-if="!editor.bindings.length" class="dim" style="margin:0">未绑定：只能在生成面板手动选用。</p>
      </div>

      <div v-if="editor.mode === 'edit' && editor.versions.length" class="sec">
        <b style="font-size:13px">版本历史</b>
        <div class="ver-list">
          <div v-for="v in editor.versions" :key="v.v" class="ver-row" :class="{ cur: v.v === editor.current }">
            <b>v{{ v.v }}</b>
            <span class="dim">{{ fmtTime(v.at) }}</span>
            <span class="dim ver-note" :title="v.note">{{ v.note || '' }}</span>
            <span class="spacer"></span>
            <span v-if="v.v === editor.current" class="ok-text">当前</span>
            <button class="small" @click="viewVersion = v">查看</button>
            <button v-if="v.v !== editor.current" class="small" @click="rollback(v)">回滚</button>
          </div>
        </div>
      </div>

      <div class="modal-foot">
        <button class="primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
        <button @click="editor = null">取消</button>
      </div>
    </div>
  </div>

  <!-- 版本查看 -->
  <div v-if="viewVersion" class="modal-mask inner2" @click.self="viewVersion = null">
    <div class="editor" style="width:520px">
      <div class="modal-head"><b>v{{ viewVersion.v }}</b><span class="dim">{{ fmtTime(viewVersion.at) }} {{ viewVersion.note }}</span>
        <span class="spacer"></span><button class="small" @click="viewVersion = null">✕</button></div>
      <pre class="ver-text">{{ viewVersion.text }}</pre>
    </div>
  </div>
</template>

<style scoped>
.modal-mask {
  position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 90;
  display: flex; align-items: center; justify-content: center;
}
.modal-mask.inner { z-index: 95; }
.modal-mask.inner2 { z-index: 97; }
.modal {
  width: 880px; max-width: 94vw; max-height: 88vh; overflow-y: auto;
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 20px 18px;
}
.editor {
  width: 680px; max-width: 94vw; max-height: 88vh; overflow-y: auto;
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 20px 18px;
}
.modal-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }
.modal-head h3 { margin: 0; font-size: 16px; }
.count { font-size: 12px; color: var(--text-dim); font-weight: 400; }
.spacer { flex: 1; }
.tabs { display: flex; gap: 2px; margin-left: 8px; }
.tab {
  background: transparent; border: 1px solid transparent; border-bottom: 2px solid transparent;
  border-radius: 4px 4px 0 0; padding: 4px 12px; font-size: 13px; color: var(--text-dim);
}
.tab.on { color: var(--text); border-bottom-color: var(--accent); background: var(--bg-input); }
.p-tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.p-tbl th {
  text-align: left; font-weight: 500; color: var(--text-dim); font-size: 12px;
  padding: 7px 10px; border-bottom: 1px solid var(--border); background: var(--bg-input);
}
.p-tbl td { padding: 7px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
.p-tbl tbody tr:last-child td { border-bottom: none; }
.p-tbl tbody tr:hover { background: var(--bg-hover); }
.dim { color: var(--text-dim); font-size: 12px; }
.ok-text { color: var(--ok); font-size: 12px; }
.chip {
  display: inline-block; font-size: 11px; padding: 1px 8px; border-radius: 8px;
  background: var(--bg-input); color: var(--text-dim); margin: 0 4px 3px 0;
}
.chip.global { background: #1e88e533; color: var(--accent-hover); }
.chip.template { background: #4caf5026; color: var(--ok); }
.ops { white-space: nowrap; }
.ops button { margin-right: 5px; }
.danger { border-color: var(--err); color: var(--err); }
.field { margin-bottom: 10px; }
.field input, .field textarea { width: 100%; }
.sec {
  border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px;
  margin-bottom: 10px; background: var(--bg-input);
}
.ver-list { margin-top: 6px; }
.ver-row { display: flex; align-items: center; gap: 10px; padding: 4px 0; font-size: 12px; border-bottom: 1px solid var(--border); }
.ver-row:last-child { border-bottom: none; }
.ver-row.cur b { color: var(--ok); }
.ver-note { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ver-text { white-space: pre-wrap; font-size: 12px; line-height: 1.6; background: var(--bg-input); padding: 10px; border-radius: 5px; max-height: 50vh; overflow-y: auto; }
.modal-foot { display: flex; gap: 10px; justify-content: flex-end; margin-top: 6px; }
</style>
