<script setup>
import { ref, computed, onMounted } from 'vue'
import { useStore, toast } from '../stores'
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

onMounted(async () => {
  name.value = store.currentSprite?.name || ''
  outlineOn.value = !!outlinePreset.value?.enabled
  try {
    const r = await api.spinePreview(store.currentSprite.id)
    items.value = r.items
    conflicts.value = r.conflicts || []
    for (const it of items.value) checked.value[it.action_id] = it.frames > 0
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
  for (const i of selected.value) seen[i.anim] = (seen[i.anim] || 0) + 1
  return Object.keys(seen).filter(k => seen[k] > 1)
})

function selectAll() { for (const i of items.value) checked.value[i.action_id] = i.frames > 0 }
function selectNone() { for (const i of items.value) checked.value[i.action_id] = false }

async function run() {
  if (!selected.value.length) return toast('请至少勾选一个动作')
  if (dupSelected.value.length)
    return toast(`动画名重复：${dupSelected.value.join('、')}——请在参考视频库里改 Spine 动画名`)
  running.value = true
  result.value = null
  const payload = {
    name: name.value.trim() || undefined,
    action_ids: selected.value.map(i => i.action_id),
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
  try {
    await startJob(() => api.spineExport(store.currentSprite.id, payload), {
      title: 'Spine 导出',
      onDone: (r) => { result.value = r; running.value = false },
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
      </p>

      <div v-if="loading" class="hint" style="padding:26px;text-align:center">加载动作中...</div>

      <template v-else>
        <div class="ops-row">
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
        </div>

        <p v-if="conflicts.length" class="warn-box">
          ⚠ 动画名重复：{{ conflicts.join('、') }}——同名会互相覆盖，请到「参考视频库」给对应模板改 Spine 动画名。
        </p>
        <p v-if="unprocessed.length" class="warn-box">
          ⚠ {{ unprocessed.length }} 个动作还有未抠图的帧（{{ unprocessed.map(i => i.name).join('、') }}），
          导出后在 Spine 里会带背景。
        </p>

        <div class="tbl-scroll">
          <table class="tpl-tbl">
            <thead><tr>
              <th style="width:34px"></th><th>动作</th>
              <th style="width:120px">Spine 动画名</th>
              <th style="width:110px">帧 / 已抠图</th>
            </tr></thead>
            <tbody>
              <tr v-for="i in items" :key="i.action_id" :class="{ dimrow: !i.frames }">
                <td><input type="checkbox" v-model="checked[i.action_id]" :disabled="!i.frames" /></td>
                <td>{{ i.name }}</td>
                <td class="mono-cell" :class="{ dup: i.duplicate }">{{ i.anim }}</td>
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
          <template v-if="result.canvas">· 画布 {{ result.canvas }}</template>
          <template v-if="result.outline">· 描边 {{ result.outline.width }}px</template>
          <template v-if="result.atlas">· 图集 {{ result.atlas.size }}（{{ result.atlas.regions }} 区域）</template>
          <div class="dim" style="margin-top:4px">{{ result.dir }}</div>
          <div v-if="result.skipped?.length" class="warn-text" style="margin-top:4px">
            跳过 {{ result.skipped.length }} 个：{{ result.skipped.map(s => s.name || s.action_id).join('、') }}
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
.dim { color: var(--text-dim); font-size: 12px; }
.ok-text { color: var(--ok); font-size: 12px; }
.warn-text { color: var(--warn); font-size: 12px; }
.warn-box {
  margin: 0 0 8px; padding: 5px 9px; font-size: 12px; color: #ffcc80;
  background: #ff980022; border-left: 2px solid var(--warn); border-radius: 3px;
}
.result-box {
  margin-top: 10px; padding: 8px 10px; font-size: 13px; color: var(--ok);
  background: var(--bg-input); border-radius: 4px;
}
.foot { display: flex; gap: 8px; align-items: center; margin-top: 12px; }
</style>
