<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useStore, toast } from '../stores'
import { startJob } from '../jobs'
import api from '../api'

const store = useStore()
const format = ref('sprite_sheet')
// 文件名只有两层：这个动作存过导出名就用它，没有就是动作名。
// 可以写 {action} {anim} {sprite} 占位，比如存 {anim} 就跟着 Spine 动画名走。
const DEFAULT_PATTERN = '{action}'
const outputName = ref('')
// 占位符各自展开成什么由后端给（{anim} 要按动作/模板推 Spine 动画名）
const nameParts = ref({ sprite: '', action: '', anim: '', alias: '' })
// 该动作存下来的导出别名：动作名是固定的，对外叫什么挂在动作上，随时可改
const alias = ref('')
// 当前这个名字是哪来的
const nameFrom = computed(() => {
  const v = outputName.value.trim()
  if (!v) return { key: 'default', text: '默认（动作名）' }
  if (alias.value && v === alias.value) return { key: 'action', text: '该动作的导出名' }
  return { key: 'dirty', text: '未保存' }
})
const resolvedName = computed(() => {
  const p = nameParts.value
  const out = (outputName.value.trim() || DEFAULT_PATTERN)
    .replace(/\{sprite\}/g, p.sprite || '').replace(/\{action\}/g, p.action || '')
    .replace(/\{anim\}/g, p.anim || '')
    .replace(/^[_\s]+|[_\s]+$/g, '')
  // 和后端同一套兜底：动作名取不到就退到角色名
  return (out || p.sprite || 'sprite').replace(/[^a-zA-Z0-9_\-一-鿿]/g, '_')
})
// 精灵预设里启用的尺寸/描边处理，导出时按此顺序非破坏性应用
const sizeOps = computed(() => {
  const p = store.currentSprite?.preset || {}
  const ops = []
  if (p.scale?.enabled) {
    ops.push('缩放 ' + (p.scale.mode === 'percent'
      ? `${p.scale.percent}%` : `${p.scale.width}x${p.scale.height}`))
  }
  if (p.outline?.enabled && p.outline.width > 0) {
    ops.push(`描边 ${p.outline.width}px`)
  }
  return ops
})

const sprite = reactive({
  layout: 'grid', columns: null, padding: 0,
  frame_width: null, frame_height: null,
  generate_json: true, resample_filter: 'lanczos',
  bg_color: '#00000000',
})
const gif = reactive({ fps: 10, loop: 0, optimize: true, frame_width: 256, frame_height: 256, resample_filter: 'lanczos' })
const webp = reactive({ quality: 80, frame_width: null, frame_height: null })
const godot = reactive({ animation_name: 'default', fps: 10, loop: true, export_individual_frames: true, frame_width: null, frame_height: null })
const pngquant = reactive({ enabled: false, quality_min: 60, quality_max: 80 })

const exports = ref([])
const selected = computed(() => store.frames.filter((f) => f.is_selected).map((f) => f.index))

const formats = [
  { key: 'sprite_sheet', name: 'PNG 精灵图 + JSON' },
  { key: 'gif', name: 'GIF 动画' },
  { key: 'frames', name: '单独帧 PNG' },
  { key: 'webp', name: 'WebP' },
  { key: 'godot', name: 'Godot SpriteFrames' },
]

function hexToRgba(hex) {
  const h = hex.replace('#', '')
  if (h.length === 8) {
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16), parseInt(h.slice(6, 8), 16)]
  }
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16), 255]
}

