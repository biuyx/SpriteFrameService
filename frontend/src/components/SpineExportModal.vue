<script setup>
import { ref, computed, onMounted } from 'vue'
import { useStore, toast, askConfirm } from '../stores'
import { startJob } from '../jobs'
import api from '../api'

const emit = defineEmits(['close'])
const store = useStore()

// 尺寸只由画布决定：帧等比压进画布，结果与源帧分辨率无关，也不会和别处的缩放叠乘
const PRESETS = {
  efrenter: { label: '既有工程同款（128 画布）', canvas: 128 },
  big: { label: '256 画布', canvas: 256 },
  full: { label: '320 画布', canvas: 320 },
  custom: { label: '自定义', canvas: null },
}

const preset = ref('efrenter')
const name = ref('')
const canvas = ref(128)
const fps = ref(12)
const loop = ref(true)
const atlas = ref(true)
const pattern = ref('{anim}_{i:04d}')
const startIndex = ref(1)

const loading = ref(true)
const items = ref([])
const conflicts = ref([])
const checked = ref({})
// 逐动作的动画名：默认是后端推断值，可在表里改；勾上「记住」就落到动作上
const names = ref({})
const remember = ref(true)
const running = ref(false)
const result = ref(null)

function applyPreset(k) {
  const p = PRESETS[k]
  if (!p || k === 'custom') return
  canvas.value = p.canvas
}

// 描边取精灵预设，在压进画布之后执行——填几 px 成品就是几 px
const outlinePreset = computed(() => store.currentSprite?.preset?.outline || null)
const outlineOn = ref(true)

// 导出模板：从既有 Spine 工程反解出的约定（版本/帧名/画布/逐动画对齐偏移）
const templates = ref([])
const templateId = ref('')
const importPath = ref('')
const importing = ref(false)
const activeTemplate = computed(() =>
  templates.value.find(t => t.id === templateId.value) || null)

async function loadTemplates() {
  try { templates.value = (await api.spineTemplates()).templates } catch { /* ignore */ }
}

// 已有产物：手动导的和流水线收口导的都在这里，随时可再下载
const exports = ref([])

async function loadExports() {
  try {
    exports.value = (await api.spineExports(store.currentSprite.id)).exports
  } catch { /* ignore */ }
}

function downloadExport(name) {
  window.open(api.spineDownload(store.currentSprite.id, name), '_blank')
}

async function removeExport(e) {
  if (!(await askConfirm(`删除导出产物「${e.name}」？共 ${e.files} 个文件、${fmtSize(e.bytes)}。`,
                         { danger: true }))) return
  try {
    await api.deleteSpineExport(store.currentSprite.id, e.name)
    await loadExports()
    toast(`已删除「${e.name}」`)
  } catch (err) {
    toast(`删除失败: ${err.message}`)
  }
}

function fmtSize(n) {
  if (!n) return '0'
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB'
  return (n / 1024 / 1024).toFixed(1) + ' MB'
}

