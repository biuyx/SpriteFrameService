<script setup>
import { ref, computed, onMounted } from 'vue'
import { useStore, refreshFrames, toast } from '../stores'
import { startJob } from '../jobs'
import api from '../api'

const store = useStore()
const mode = ref('ai')

// AI 参数：默认取精灵的工艺预设（全角色统一），可在此覆盖；
// 预设指定的模型本机未安装时（如 BRIA 未分发）回退到本机默认模型
const defaultBgModel = store.capabilities?.default_background_model || 'isnet-anime'
const spritePreset = ref({ model: defaultBgModel, alpha_threshold: 128, erode: 1, feather: 0,
                           ...(store.currentSprite?.preset?.matting || {}) })
const installedModels = new Set((store.capabilities?.background_models || [])
  .filter((m) => m.installed).map((m) => m.name))
const aiModel = ref(installedModels.has(spritePreset.value.model)
  ? spritePreset.value.model : defaultBgModel)
const alphaThreshold = ref(spritePreset.value.alpha_threshold)
const erode = ref(spritePreset.value.erode)
const feather = ref(spritePreset.value.feather)

// 当前参数是否偏离精灵预设（偏离时显式标出，防止悄悄不一致）
const presetDirty = computed(() =>
  aiModel.value !== spritePreset.value.model ||
  alphaThreshold.value !== spritePreset.value.alpha_threshold ||
  erode.value !== spritePreset.value.erode ||
  feather.value !== spritePreset.value.feather
)

async function saveAsSpritePreset() {
  if (!store.currentSprite) return
  await api.patchSprite(store.currentSprite.id, { preset: { matting: aiParams() } })
  spritePreset.value = { ...aiParams() }
  store.currentSprite.preset = { ...store.currentSprite.preset, matting: aiParams() }
  toast(`已保存为「${store.currentSprite.name}」的工艺预设，该精灵所有动作默认使用`)
}

function resetToPreset() {
  aiModel.value = spritePreset.value.model
  alphaThreshold.value = spritePreset.value.alpha_threshold
  erode.value = spritePreset.value.erode
  feather.value = spritePreset.value.feather
}

// 颜色参数
const colorPreset = ref('绿幕')
const colorLower = ref('35,50,50')
const colorUpper = ref('85,255,255')
const colorInvert = ref(false)
const colorDenoise = ref(1)
const colorFeather = ref(0)

// 描边
const outlineThickness = ref(3)
const outlineColor = ref('#000000')

// 测试
const testIndex = ref(0)
const testImg = ref(null)
const testBusy = ref(false)
const testInfo = ref('')

const bgModels = computed(() => store.capabilities?.background_models || [])
const presets = computed(() => store.capabilities?.color_presets || {})

const selected = computed(() =>
  store.frames.filter((f) => f.is_selected).map((f) => f.index)
)

function parseTriple(s, fallback) {
  const parts = String(s).split(',').map((x) => parseInt(x.trim(), 10))
  if (parts.length !== 3 || parts.some((x) => isNaN(x))) return fallback
  return parts
}

function aiParams() {
  return {
    model: aiModel.value,
    alpha_threshold: alphaThreshold.value,
    erode: erode.value,
    feather: feather.value,
  }
}

function colorParams() {
  return {
    lower: parseTriple(colorLower.value, [35, 50, 50]),
    upper: parseTriple(colorUpper.value, [85, 255, 255]),
    invert: colorInvert.value,
    denoise: colorDenoise.value,
    color_feather: colorFeather.value,
  }
}

function usePreset(name) {
  const p = presets.value[name]
  if (!p) return
  colorLower.value = p.lower.join(',')
  colorUpper.value = p.upper.join(',')
  colorInvert.value = p.invert
}

async function runTest() {
  testBusy.value = true
  testInfo.value = ''
  testImg.value = null
  try {
    const blob = await api.bgTest(store.sessionId, {
      frame_index: testIndex.value,
      mode: mode.value,
      params: mode.value === 'ai' ? aiParams() : colorParams(),
    })
    testImg.value = URL.createObjectURL(blob)
    testInfo.value = `帧 #${testIndex.value} 处理结果`
  } catch (e) {
    toast(`测试失败: ${e.message}`)
  } finally {
    testBusy.value = false
  }
}

async function batchRemove() {
  const params = { mode: mode.value }
  if (selected.value.length) params.indices = selected.value
  params.params = mode.value === 'ai' ? aiParams() : colorParams()
  await startJob(() => api.bgRemove(store.sessionId, params), {
    title: mode.value === 'ai' ? `AI 抠图 (${aiModel.value})` : '颜色抠图',
    onDone: async (r) => {
      await refreshFrames()
      toast(`抠图完成：${r.processed}/${r.total} 帧`)
    },
  })
}

async function runOutline() {
  const params = { thickness: outlineThickness.value }
  if (selected.value.length) params.indices = selected.value
  const c = outlineColor.value.replace('#', '')
  params.color = [parseInt(c.slice(0, 2), 16), parseInt(c.slice(2, 4), 16), parseInt(c.slice(4, 6), 16)]
  await startJob(() => api.outline(store.sessionId, params), {
    title: '添加描边',
    onDone: async (r) => {
      await refreshFrames()
      toast(`描边完成：${r.processed}/${r.total} 帧`)
    },
  })
}