function buildConfig() {
  const common = {
    output_name: outputName.value.trim(),
    pngquant_config: { enabled: pngquant.enabled, quality_min: pngquant.quality_min, quality_max: pngquant.quality_max },
    loop_transition: {
      enabled: store.loopTransition.enabled,
      count: store.loopTransition.count,
      mode: store.loopTransition.mode,
    },
    // 尺寸与描边的唯一来源是精灵预设，导出时非破坏性应用（先缩放后描边）
    scale: store.currentSprite?.preset?.scale || { enabled: false },
    outline: store.currentSprite?.preset?.outline || { enabled: false },
  }
  if (format.value === 'sprite_sheet') {
    return {
      format: 'sprite_sheet',
      ...common,
      sprite_config: {
        layout: sprite.layout, columns: sprite.columns, padding: sprite.padding,
        frame_width: sprite.frame_width, frame_height: sprite.frame_height,
        generate_json: sprite.generate_json, resample_filter: sprite.resample_filter,
        background_color: hexToRgba(sprite.bg_color),
      },
    }
  }
  if (format.value === 'gif') {
    return {
      format: 'gif', ...common,
      gif_config: {
        fps: gif.fps, loop: gif.loop, optimize: gif.optimize,
        frame_width: gif.frame_width, frame_height: gif.frame_height,
        resample_filter: gif.resample_filter,
      },
    }
  }
  if (format.value === 'frames') {
    return { format: 'frames', ...common }
  }
  if (format.value === 'webp') {
    // 默认精灵图 WebP；若设置 frame 尺寸则导出单独 WebP 帧
    const asSpriteSheet = webpIsSheet.value
    return {
      format: 'webp', ...common,
      sprite_config: asSpriteSheet
        ? {
            layout: sprite.layout, columns: sprite.columns, padding: sprite.padding,
            frame_width: webp.frame_width, frame_height: webp.frame_height,
            generate_json: false, resample_filter: sprite.resample_filter,
            background_color: hexToRgba(sprite.bg_color),
          }
        : null,
      webp_config: { quality: webp.quality, frame_width: webp.frame_width, frame_height: webp.frame_height },
    }
  }
  return {
    format: 'godot', ...common,
    godot_config: {
      animation_name: godot.animation_name, fps: godot.fps, loop: godot.loop,
      export_individual_frames: godot.export_individual_frames,
      frame_width: godot.frame_width, frame_height: godot.frame_height,
    },
  }
}

const webpIsSheet = ref(true)

async function doExport() {
  if (!store.frames.length) return toast('请先抽帧')
  const config = buildConfig()
  await startJob(() => api.export(store.sessionId, { config, indices: selected.value.length ? selected.value : undefined }), {
    title: `导出 (${format.value})`,
    onDone: async () => {
      toast('导出完成')
      await refreshExports()
    },
  })
}

async function refreshExports() {
  try {
    exports.value = await api.exports(store.sessionId)
  } catch { /* ignore */ }
}

// ---- 精灵导出预设（自动流水线的导出参数；默认序列帧 PNG）----
function applyPreset(p) {
  if (!p || !p.format) return
  format.value = p.format
  if (p.pngquant_config) Object.assign(pngquant, p.pngquant_config)
  if (p.sprite_config) {
    const s = p.sprite_config
    Object.assign(sprite, { layout: s.layout ?? sprite.layout, columns: s.columns ?? null, padding: s.padding ?? 0,
      frame_width: s.frame_width ?? null, frame_height: s.frame_height ?? null,
      generate_json: s.generate_json ?? true, resample_filter: s.resample_filter ?? 'lanczos' })
  }
  if (p.gif_config) Object.assign(gif, p.gif_config)
  if (p.webp_config) Object.assign(webp, p.webp_config)
  if (p.godot_config) Object.assign(godot, p.godot_config)
  if (p.loop_transition) Object.assign(store.loopTransition, p.loop_transition)
}
async function savePreset() {
  if (!store.currentSprite) return
  const cfg = buildConfig()
  delete cfg.output_name
  // 缩放/描边单独存在 preset.scale / preset.outline，不在导出预设里留副本
  delete cfg.scale
  delete cfg.outline
  // 预设只管格式与参数：文件名是逐动作的，不进预设
  const output = { ...cfg }
  const sp = await api.patchSprite(store.currentSprite.id, {
    preset: { ...(store.currentSprite.preset || {}), output } })
  store.currentSprite.preset = sp.preset
  toast(`已保存「${store.currentSprite.name}」的导出预设：格式与参数，`
        + `该角色所有动作共用（文件名各自按动作走）`)
}
async function saveAlias() {
  const v = outputName.value.trim()
  if (!store.currentAction) return
  await api.patchAction(store.currentSprite.id, store.currentAction.id,
                        { export_alias: v })
  alias.value = v
  store.currentAction.export_alias = v
  nameParts.value = { ...nameParts.value, alias: v }
  toast(v ? `「${nameParts.value.action}」以后导出就叫 ${resolvedName.value}`
          : `已清除「${nameParts.value.action}」的导出名，落回动作名`)
}

onMounted(async () => {
  applyPreset(store.currentSprite?.preset?.output)
  try {
    const p = await api.exportNameParts(store.sessionId)
    nameParts.value = p
    alias.value = (p.alias || '').trim()
    // 动作上存过别名就以它为准——比角色级的名称格式更具体
    if (alias.value) outputName.value = alias.value
  } catch { /* ignore */ }
})

