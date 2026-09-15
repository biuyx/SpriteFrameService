<script setup>
import { ref, computed } from 'vue'
import { useStore, refreshFrames, toast } from '../stores'
import { startJob } from '../jobs'
import api from '../api'

const store = useStore()

const scaleMode = ref('percent')
const percent = ref(50)
const width = ref(512)
const height = ref(512)
const algorithm = ref('lanczos')

const cropMargins = ref({ top: 0, bottom: 0, left: 0, right: 0 })
const edgeErode = ref(1)

const esrganModel = ref('realesrgan-x4plus')
const esrganTile = ref(0)

// ---- 描边（纯色，抠图之后执行；与 PS 图层样式 Stroke 语义一致）----
const OUTLINE_STYLES = {
  smooth: { corner: 'round', antialias: true, label: '平滑（圆角，抗锯齿）' },
  hard: { corner: 'round', antialias: false, label: '硬边（圆角，像素风）' },
  square: { corner: 'miter', antialias: false, label: '方角硬边（像素风）' },
}
const outline = ref({ width: 2, color: '#000000', opacity: 1, position: 'outer',
                      style: 'smooth', auto_pad: true })
const outlineImg = ref(null)
const outlineBusy = ref(false)
const outlineInfo = ref('')
const outlineFrame = ref(0)
// 已抠图的帧才有 alpha，描边才有意义
const processedCount = computed(() => store.frames.filter((f) => f.has_processed).length)

function outlineParams() {
  const st = OUTLINE_STYLES[outline.value.style] || OUTLINE_STYLES.smooth
  const c = outline.value.color.replace('#', '')
  return {
    width: outline.value.width,
    color: [parseInt(c.slice(0, 2), 16), parseInt(c.slice(2, 4), 16), parseInt(c.slice(4, 6), 16)],
    opacity: outline.value.opacity,
    position: outline.value.position,
    corner: st.corner,
    antialias: st.antialias,
  }
}

async function previewOutline() {
  outlineBusy.value = true
  outlineInfo.value = ''
  try {
    const blob = await api.imageOutlineTest(store.sessionId, {
      frame_index: outlineFrame.value, params: outlineParams(),
    })
    outlineImg.value = URL.createObjectURL(blob)
    outlineInfo.value = `帧 #${outlineFrame.value} 描边预览（未落盘）`
  } catch (e) {
    toast(`预览失败: ${e.message}`)
  } finally {
    outlineBusy.value = false
  }
}

async function runOutline() {
  if (!processedCount.value) return toast('描边要求 RGBA——请先在「背景抠图」完成抠图')
  const params = { params: outlineParams(), auto_pad: outline.value.auto_pad }
  if (selected.value.length) params.indices = selected.value
  await startJob(() => api.imageOutline(store.sessionId, params), {
    title: `描边 ${outline.value.width}px`,
    onDone: async (r) => {
      await refreshFrames()
      if (r?.error) return toast(r.error)
      toast(`描边完成：${r.processed}/${r.total} 帧`
            + (r.pad ? `（已扩边 ${r.pad}px 避免裁切）` : '')
            + (r.skipped ? `，跳过未抠图 ${r.skipped} 帧` : ''))
    },
  })
}

const selected = computed(() =>
  store.frames.filter((f) => f.is_selected).map((f) => f.index)
)

const esrganModels = computed(() => store.capabilities?.realesrgan || [])
const esrganAvailable = computed(() =>
  store.capabilities?.realesrgan_info?.available
)

async function runScale() {
  const params = { mode: scaleMode.value, algorithm: algorithm.value }
  if (selected.value.length) params.indices = selected.value
  if (scaleMode.value === 'percent') params.percent = percent.value
  else { params.width = width.value; params.height = height.value }
  await startJob(() => api.scale(store.sessionId, params), {
    title: '批量缩放',
    onDone: async (r) => {
      await refreshFrames()
      toast(`缩放完成：${r.from} → ${r.to}`)
    },
  })
}

async function runCrop() {
  const params = { ...cropMargins.value }
  if (selected.value.length) params.indices = selected.value
  await startJob(() => api.crop(store.sessionId, params), {
    title: '空白裁剪',
    onDone: async (r) => {
      await refreshFrames()
      toast(r.processed ? `裁剪完成：${r.size}` : r.message)
    },
  })
}

async function runEdges() {
  const params = { erode: edgeErode.value }
  if (selected.value.length) params.indices = selected.value
  await startJob(() => api.optimizeEdges(store.sessionId, params), {
    title: '边缘优化',
    onDone: async (r) => {
      await refreshFrames()
      toast(`边缘优化完成：${r.processed} 帧`)
    },
  })
}

async function runEnhance() {
  const params = { model: esrganModel.value, tile: esrganTile.value }
  if (selected.value.length) params.indices = selected.value
  await startJob(() => api.enhance(store.sessionId, params), {
    title: '图像增强',
    onDone: async (r) => {
      await refreshFrames()
      toast(`增强完成：${r.processed} 帧`)
    },
  })
}
</script>

