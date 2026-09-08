<script setup>
import { ref, computed, onMounted } from 'vue'
import { toast, askConfirm } from '../stores'
import api from '../api'

const emit = defineEmits(['close', 'changed'])

const templates = ref([])
const ossConfigured = ref(false)
const loading = ref(true)
const changed = ref(false)

const fileInput = ref(null)
const uploading = ref(false)
const scanDir = ref('')
const scanning = ref(false)
const groupFilter = ref('')       // '' = 全部, '__none__' = 未分组
const uploadGroup = ref('')       // 上传/目录导入 归入的分组

const preview = ref(null)      // 预览中的模板
const editing = ref(null)      // {id, key, variant, duration_hint}
const ossBusy = ref({})        // id -> bool
const batchOss = ref(null)     // {done, total} 批量上传进度

async function load() {
  loading.value = true
  try {
    const [t, s] = await Promise.all([
      api.templates(),
      api.settings().catch(() => null),
    ])
    templates.value = t.templates
    ossConfigured.value = !!s?.oss?.configured
  } finally {
    loading.value = false
  }
}

const groups = computed(() =>
  [...new Set(templates.value.map(t => (t.group || '').trim()).filter(Boolean))].sort())
const sorted = computed(() => {
  let list = templates.value
  if (groupFilter.value === '__none__') list = list.filter(t => !(t.group || '').trim())
  else if (groupFilter.value) list = list.filter(t => (t.group || '').trim() === groupFilter.value)
  return [...list].sort((a, b) => (a.key + a.variant).localeCompare(b.key + b.variant, 'zh'))
})
const ossPending = computed(() => templates.value.filter(t => !t.oss_url))

async function clearRule(t) {
  if (!(await askConfirm(`清除模板「${t.variant || t.key}」的抽帧规则？`))) return
  await api.patchTemplate(t.id, { extract_rule: null })
  changed.value = true
  await load()
  toast('已清除抽帧规则')
}

async function onFiles(files) {
  if (!files?.length) return
  uploading.value = true
  let ok = 0
  const fails = []
  for (const f of files) {
    try {
      await api.uploadTemplate(f, uploadGroup.value.trim())
      ok++
    } catch (e) {
      fails.push(`${f.name}: ${e.message}`)
    }
  }
  uploading.value = false
  if (fileInput.value) fileInput.value.value = ''
  changed.value = true
  await load()
  toast(fails.length ? `导入 ${ok} 个，失败 ${fails.length} 个：${fails[0]}` : `已导入 ${ok} 个模板`)
}

async function runScan() {
  const dir = scanDir.value.trim()
  if (!dir) return toast('请填写模板视频所在目录')
  scanning.value = true
  try {
    const r = await api.scanTemplates(dir, uploadGroup.value.trim())
    changed.value = true
    await load()
    toast(`目录导入完成：新增 ${r.imported_count} 个，跳过 ${r.skipped.length} 个（已存在）`)
  } catch (e) {
    toast(`导入失败: ${e.message}`)
  } finally {
    scanning.value = false
  }
}

async function uploadOss(t) {
  ossBusy.value[t.id] = true
  try {
    await api.templateOssUpload(t.id)
    changed.value = true
    await load()
    toast(`「${t.variant || t.key}」已上传 OSS`)
  } catch (e) {
    toast(`上传失败: ${e.message}`)
  } finally {
    ossBusy.value[t.id] = false
  }
}

async function uploadAllOss() {
  const pending = ossPending.value
  if (!pending.length) return toast('全部模板都已上传 OSS')
  if (!(await askConfirm(`把 ${pending.length} 个未上传的模板传到 OSS？`))) return
  batchOss.value = { done: 0, total: pending.length }
  let fail = 0
  for (const t of pending) {
    try { await api.templateOssUpload(t.id) } catch { fail++ }
    batchOss.value.done++
  }
  batchOss.value = null
  changed.value = true
  await load()
  toast(fail ? `OSS 上传完成：${pending.length - fail} 成功 / ${fail} 失败` : 'OSS 上传全部完成')
}

function startEdit(t) {
  editing.value = { id: t.id, key: t.key, variant: t.variant,
                    duration_hint: t.duration_hint, group: t.group || '' }
}