function fmtSize(b) {
  if (b < 1024) return b + ' B'
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' KB'
  return (b / (1024 * 1024)).toFixed(2) + ' MB'
}

function downloadExport(ex) {
  window.open(api.exportDownload(store.sessionId, ex.name), '_blank')
}

onMounted(refreshExports)
</script>

<template>
  <div class="grid2">
    <div>
      <div class="panel">
        <div class="section-title"><h2>导出</h2></div>
        <div class="row">
          <div class="field inline"><label>格式</label>
            <select v-model="format">
              <option v-for="f in formats" :key="f.key" :value="f.key">{{ f.name }}</option>
            </select>
          </div>
          <div class="field inline"><label>文件名</label>
            <input v-model="outputName" :placeholder="DEFAULT_PATTERN" style="width:170px"
                   title="占位符：{action} 动作名、{anim} Spine 动画名、{sprite} 角色名；留空即动作名" /></div>
          <span class="mono" title="这次导出实际会用的名字">→ {{ resolvedName }}</span>
          <span class="hint" :style="nameFrom.key === 'dirty' ? 'color:var(--warn)' : ''">
            {{ nameFrom.text }}</span>
          <button class="small" :disabled="nameFrom.key === 'action'"
                  :title="`把这个名字存到动作「${nameParts.action}」上：动作名不变，`
                          + '以后这个动作导出就用它（一键流水线也用）'"
                  @click="saveAlias">存为该动作的导出名</button>
          <button v-if="alias" class="small" title="清除后落回动作名"
                  @click="outputName = ''; saveAlias()">清除</button>
          <span class="hint">将导出 {{ selected.length || store.frameCount }} 帧（选中帧）</span>
        </div>
        <div class="row" style="align-items:center;gap:8px">
          <button class="small" title="把导出格式与参数存为本精灵的导出预设，该角色所有动作共用；文件名不进预设，各自按动作走"
                  @click="savePreset">保存为精灵导出预设</button>
          <span v-if="store.currentSprite?.preset?.output?.format" class="hint">
            当前预设：{{ store.currentSprite.preset.output.format }}（文件名按动作）</span>
          <span v-else class="hint">未设预设时流水线默认导出序列帧 PNG</span>
        </div>

        <!-- 精灵图配置 -->
        <div v-if="format === 'sprite_sheet' || (format === 'webp' && webpIsSheet)" class="row" style="border-top:1px solid var(--border); padding-top:10px">
          <div class="field inline"><label>布局</label>
            <select v-model="sprite.layout">
              <option value="grid">网格</option>
              <option value="horizontal">水平</option>
              <option value="vertical">垂直</option>
            </select>
          </div>
          <div v-if="sprite.layout === 'grid'" class="field inline"><label>列数</label><input type="number" v-model.number="sprite.columns" :min="1" /></div>
          <div class="field inline"><label>间距</label><input type="number" v-model.number="sprite.padding" :min="0" /></div>
          <div class="field inline"><label>帧宽</label><input type="number" v-model.number="sprite.frame_width" :min="1" placeholder="原始" /></div>
          <div class="field inline"><label>帧高</label><input type="number" v-model.number="sprite.frame_height" :min="1" placeholder="原始" /></div>
          <div class="field inline"><label>背景</label><input type="color" v-model="sprite.bg_color" /></div>
          <div class="field inline"><label>缩放算法</label>
            <select v-model="sprite.resample_filter">
              <option v-for="a in store.capabilities?.scale_algorithms || []" :key="a" :value="a">{{ a }}</option>
            </select>
          </div>
          <div class="field inline" v-if="format === 'sprite_sheet'"><label>生成JSON</label><input type="checkbox" v-model="sprite.generate_json" /></div>
        </div>

        <!-- GIF 配置 -->
        <div v-if="format === 'gif'" class="row" style="border-top:1px solid var(--border); padding-top:10px">
          <div class="field inline"><label>fps</label><input type="number" v-model.number="gif.fps" :min="1" :max="60" /></div>
          <div class="field inline"><label>循环</label><input type="number" v-model.number="gif.loop" :min="0" title="0=无限" /></div>
          <div class="field inline"><label>帧宽</label><input type="number" v-model.number="gif.frame_width" :min="1" /></div>
          <div class="field inline"><label>帧高</label><input type="number" v-model.number="gif.frame_height" :min="1" /></div>
          <div class="field inline"><label>优化</label><input type="checkbox" v-model="gif.optimize" /></div>
          <div class="field inline"><label>缩放算法</label>
            <select v-model="gif.resample_filter">
              <option v-for="a in store.capabilities?.scale_algorithms || []" :key="a" :value="a">{{ a }}</option>
            </select>
          </div>
        </div>

        <!-- WebP 配置 -->
        <div v-if="format === 'webp'" class="row" style="border-top:1px solid var(--border); padding-top:10px">
          <div class="field inline"><label>导出方式</label>
            <select v-model="webpIsSheet">
              <option :value="true">精灵图 WebP</option>
              <option :value="false">单独 WebP 帧</option>
            </select>
          </div>
          <div class="field inline"><label>质量</label><input type="number" v-model.number="webp.quality" :min="1" :max="100" /></div>
          <div class="field inline"><label>帧宽</label><input type="number" v-model.number="webp.frame_width" :min="1" placeholder="原始" /></div>
          <div class="field inline"><label>帧高</label><input type="number" v-model.number="webp.frame_height" :min="1" placeholder="原始" /></div>
        </div>

        <!-- Godot 配置 -->
        <div v-if="format === 'godot'" class="row" style="border-top:1px solid var(--border); padding-top:10px">
          <div class="field inline"><label>动画名</label><input v-model="godot.animation_name" /></div>
          <div class="field inline"><label>fps</label><input type="number" v-model.number="godot.fps" :min="1" :max="60" /></div>
          <div class="field inline"><label>循环</label><input type="checkbox" v-model="godot.loop" /></div>
          <div class="field inline"><label>导出单帧</label><input type="checkbox" v-model="godot.export_individual_frames" /></div>
          <div class="field inline"><label>帧宽</label><input type="number" v-model.number="godot.frame_width" :min="1" placeholder="原始" /></div>
          <div class="field inline"><label>帧高</label><input type="number" v-model.number="godot.frame_height" :min="1" placeholder="原始" /></div>
        </div>

        <!-- 循环过渡（与右侧预览面板共享设置） -->
        <div class="row" style="border-top:1px solid var(--border); padding-top:10px">
          <div class="field inline"><label>循环过渡</label>
            <input type="checkbox" :checked="store.loopTransition.enabled" @change="e => store.loopTransition.enabled = e.target.checked" />
          </div>
          <div v-if="store.loopTransition.enabled" class="field inline"><label>帧数</label>
            <input type="number" v-model.number="store.loopTransition.count" :min="1" :max="30" />
          </div>
          <div v-if="store.loopTransition.enabled" class="field inline"><label>模式</label>
            <select v-model="store.loopTransition.mode">
              <option value="blend">像素混合</option>
              <option value="align">轮廓对齐</option>
            </select>
          </div>
          <span class="hint">使导出的动画首尾无缝衔接</span>
        </div>

        <!-- PNG 压缩 -->
        <div class="row" style="border-top:1px solid var(--border); padding-top:10px">
          <div class="field inline"><label>PNG压缩</label><input type="checkbox" v-model="pngquant.enabled" /></div>
          <div v-if="pngquant.enabled">
            <div class="field inline"><label>质量范围</label>
              <input type="number" v-model.number="pngquant.quality_min" :min="0" :max="100" /> - <input type="number" v-model.number="pngquant.quality_max" :min="0" :max="100" />
            </div>
          </div>
        </div>

        <p class="hint" style="margin:8px 0 0">
          <template v-if="sizeOps.length">
            导出时将应用（不改帧文件）：{{ sizeOps.join(' → ') }}。
            在「图像处理」里可改这两个预设。
          </template>
          <template v-else>
            未设置缩放/描边预设，按帧的原样导出。需要的话到「图像处理」里存一个。
          </template>
        </p>
        <div class="row">
          <button class="primary" @click="doExport">开始导出</button>
        </div>
      </div>
    </div>

    <div>
      <div class="panel">
        <h3>导出记录</h3>
        <div v-for="ex in exports" :key="ex.name" class="job-item" style="margin-bottom:6px">
          <div class="row2">
            <span>{{ ex.name }}</span>
            <span>{{ fmtSize(ex.total_size) }}</span>
          </div>
          <div class="row" style="margin-top:4px">
            <span class="hint">{{ ex.files.map(f => f.name).join('、') }}</span>
            <button class="small" @click="downloadExport(ex)">下载</button>
          </div>
        </div>
        <p v-if="!exports.length" class="hint">暂无导出记录</p>
      </div>
    </div>
  </div>
</template>
