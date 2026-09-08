<script setup>
import { ref, computed } from 'vue'
import { useStore, refreshFrames, toast, askConfirm } from '../stores'
import { startJob } from '../jobs'
import api from '../api'
import SaveRuleButton from '../components/SaveRuleButton.vue'

const store = useStore()
const mode = ref('pose')
const threshold = ref(0.9)
const weights = ref({ u: 0.2, m: 0.3, l: 0.5 })
const overlayIndex = ref(0)
const overlayMode = ref('pose')
const detectResult = ref(null)

// 找循环帧：先出结果，确认后再应用范围
const loopResult = ref(null)
const loopPending = ref(false)

// 间隔选帧：从起始帧起每 N 帧选中 1 帧（抽稀用）
const stepStart = ref(0)
const stepN = ref(2)

function stepIndices() {
  const n = Math.max(1, Math.round(stepN.value))
  const start = Math.min(Math.max(0, stepStart.value), Math.max(0, store.frameCount - 1))
  const indices = []
  for (let i = start; i < store.frameCount; i += n) indices.push(i)
  return indices
}

async function selectByStep() {
  if (!store.frameCount) return toast('当前没有帧')
  const indices = stepIndices()
  await api.selection(store.sessionId, { mode: 'set', indices })
  await refreshFrames()
  toast(`已选中 ${indices.length} 帧（从 #${Math.max(0, stepStart.value)} 起，每 ${Math.max(1, Math.round(stepN.value))} 帧取 1）`)
}

// 抽稀：只保留间隔选中的帧，其余删除（一步完成 选取→反选→删除）
async function keepByStep() {
  if (!store.frameCount) return toast('当前没有帧')
  const keep = new Set(stepIndices())
  const drop = store.frames.map((f) => f.index).filter((i) => !keep.has(i))
  if (!drop.length) return toast('按当前间隔没有可删除的帧')
  if (!(await askConfirm(
    `抽稀：保留 ${keep.size} 帧（每 ${Math.max(1, Math.round(stepN.value))} 帧取 1），删除其余 ${drop.length} 帧？`,
    { danger: true }))) return
  await api.deleteFrames(store.sessionId, drop)
  await refreshFrames()
  toast(`已抽稀：保留 ${store.frameCount} 帧`)
}

const modes = [
  { key: 'pose', name: '姿势 (MediaPipe)' },
  { key: 'pose_rtm', name: '姿势 (RTMPose，需 rtmlib)' },
  { key: 'contour', name: '轮廓匹配' },
  { key: 'image', name: '图像特征' },
  { key: 'regional', name: '分区域 SSIM' },
]

const selected = computed(() =>
  store.frames.filter((f) => f.is_selected).map((f) => f.index)
)

async function detect() {
  const params = { mode: mode.value }
  if (selected.value.length) params.indices = selected.value
  if (mode.value === 'regional') params.weights = [weights.value.u, weights.value.m, weights.value.l]
  await startJob(() => api.detect(store.sessionId, params), {
    title: `分析 (${mode.value})`,
    onDone: async (r) => {
      detectResult.value = r
      await refreshFrames()
      toast(`${r.processed}/${r.total} 帧完成分析`)
    },
  })
}

async function removeSimilar() {
  const params = { mode: mode.value, threshold: threshold.value }
  if (selected.value.length) params.indices = selected.value
  await startJob(() => api.removeSimilar(store.sessionId, params), {
    title: '去相似帧',
    onDone: async (r) => {
      await refreshFrames()
      if (r.groups && r.groups.length) {
        toast(`去相似完成：保留 ${r.kept} 帧，取消选中 ${r.removed} 帧`)
      } else {
        toast(r.message || '去相似完成')
      }
    },
  })
}

// 找循环帧：先计算（不改变选中），弹出确认后再应用
async function findLoop() {
  loopPending.value = true
  loopResult.value = null
  const params = { mode: mode.value, apply_range: false }
  if (selected.value.length) params.indices = selected.value
  await startJob(() => api.findLoop(store.sessionId, params), {
    title: '找循环帧',
    onDone: (r) => {
      loopResult.value = r
      loopPending.value = false
    },
  })
}

async function applyLoopRange() {
  const r = loopResult.value
  if (!r || !r.suggested_range) return
  const [start, end] = r.suggested_range
  // 选中建议范围 [start..end]，取消范围外
  const indices = []
  for (const f of store.frames) {
    if (f.index >= start && f.index <= end) indices.push(f.index)
  }
  await api.selection(store.sessionId, { mode: 'set', indices })
  await refreshFrames()
  toast(`已应用循环范围 #${start} ~ #${end}`)
}