async function saveEdit() {
  const e = editing.value
  if (!e) return
  try {
    await api.patchTemplate(e.id, {
      key: e.key, variant: e.variant,
      duration_hint: e.duration_hint || null,
      group: e.group || '',
    })
    editing.value = null
    changed.value = true
    await load()
    toast('已保存')
  } catch (err) {
    toast(`保存失败: ${err.message}`)
  }
}

async function remove(t) {
  if (!(await askConfirm(`删除模板「${t.filename}」？已绑定该模板的动作生成时将提示无模板。`,
                         { danger: true }))) return
  await api.deleteTemplate(t.id)
  changed.value = true
  await load()
  toast('已删除')
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
        <h3>参考视频库 <span class="count" v-if="!loading">{{ templates.length }}</span></h3>
        <span class="spacer"></span>
        <button class="small" @click="close">✕ 关闭</button>
      </div>

      <div class="ops-row">
        <select v-model="groupFilter" title="按分组筛选">
          <option value="">全部分组</option>
          <option value="__none__">未分组</option>
          <option v-for="g in groups" :key="g" :value="g">{{ g }}</option>
        </select>
        <button class="small" :disabled="uploading" @click="fileInput.click()">
          {{ uploading ? '导入中…' : '上传视频（可多选）' }}</button>
        <input ref="fileInput" type="file" accept="video/*" multiple style="display:none"
               @change="e => onFiles([...e.target.files])" />
        <input v-model="scanDir" placeholder="或填目录路径批量导入，如 D:\素材\动作模板" style="flex:1;min-width:170px" />
        <button class="small" :disabled="scanning" @click="runScan">{{ scanning ? '扫描中…' : '目录导入' }}</button>
        <input v-model="uploadGroup" list="tpl-groups" placeholder="导入到分组（可空）" style="width:140px"
               title="上传/目录导入 归入的分组名" />
        <datalist id="tpl-groups"><option v-for="g in groups" :key="g" :value="g" /></datalist>
        <button class="small" :disabled="!ossConfigured || !!batchOss || !ossPending.length"
                :title="ossConfigured ? '' : '先在 ⚙ 设置 里配置 OSS'" @click="uploadAllOss">
          {{ batchOss ? `OSS 上传中 ${batchOss.done}/${batchOss.total}` : `全部传OSS（${ossPending.length}）` }}</button>
      </div>
      <p class="hint" style="margin:0 0 8px">
        文件名即元数据：<code>01_idle_front (待机)（4秒）.mp4</code> → key / 变体 / 推荐时长。
        <span v-if="!ossConfigured" class="warn-text">OSS 未配置——生成时参考视频需要公网 URL，请先在「⚙ 设置」里配置。</span>
        <span v-else>OSS 未上传的模板会在首次生成时自动上传，这里可提前批量传好。</span>
      </p>

      <div v-if="loading" class="hint" style="padding:30px;text-align:center">加载中...</div>
      <div v-else-if="!templates.length" class="hint" style="padding:30px;text-align:center">
        库是空的——上传视频或填目录批量导入。</div>

      <div v-else class="tbl-scroll">
        <table class="tpl-tbl">
          <thead>
            <tr>
              <th>动作 key</th><th>变体</th><th style="width:90px">分组</th>
              <th style="width:50px">时长</th>
              <th style="width:110px">抽帧规则</th>
              <th style="width:70px">OSS</th>
              <th style="width:200px">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="t in sorted" :key="t.id">
              <td class="mono-cell">{{ t.key }}</td>
              <td>{{ t.variant || '—' }}</td>
              <td><span v-if="(t.group || '').trim()" class="grp">{{ t.group }}</span>
                  <span v-else class="dim">—</span></td>
              <td>{{ t.duration_hint ? t.duration_hint + 's' : '—' }}</td>
              <td>
                <span v-if="t.extract_rule" class="ok-text"
                      title="在工作台/动作分析里点「保存为模板规则」可更新">
                  {{ t.extract_rule.start }}–{{ t.extract_rule.end }}s @{{ t.extract_rule.fps
                  }}{{ t.extract_rule.keep ? ` 留${t.extract_rule.keep.length}` : '' }}</span>
                <span v-else class="dim">—</span>
              </td>
              <td>
                <span v-if="t.oss_url" class="ok-text" :title="t.oss_url">✓</span>
                <span v-else class="dim">未传</span>
              </td>
              <td class="ops">
                <button class="small" @click="preview = t">预览</button>
                <button v-if="!t.oss_url" class="small" :disabled="!ossConfigured || ossBusy[t.id]"
                        @click="uploadOss(t)">{{ ossBusy[t.id] ? '传…' : '传OSS' }}</button>
                <button class="small" @click="startEdit(t)">编辑</button>
                <button v-if="t.extract_rule" class="small" title="清除抽帧规则" @click="clearRule(t)">清规则</button>
                <button class="small danger" @click="remove(t)">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- 预览 -->
  <div v-if="preview" class="modal-mask inner" @click.self="preview = null">
    <div class="prev-box">
      <div class="modal-head">
        <b>{{ preview.key }}{{ preview.variant ? ` · ${preview.variant}` : '' }}</b>
        <span class="spacer"></span>
        <button class="small" @click="preview = null">✕ 关闭</button>
      </div>
      <video :src="api.templateVideoUrl(preview.id)" controls autoplay loop
             style="width:100%;max-height:60vh;background:#000"></video>
    </div>
  </div>

  <!-- 编辑 -->
  <div v-if="editing" class="modal-mask inner" @click.self="editing = null">
    <div class="edit-box">
      <h3>编辑模板</h3>
      <div class="field"><label>动作 key（与动作名匹配用）</label>
        <input v-model="editing.key" @keyup.enter="saveEdit" /></div>
      <div class="field"><label>变体名（可空）</label>
        <input v-model="editing.variant" @keyup.enter="saveEdit" /></div>
      <div class="field"><label>分组（可空 = 未分组）</label>
        <input v-model="editing.group" list="tpl-groups" @keyup.enter="saveEdit" /></div>
      <div class="field"><label>推荐时长（秒，可空）</label>
        <input v-model.number="editing.duration_hint" type="number" min="1" max="60" /></div>
      <div class="edit-ops">
        <button class="primary" @click="saveEdit">保存</button>
        <button @click="editing = null">取消</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-mask {
  position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 90;
  display: flex; align-items: center; justify-content: center;
}
.modal-mask.inner { z-index: 95; background: rgba(0,0,0,.5); }
.modal {
  width: 860px; max-width: 94vw; max-height: 86vh; display: flex; flex-direction: column;
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 20px 18px;
}
.modal-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.modal-head h3 { margin: 0; font-size: 16px; }
.count { font-size: 13px; color: var(--text-dim); font-weight: 400; }
.spacer { flex: 1; }
.ops-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.warn-text { color: var(--warn); }
.ok-text { color: var(--ok); font-size: 12px; }
.dim { color: var(--text-dim); font-size: 12px; }

.tbl-scroll { flex: 1; min-height: 0; overflow-y: auto; border: 1px solid var(--border); border-radius: 5px; }
.tpl-tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tpl-tbl th {
  text-align: left; font-weight: 500; color: var(--text-dim); font-size: 12px;
  padding: 7px 10px; border-bottom: 1px solid var(--border); background: var(--bg-input);
  position: sticky; top: 0;
}
.tpl-tbl td { padding: 6px 10px; border-bottom: 1px solid var(--border); }
.tpl-tbl tbody tr:last-child td { border-bottom: none; }
.tpl-tbl tbody tr:hover { background: var(--bg-hover); }
.mono-cell { font-family: Consolas, monospace; font-size: 12px; }
.grp {
  font-size: 11px; padding: 1px 8px; background: var(--bg-input); border-radius: 8px;
  color: var(--text-dim); display: inline-block; max-width: 92px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: middle;
}
.ops { white-space: nowrap; }
.ops button { margin-right: 5px; }
.danger { border-color: var(--err); color: var(--err); }

.prev-box {
  width: 640px; max-width: 92vw; background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px;
}
.edit-box {
  width: 340px; max-width: 92vw; background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: 8px; padding: 18px 20px;
}
.edit-box h3 { margin: 0 0 12px; font-size: 15px; }
.edit-box .field { margin-bottom: 12px; }
.edit-box input { width: 100%; }
.edit-ops { display: flex; gap: 10px; justify-content: flex-end; }
</style>