onMounted(() => {
  if (bgModels.value.length && !bgModels.value.some((m) => m.installed)) {
    toast('提示：未找到 AI 抠图模型，请在 models/ 目录放置 .onnx 模型')
  }
})
</script>

<template>
  <div class="grid2">
    <div>
      <div class="panel">
        <div class="section-title"><h2>背景去除</h2></div>
        <div class="row">
          <div class="field inline"><label>模式</label>
            <select v-model="mode">
              <option value="ai">AI 智能抠图</option>
              <option value="color">颜色过滤（绿/蓝幕）</option>
            </select>
          </div>
        </div>

        <template v-if="mode === 'ai'">
          <div class="row">
            <div class="field inline"><label>模型</label>
              <select v-model="aiModel">
                <option v-for="m in bgModels" :key="m.name" :value="m.name" :disabled="!m.installed">
                  {{ m.display_name }} {{ m.installed ? '' : '(未安装)' }}
                </option>
              </select>
            </div>
          </div>
          <div class="row">
            <div class="field inline"><label>Alpha阈值</label><input type="number" v-model.number="alphaThreshold" :min="0" :max="255" /></div>
            <div class="field inline"><label>腐蚀(负=膨胀)</label><input type="number" v-model.number="erode" :min="-10" :max="10" /></div>
            <div class="field inline"><label>羽化</label><input type="number" v-model.number="feather" :min="0" :max="20" /></div>
          </div>
          <div class="row preset-row">
            <template v-if="presetDirty">
              <span class="preset-warn">⚠ 已偏离「{{ store.currentSprite?.name }}」的工艺预设——同一精灵的动作建议用统一参数</span>
              <button class="small" @click="resetToPreset">还原预设</button>
              <button class="small" @click="saveAsSpritePreset">保存为精灵预设</button>
            </template>
            <span v-else class="hint">✓ 使用「{{ store.currentSprite?.name }}」的工艺预设（全部动作统一）</span>
          </div>
          <p v-if="alphaThreshold > 0 && alphaThreshold < 51" class="param-hint">
            Alpha 阈值 1~50 会把半透明白边提升为完全不透明，白边反而更重；建议 0 或 ≥128
          </p>
          <p v-if="feather > 0" class="param-hint">
            羽化会加宽边缘过渡带，做精灵图通常不需要
          </p>
        </template>

        <template v-else>
          <div class="row">
            <div class="field inline"><label>预设</label>
              <select v-model="colorPreset" @change="usePreset(colorPreset)">
                <option v-for="(_, name) in presets" :key="name" :value="name">{{ name }}</option>
              </select>
            </div>
            <div class="field inline"><label>HSV下限</label><input v-model="colorLower" style="width:110px" /></div>
            <div class="field inline"><label>HSV上限</label><input v-model="colorUpper" style="width:110px" /></div>
          </div>
          <div class="row">
            <div class="field inline"><label>反选</label><input type="checkbox" v-model="colorInvert" /></div>
            <div class="field inline"><label>去噪</label><input type="number" v-model.number="colorDenoise" :min="0" :max="10" /></div>
            <div class="field inline"><label>羽化</label><input type="number" v-model.number="colorFeather" :min="0" :max="20" /></div>
          </div>
        </template>

        <div class="row">
          <button class="primary" @click="batchRemove">批量抠图（{{ selected.length || '全部' }} 帧）</button>
        </div>
        <p class="hint">抠图结果写入「处理后」图层，可在帧管理查看绿色徽标。</p>
      </div>

      <div class="panel">
        <h3>描边</h3>
        <p class="desc">为已抠图（RGBA）帧添加描边。</p>
        <div class="row">
          <div class="field inline"><label>厚度</label><input type="number" v-model.number="outlineThickness" :min="1" :max="20" /></div>
          <div class="field inline"><label>颜色</label><input type="color" v-model="outlineColor" /></div>
          <button @click="runOutline">描边选中帧</button>
        </div>
      </div>
    </div>

    <div>
      <div class="panel">
        <div class="section-title"><h2>参数测试</h2></div>
        <p class="desc">对单帧应用当前参数预览效果，用于调参后再批量处理。</p>
        <div class="row">
          <div class="field inline"><label>帧索引</label>
            <input type="number" v-model.number="testIndex" :min="0" :max="store.frameCount - 1" />
          </div>
          <button :disabled="testBusy" @click="runTest">{{ testBusy ? '处理中...' : '测试' }}</button>
        </div>
        <div class="preview-box">
          <img v-if="testImg" :src="testImg" />
          <span v-else class="hint">点击「测试」预览效果</span>
        </div>
        <p v-if="testInfo" class="hint" style="margin-top:6px">{{ testInfo }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.preset-row { align-items: center; gap: 8px; }
.preset-warn { font-size: 12px; color: var(--warn); }
.param-hint {
  margin: 4px 0 0; padding: 5px 9px; font-size: 12px; color: #ffcc80;
  background: #ff980022; border-left: 2px solid var(--warn); border-radius: 3px;
}
</style>