function fmtTime(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const p = (x) => String(x).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function runImport() {
  const p = importPath.value.trim()
  if (!p) return toast('填一下参考工程的目录，或 .json / .skel 文件路径')
  importing.value = true
  try {
    const r = await api.importSpineTemplate({ path: p })
    await loadTemplates()
    templateId.value = r.template.id
    importPath.value = ''
    const t = r.template
    toast(`已导入模板「${t.name}」：${t.animations.length} 个动画 · `
          + `Spine ${t.spine_version} · 画布 ${t.canvas || '未知'}`
          + (r.warnings.length ? `（${r.warnings[0]}）` : ''))
  } catch (e) {
    toast(`导入失败: ${e.message}`)
  } finally {
    importing.value = false
  }
}

function animOf(i) { return (names.value[i.action_id] ?? i.anim).trim() }
function defaultOf(i) { return i.suggest || i.anim }   // 不看自定义时的推断值
function resetName(i) { names.value[i.action_id] = defaultOf(i) }
// 用模板导时，名字对不上模板里的动画就等于新加一个动画——标出来让人确认
function offTemplate(i) {
  const t = activeTemplate.value
  return !!t && !(t.animation_names || []).includes(animOf(i))
}

// 模板里有、但这次导不出来的动画——换模板时最该看的就是这个
const coverage = computed(() => {
  const t = activeTemplate.value
  if (!t) return null
  const mine = new Set(selected.value.map(animOf))
  const theirs = t.animation_names || []
  return {
    missing: theirs.filter(n => !mine.has(n)),
    extra: [...mine].filter(n => !theirs.includes(n)),
    total: theirs.length,
  }
})

async function loadPreview(keepChecked = false) {
  const r = await api.spinePreview(store.currentSprite.id)
  items.value = r.items
  conflicts.value = r.conflicts || []
  for (const it of items.value) {
    names.value[it.action_id] = it.anim
    if (!keepChecked) checked.value[it.action_id] = it.frames > 0
  }
}

onMounted(async () => {
  name.value = store.currentSprite?.name || ''
  outlineOn.value = !!outlinePreset.value?.enabled
  await Promise.all([loadTemplates(), loadExports()])
  try {
    await loadPreview()
  } catch (e) {
    toast(`读取动作失败: ${e.message}`)
  } finally {
    loading.value = false
  }
})

const selected = computed(() => items.value.filter(i => checked.value[i.action_id]))
const selectedFrames = computed(() => selected.value.reduce((s, i) => s + i.frames, 0))
// 选中的动作里有没有还没抠图的帧——带背景导出到 Spine 里会是方块
const unprocessed = computed(() =>
  selected.value.filter(i => i.processed < i.frames))
const dupSelected = computed(() => {
  const seen = {}
  for (const i of selected.value) {
    const n = animOf(i)
    seen[n] = (seen[n] || 0) + 1
  }
  return Object.keys(seen).filter(k => seen[k] > 1)
})
const blankNames = computed(() => selected.value.filter(i => !animOf(i)))
const renamed = computed(() => items.value.filter(i => animOf(i) !== defaultOf(i)))

function selectAll() { for (const i of items.value) checked.value[i.action_id] = i.frames > 0 }
function selectNone() { for (const i of items.value) checked.value[i.action_id] = false }

async function run() {
  if (!selected.value.length) return toast('请至少勾选一个动作')
  if (blankNames.value.length)
    return toast(`这些动作还没填动画名：${blankNames.value.map(i => i.name).join('、')}`)
  if (dupSelected.value.length)
    return toast(`动画名重复：${dupSelected.value.join('、')}——同名会互相覆盖，请改掉其中一个`)
  running.value = true
  result.value = null
  const payload = {
    name: name.value.trim() || undefined,
    action_ids: selected.value.map(i => i.action_id),
    // 逐动作的名字都带上：后端跟推断值一致的会自动不记，只留真改过的
    anim_names: Object.fromEntries(selected.value.map(i => [i.action_id, animOf(i)])),
    remember_names: remember.value,
    fps: fps.value,
    canvas: canvas.value || null,
    frame_pattern: pattern.value.trim() || '{anim}_{i:04d}',
    start_index: startIndex.value,
    loop: loop.value,
    atlas: atlas.value,
    // 不勾就显式关掉，别让后端回落到精灵预设
    outline: outlineOn.value && outlinePreset.value
      ? { ...outlinePreset.value, enabled: true }
      : { enabled: false },
  }
  // 用模板时，版本/帧名/画布/对齐偏移都交给模板，这里不再覆盖
  if (templateId.value) {
    payload.template_id = templateId.value
    delete payload.canvas
    delete payload.frame_pattern
    delete payload.start_index
  }
  try {
    await startJob(() => api.spineExport(store.currentSprite.id, payload), {
      title: 'Spine 导出',
      onDone: (r) => {
        result.value = r
        running.value = false
        loadExports()
        // 记住改名后「自定义」标记要跟着变，重拉一次预览（保留勾选）
        if (remember.value) loadPreview(true).catch(() => {})
      },
      onError: () => { running.value = false },
    })
  } catch {
    running.value = false
  }
}

function download() {
  window.open(api.spineDownload(store.currentSprite.id, result.value.name), '_blank')
}
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <div class="modal-head">
        <h3>导出 Spine 资源</h3>
        <span class="spacer"></span>
        <button class="small" @click="emit('close')">✕ 关闭</button>
      </div>

      <p class="hint" style="margin:0 0 10px">
        产出骨架 JSON + 图集（.atlas/.png）+ images/ 序列帧。
        美术在 Spine 里 <b>Import Data</b> 选该 JSON 即可打开为工程，另存为 .spine。
        动画名默认按参考视频库里的设定推断，可在下表逐个改；选了导出模板时，
        输入框会列出模板里已有的动画名，挑一个就对上去了。
      </p>

      <div v-if="loading" class="hint" style="padding:26px;text-align:center">加载动作中...</div>

      <template v-else>
        <div class="ops-row tpl-row">
          <div class="field inline"><label>导出模板</label>
            <select v-model="templateId" style="min-width:170px"
                    title="从既有 Spine 工程反解出的导出约定：版本、帧名格式、画布、逐动画对齐偏移">
              <option value="">不用模板（按下面的参数导）</option>
              <option v-for="t in templates" :key="t.id" :value="t.id">
                {{ t.name }}（{{ t.animation_count }} 个动画 · Spine {{ t.spine_version }}）
              </option>
            </select>
          </div>
          <input v-model="importPath" style="flex:1;min-width:160px"
                 placeholder="导入参考工程：目录 或 .json / .skel 路径"
                 @keyup.enter="runImport" />
          <button class="small" :disabled="importing" @click="runImport">
            {{ importing ? '解析中…' : '导入' }}</button>
        </div>

        <div v-if="activeTemplate" class="tpl-info">
          <b>{{ activeTemplate.name }}</b>
          · Spine {{ activeTemplate.spine_version }}
          · 画布 {{ activeTemplate.canvas || '未知' }}
          · 帧名 <code>{{ activeTemplate.frame_pattern }}</code> 从 {{ activeTemplate.start_index }} 起
          <template v-if="activeTemplate.atlas?.scale && activeTemplate.atlas.scale !== 1">
            · 图集压缩 {{ activeTemplate.atlas.scale }}×
          </template>
          <div class="dim" style="margin-top:3px">
            版本、帧名格式、画布、每个动画的对齐偏移与帧率都按模板走，下面的同名参数不再生效。
          </div>
          <div v-if="coverage && coverage.missing.length" class="warn-text" style="margin-top:3px">
            模板里有 {{ coverage.total }} 个动画，这次缺 {{ coverage.missing.length }} 个：
            {{ coverage.missing.slice(0, 6).join('、') }}{{ coverage.missing.length > 6 ? ' …' : '' }}
          </div>
          <div v-if="coverage && coverage.extra.length" class="warn-text" style="margin-top:3px">
            这些动画模板里没有：{{ coverage.extra.join('、') }}（会按默认约定导出）
          </div>
        </div>

        <div class="ops-row" :class="{ dimmed: !!templateId }">
          <div class="field inline"><label>骨架名</label>
            <input v-model="name" style="width:150px" placeholder="默认用精灵名" /></div>
          <div class="field inline"><label>预设</label>
            <select v-model="preset" @change="applyPreset(preset)">
              <option v-for="(p, k) in PRESETS" :key="k" :value="k">{{ p.label }}</option>
            </select>
          </div>
          <div class="field inline"><label>画布</label>
            <input type="number" v-model.number="canvas" min="16" max="4096"
                   style="width:80px" placeholder="不统一" @input="preset = 'custom'" /></div>
        </div>
        <div class="ops-row">
          <label class="chk" :class="{ off: !outlinePreset }"
                 title="描边在压进画布之后执行，宽度就是成品的实际像素宽">
            <input type="checkbox" v-model="outlineOn" :disabled="!outlinePreset" />
            应用描边预设</label>
          <span v-if="outlinePreset" class="hint">
            {{ outlinePreset.width }}px ·
            RGB({{ (outlinePreset.color || []).join(',') }}) ·
            {{ outlinePreset.position }}
            <template v-if="!outlinePreset.enabled">（预设当前是关闭的，这里可临时启用）</template>
          </span>
          <span v-else class="hint">该精灵还没有描边预设——到「图像处理 → 描边」存一个</span>
        </div>
        <div class="ops-row">
          <div class="field inline"><label>默认帧率</label>
            <input type="number" v-model.number="fps" min="1" max="60" style="width:60px"
                   title="动作已记录抽帧帧率时优先用记录值" /></div>
          <div class="field inline"><label>帧名格式</label>
            <input v-model="pattern" style="width:150px" /></div>
          <div class="field inline"><label>起始帧号</label>
            <input type="number" v-model.number="startIndex" min="0" style="width:60px" /></div>
          <div class="field inline"><label>循环衔接</label>
            <input type="checkbox" v-model="loop" /></div>
          <div class="field inline"><label>打包图集</label>
            <input type="checkbox" v-model="atlas" /></div>
        </div>

        <div class="ops-row">
          <b style="font-size:13px">动作 {{ selected.length }}/{{ items.length }} · {{ selectedFrames }} 帧</b>
          <button class="small" @click="selectAll">全选</button>
          <button class="small" @click="selectNone">全不选</button>
          <span class="spacer" style="flex:1"></span>
          <label class="chk" title="记到动作上：下次导出与一键流水线自动导出都用这个名字">
            <input type="checkbox" v-model="remember" />
            记住改名<span v-if="renamed.length" class="dim">（改了 {{ renamed.length }} 个）</span>
          </label>
        </div>

        <p v-if="dupSelected.length" class="warn-box">
          ⚠ 动画名重复：{{ dupSelected.join('、') }}——同名会互相覆盖，在下表里改掉其中一个。
        </p>
        <p v-else-if="conflicts.length" class="warn-box">
          ⚠ 未勾选的动作里有重名：{{ conflicts.join('、') }}——一起导时要先改掉。
        </p>
        <p v-if="unprocessed.length" class="warn-box">
          ⚠ {{ unprocessed.length }} 个动作还有未抠图的帧（{{ unprocessed.map(i => i.name).join('、') }}），
          导出后在 Spine 里会带背景。
        </p>

        <datalist id="spine-anim-options">
          <option v-for="n in (activeTemplate?.animation_names || [])" :key="n" :value="n" />
        </datalist>

        <div class="tbl-scroll">
          <table class="tpl-tbl">
            <thead><tr>
              <th style="width:34px"></th><th>动作</th>
              <th style="width:210px">Spine 动画名（可改）</th>
              <th style="width:110px">帧 / 已抠图</th>
            </tr></thead>
            <tbody>
              <tr v-for="i in items" :key="i.action_id" :class="{ dimrow: !i.frames }">
                <td><input type="checkbox" v-model="checked[i.action_id]" :disabled="!i.frames" /></td>
                <td>{{ i.name }}</td>
                <td class="anim-cell">
                  <input class="anim-in" v-model="names[i.action_id]"
                         list="spine-anim-options" spellcheck="false"
                         :class="{ dup: dupSelected.includes(animOf(i)),
                                   off: offTemplate(i) }"
                         :placeholder="i.suggest"
                         :title="offTemplate(i)
                           ? '模板里没有这个动画名，会当成新动画导出'
                           : '导出后在 Spine 里显示的动画名'" />
                  <span v-if="i.custom && animOf(i) === i.custom" class="tag"
                        title="这个名字已记在动作上，流水线自动导出也用它">记</span>
                  <button v-if="animOf(i) !== defaultOf(i)" class="mini"
                          :title="`恢复默认：${defaultOf(i)}`"
                          @click="resetName(i)">↺</button>
                </td>
                <td>
                  <span v-if="!i.frames" class="dim">无帧</span>
                  <span v-else :class="i.processed === i.frames ? 'ok-text' : 'dim'">
                    {{ i.frames }} / {{ i.processed }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-if="result" class="result-box">
          ✓ 导出完成：{{ result.animations.length }} 个动画 / {{ result.frames }} 帧
          <template v-if="result.template">· 模板 {{ result.template }}</template>
          <template v-if="result.spine_version">· Spine {{ result.spine_version }}</template>
          <template v-if="result.canvas">· 画布 {{ result.canvas }}</template>
          <template v-if="result.outline">· 描边 {{ result.outline.width }}px</template>
          <template v-if="result.atlas">· 图集 {{ result.atlas.size }}（{{ result.atlas.regions }} 区域）</template>
          <div class="dim" style="margin-top:4px">{{ result.dir }}</div>
          <div v-if="result.skipped?.length" class="warn-text" style="margin-top:4px">
            跳过 {{ result.skipped.length }} 个：{{ result.skipped.map(s => s.name || s.action_id).join('、') }}
          </div>
        </div>

        <div class="done-box">
          <div class="done-head">
            <b>已导出的产物</b>
            <span class="dim">{{ exports.length }} 份 · 手动导的与流水线收口导的都在这里</span>
            <span class="spacer" style="flex:1"></span>
            <button class="small" @click="loadExports">刷新</button>
          </div>
          <div v-if="!exports.length" class="dim" style="padding:6px 0">
            还没有导出过。导完会出现在这里，随时可以再下载。
          </div>
          <div v-for="e in exports" :key="e.name" class="done-row">
            <b class="ename">{{ e.name }}</b>
            <span class="dim">{{ fmtTime(e.updated_at) }}</span>
            <span v-if="e.error" class="warn-text">{{ e.error }}</span>
            <span v-else class="dim">
              {{ e.animations }} 个动画 · {{ e.frames }} 帧 · {{ e.images }} 张图
              <template v-if="e.spine_version"> · Spine {{ e.spine_version }}</template>
              <template v-if="e.canvas"> · 画布 {{ e.canvas }}</template>
              <template v-if="!e.has_atlas"> · 无图集</template>
            </span>
            <span class="spacer" style="flex:1"></span>
            <span class="dim">{{ fmtSize(e.bytes) }}</span>
            <button class="small" @click="downloadExport(e.name)">下载 zip</button>
            <button class="small danger" title="删除产物（不影响帧数据）"
                    @click="removeExport(e)">删除</button>
          </div>
        </div>

        <div class="foot">
          <span class="spacer"></span>
          <button v-if="result" class="small" @click="download">下载 zip</button>
          <button class="primary" :disabled="running || !selected.length" @click="run">
            {{ running ? '导出中…' : '开始导出' }}</button>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.modal-mask {
  position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 90;
  display: flex; align-items: center; justify-content: center;
}
.modal {
  width: 720px; max-width: 94vw; max-height: 88vh; display: flex; flex-direction: column;
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 20px 18px;
}
.modal-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.modal-head h3 { margin: 0; font-size: 16px; }
.spacer { flex: 1; }
.ops-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
.tbl-scroll { flex: 1; min-height: 90px; overflow-y: auto; border: 1px solid var(--border); border-radius: 5px; }
.tpl-tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tpl-tbl th {
  text-align: left; font-weight: 500; color: var(--text-dim); font-size: 12px;
  padding: 7px 10px; border-bottom: 1px solid var(--border); background: var(--bg-input);
  position: sticky; top: 0;
}
.tpl-tbl td { padding: 5px 10px; border-bottom: 1px solid var(--border); }
.tpl-tbl tbody tr:last-child td { border-bottom: none; }
.dimrow { opacity: .5; }
.mono-cell { font-family: Consolas, monospace; font-size: 12px; }
.mono-cell.dup { color: var(--err); }
.anim-cell { display: flex; align-items: center; gap: 4px; }
.anim-in {
  flex: 1; min-width: 0; font-family: Consolas, monospace; font-size: 12px;
  padding: 2px 6px;
}
.anim-in.dup { border-color: var(--err); color: var(--err); }
.anim-in.off { border-color: var(--warn); }
.mini {
  padding: 1px 5px; font-size: 12px; line-height: 1.3;
  background: none; border: 1px solid var(--border); border-radius: 3px;
  color: var(--text-dim); cursor: pointer;
}
.mini:hover { color: var(--text); }
.tag {
  font-size: 11px; color: var(--ok); border: 1px solid var(--ok);
  border-radius: 3px; padding: 0 3px; opacity: .8;
}
.dim { color: var(--text-dim); font-size: 12px; }
.ok-text { color: var(--ok); font-size: 12px; }
.warn-text { color: var(--warn); font-size: 12px; }
.tpl-row { border-bottom: 1px solid var(--border); padding-bottom: 8px; }
.tpl-info {
  margin: 0 0 8px; padding: 6px 10px; font-size: 12px;
  background: var(--bg-input); border-left: 2px solid var(--ok); border-radius: 3px;
}
.dimmed { opacity: .45; }
.warn-box {
  margin: 0 0 8px; padding: 5px 9px; font-size: 12px; color: #ffcc80;
  background: #ff980022; border-left: 2px solid var(--warn); border-radius: 3px;
}
.result-box {
  margin-top: 10px; padding: 8px 10px; font-size: 13px; color: var(--ok);
  background: var(--bg-input); border-radius: 4px;
}
.done-box {
  margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--border);
  max-height: 168px; overflow-y: auto;
}
.done-head { display: flex; align-items: center; gap: 8px; font-size: 13px; margin-bottom: 4px; }
.done-row {
  display: flex; align-items: center; gap: 8px; font-size: 12px;
  padding: 4px 0; border-bottom: 1px solid var(--border);
}
.done-row:last-child { border-bottom: none; }
.ename {
  font-family: Consolas, monospace; max-width: 180px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.danger { border-color: var(--err); color: var(--err); }
.foot { display: flex; gap: 8px; align-items: center; margin-top: 12px; }
</style>