function cancelLoopRange() {
  loopResult.value = null
}

function overlayUrl(idx, m) {
  return api.overlay(store.sessionId, idx, m, 0)
}
</script>

<template>
  <div class="grid2">
    <div>
      <div class="panel">
        <div class="section-title"><h2>动作分析</h2></div>
        <div class="row">
          <div class="field inline"><label>检测模式</label>
            <select v-model="mode">
              <option v-for="m in modes" :key="m.key" :value="m.key">{{ m.name }}</option>
            </select>
          </div>
          <button class="primary" @click="detect">分析选中帧</button>
        </div>

        <template v-if="mode === 'regional'">
          <div class="row">
            <div class="field inline"><label>上部权重</label><input type="number" v-model.number="weights.u" step="0.1" :min="0" :max="1" /></div>
            <div class="field inline"><label>中部权重</label><input type="number" v-model.number="weights.m" step="0.1" :min="0" :max="1" /></div>
            <div class="field inline"><label>下部权重</label><input type="number" v-model.number="weights.l" step="0.1" :min="0" :max="1" /></div>
          </div>
        </template>

        <p v-if="detectResult" class="hint">最近一次分析：{{ detectResult.processed }}/{{ detectResult.total }} 帧成功</p>
      </div>

      <div class="panel">
        <h3>去相似帧 / 找循环帧</h3>
        <p class="desc">基于当前 {{ mode }} 模式的分析数据。去相似：每组相似帧只保留第一帧（取消其余选中）。找循环：从选中帧中找与首帧最相似的循环点，确认后再应用范围。</p>
        <div class="row">
          <div class="field inline"><label>相似度阈值 %</label>
            <input type="number" v-model.number="threshold" step="0.01" :min="0.5" :max="0.99" />
          </div>
          <button @click="removeSimilar">去相似帧</button>
          <button :disabled="loopPending" @click="findLoop">{{ loopPending ? '查找中...' : '找循环帧' }}</button>
          <span class="spacer" style="flex:1"></span>
          <SaveRuleButton />
        </div>
        <div class="row" style="align-items:center">
          <div class="field inline"><label>起始帧</label>
            <input type="number" v-model.number="stepStart" :min="0" style="width:70px" /></div>
          <div class="field inline"><label>间隔</label>
            <input type="number" v-model.number="stepN" :min="1" style="width:70px" /></div>
          <button class="small" @click="selectByStep">间隔选帧</button>
          <button class="small danger" @click="keepByStep">按间隔抽稀</button>
          <span class="hint">每 N 帧取 1（如间隔 2 → #0、#2、#4…）；「抽稀」直接只保留这些帧</span>
        </div>
        <p class="hint" style="margin:4px 0 0">
          删帧完成后可「保存为模板规则」——同模板生成的其他角色批量抽帧时自动沿用同样的保留结果。</p>

        <!-- 查找结果 + 确认 -->
        <div v-if="loopResult" class="mono" style="margin-top:8px; padding:10px; background:var(--bg-input); border-radius:4px">
          <div v-if="loopResult.message">{{ loopResult.message }}</div>
          <template v-else>
            <div>首帧: #{{ loopResult.first_index }}  循环点: #{{ loopResult.loop_index }} (相似度 {{ (loopResult.similarity * 100).toFixed(1) }}%)</div>
            <div v-if="loopResult.suggested_range">建议范围: #{{ loopResult.suggested_range[0] }} ~ #{{ loopResult.suggested_range[1] }}</div>
            <div class="row" style="margin-top:8px">
              <button class="primary small" @click="applyLoopRange">✓ 应用此范围</button>
              <button class="small" @click="cancelLoopRange">取消</button>
            </div>
          </template>
        </div>
      </div>
    </div>

    <div>
      <div class="panel">
        <div class="section-title"><h2>姿势骨架叠加</h2></div>
        <div class="row">
          <div class="field inline"><label>帧索引</label>
            <input type="number" v-model.number="overlayIndex" :min="0" :max="store.frameCount - 1" />
          </div>
          <div class="field inline"><label>叠加</label>
            <select v-model="overlayMode">
              <option value="pose">姿势骨架</option>
              <option value="contour">轮廓线</option>
            </select>
          </div>
        </div>
        <div class="preview-box">
          <img v-if="store.frameCount" :src="overlayUrl(overlayIndex, overlayMode)" />
          <span v-else class="hint">暂无帧</span>
        </div>
      </div>
    </div>
  </div>
</template>