<template>
  <div class="grid2">
    <div>
      <div class="panel">
        <h3>描边</h3>
        <p class="desc">给已抠图的帧加纯色描边（抠图之后、导出之前执行；缩放后再描边宽度才准确）。<br>重复执行会在已有描边外再描一圈——要改参数请先在「历史回退」撤销上一次描边。</p>
        <div class="row">
          <div class="field inline"><label>宽度(px)</label>
            <input type="number" v-model.number="outline.width" :min="0.5" :max="16" step="0.5" style="width:70px" /></div>
          <div class="field inline"><label>颜色</label><input type="color" v-model="outline.color" /></div>
          <div class="field inline"><label>风格</label>
            <select v-model="outline.style">
              <option v-for="(v, k) in OUTLINE_STYLES" :key="k" :value="k">{{ v.label }}</option>
            </select></div>
        </div>
        <div class="row">
          <div class="field inline"><label>位置</label>
            <select v-model="outline.position">
              <option value="outer">外描边</option>
              <option value="inner">内描边</option>
              <option value="center">居中</option>
            </select></div>
          <div class="field inline"><label>不透明度</label>
            <input type="number" v-model.number="outline.opacity" :min="0.1" :max="1" step="0.1" style="width:70px" /></div>
          <div class="field inline"><label title="角色贴边时自动扩透明边，所有帧扩同一量以保持对齐">自动扩边</label>
            <input type="checkbox" v-model="outline.auto_pad" /></div>
        </div>
        <div class="row">
          <div class="field inline"><label>预览帧</label>
            <input type="number" v-model.number="outlineFrame" :min="0" :max="Math.max(0, store.frameCount - 1)" style="width:70px" /></div>
          <button class="small" :disabled="outlineBusy" @click="previewOutline">
            {{ outlineBusy ? '预览中…' : '预览当前帧' }}</button>
          <button class="primary" :disabled="!processedCount" @click="runOutline">
            描边（{{ selected.length || '全部' }} 帧）</button>
          <span v-if="!processedCount" class="hint warn">尚无已抠图的帧</span>
        </div>
        <div v-if="outlineImg" class="preview-box" style="margin-top:8px;max-height:260px">
          <img :src="outlineImg" style="max-height:240px" />
        </div>
        <p v-if="outlineInfo" class="hint" style="margin-top:4px">{{ outlineInfo }}</p>
      </div>

      <div class="panel">
        <h3>批量缩放</h3>
        <div class="row">
          <div class="field inline"><label>方式</label>
            <select v-model="scaleMode">
              <option value="percent">按比例</option>
              <option value="size">固定尺寸</option>
            </select>
          </div>
          <template v-if="scaleMode === 'percent'">
            <div class="field inline"><label>%</label><input type="number" v-model.number="percent" :min="1" :max="400" /></div>
          </template>
          <template v-else>
            <div class="field inline"><label>宽</label><input type="number" v-model.number="width" :min="1" /></div>
            <div class="field inline"><label>高</label><input type="number" v-model.number="height" :min="1" /></div>
          </template>
          <div class="field inline"><label>算法</label>
            <select v-model="algorithm">
              <option v-for="a in store.capabilities?.scale_algorithms || []" :key="a" :value="a">{{ a }}</option>
            </select>
          </div>
        </div>
        <button class="primary" @click="runScale">缩放选中帧</button>
      </div>

      <div class="panel">
        <h3>空白裁剪</h3>
        <p class="desc">计算所有选中帧的联合内容边界，统一裁剪多余空白（保留边距）。</p>
        <div class="row">
          <div class="field inline"><label>上</label><input type="number" v-model.number="cropMargins.top" :min="0" :max="100" /></div>
          <div class="field inline"><label>下</label><input type="number" v-model.number="cropMargins.bottom" :min="0" :max="100" /></div>
          <div class="field inline"><label>左</label><input type="number" v-model.number="cropMargins.left" :min="0" :max="100" /></div>
          <div class="field inline"><label>右</label><input type="number" v-model.number="cropMargins.right" :min="0" :max="100" /></div>
        </div>
        <button class="primary" @click="runCrop">裁剪选中帧</button>
      </div>

      <div class="panel">
        <h3>边缘优化</h3>
        <p class="desc">对已抠图帧的 alpha 通道做腐蚀收缩，去除毛刺。</p>
        <div class="row">
          <div class="field inline"><label>收缩像素</label><input type="number" v-model.number="edgeErode" :min="1" :max="20" /></div>
          <button class="primary" @click="runEdges">优化选中帧</button>
        </div>
      </div>
    </div>

    <div>
      <div class="panel">
        <h3>RealESRGAN 图像增强</h3>
        <p class="desc">使用 Real-ESRGAN 对帧做超分辨率修复。</p>
        <template v-if="esrganAvailable">
          <div class="row">
            <div class="field inline"><label>模型</label>
              <select v-model="esrganModel">
                <option v-for="m in esrganModels" :key="m.name" :value="m.name" :disabled="!m.installed">
                  {{ m.display_name }} {{ m.installed ? '' : '(未安装)' }}
                </option>
              </select>
            </div>
            <div class="field inline"><label>分块</label><input type="number" v-model.number="esrganTile" :min="0" :max="512" title="0=不分块" /></div>
          </div>
          <button class="primary" @click="runEnhance">增强选中帧</button>
        </template>
        <p v-else class="hint" style="color: var(--err)">
          Real-ESRGAN 不可用：需要在 models/realesrgan/ 放置可执行文件与模型（Linux 使用 realesrgan-ncnn-vulkan）。
        </p>
      </div>

      <div class="panel">
        <h3>说明</h3>
        <ul class="hint">
          <li>以上操作均对「选中帧」执行；未选定时对全部帧执行。</li>
          <li>结果写入「处理后」图层，可通过右上角「历史回退」撤销。</li>
          <li>建议顺序：抠图 → 边缘优化 → 裁剪 → 缩放 → 增强。</li>
        </ul>
      </div>
    </div>
  </div>
</template>
