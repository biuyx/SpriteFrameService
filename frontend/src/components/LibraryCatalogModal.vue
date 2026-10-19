<script setup>
import { ref, computed, onMounted } from 'vue'
import api from '../api'
import { toast, askConfirm } from '../stores'
import { categoryPath, typeLabel } from '../libraryModel'
const props = defineProps({ catalog: Object, projectId: String })
const emit = defineEmits(['close', 'saved'])
const kind = ref('projects'), id = ref(null), name = ref(''), project = ref(props.projectId || '')
const parent = ref(''), group = ref(''), fsid = ref(''), shared = ref([]), error = ref(''), busy = ref(false)
const sets = ref([]), templates = ref([])
const labels = {projects:'项目',categories:'分类目录',types:'精灵类型'}
const categories = computed(() => props.catalog.categories.filter(c => c.project_id === project.value))
const records = computed(() => props.catalog[kind.value].filter(r => kind.value==='projects' || r.project_id===project.value))
const groups = computed(() => [...new Set([...sets.value, ...templates.value].map(r=>r.group).filter(Boolean))])
onMounted(async()=>{try{const [fs,ts]=await Promise.all([api.ffsets(),api.templates()]);sets.value=fs.sets;templates.value=ts.templates}catch(e){error.value=e.message}})
function reset(k=kind.value){kind.value=k;id.value=null;name.value='';parent.value='';group.value='';fsid.value='';shared.value=[];error.value=''}
function edit(r){id.value=r.id;name.value=r.name;project.value=r.project_id||project.value;parent.value=r.parent_id||r.category_id||'';const b=r.template_binding||{};group.value=b.group||'';fsid.value=b.first_frame_set_id||'';shared.value=b.shared_template_ids||[];error.value=''}
async function save(){
  busy.value=true;error.value=''
  try{
    const payload={name:name.value.trim()}
    if(kind.value!=='projects')payload.project_id=project.value||null
    if(kind.value==='categories')payload.parent_id=parent.value||null
    if(kind.value==='types'){
      payload.category_id=parent.value||null
      payload.template_binding=group.value.trim()?{group:group.value.trim(),first_frame_set_id:fsid.value||null,shared_template_ids:shared.value}:{}
    }
    const r=await api.saveCatalog(kind.value,payload,id.value)
    if(kind.value==='projects')project.value=r.id
    reset();emit('saved');toast('分类设置已保存')
  }catch(e){error.value=e.message}finally{busy.value=false}
}
async function remove(r){if(!await askConfirm(`删除${labels[kind.value]}「${r.name}」？仅未被使用的条目可删除。`))return;busy.value=true;try{await api.deleteCatalog(kind.value,r.id);reset();emit('saved')}catch(e){error.value=e.message}finally{busy.value=false}}
</script>
<template>
  <div class="catalog-mask" @click.self="emit('close')"><section class="catalog-dialog">
    <header><h3>管理项目、分类与类型</h3><button @click="emit('close')">关闭</button></header>
    <nav><button v-for="(label,k) in labels" :key="k" :class="{primary:kind===k}" @click="reset(k)">{{ label }}</button></nav>
    <label v-if="kind!=='projects'">所属项目 <select v-model="project" :disabled="!!id" @change="reset()"><option value="">请选择项目</option><option v-for="p in catalog.projects" :key="p.id" :value="p.id">{{ p.name }}</option></select></label>
    <div class="catalog-columns">
      <div class="catalog-records"><p v-if="!records.length" class="hint">还没有{{ labels[kind] }}，在右侧新建。</p><div v-for="r in records" :key="r.id"><button @click="edit(r)">{{ kind==='categories'? [categoryPath(catalog,r.parent_id),r.name].filter(Boolean).join(' / '):kind==='types'?typeLabel(catalog,r):r.name }}</button><button class="small" :disabled="busy" @click="remove(r)">删除</button></div></div>
      <form @submit.prevent="save"><h4>{{ id?'编辑':'新建' }}{{ labels[kind] }}</h4><label>名称 <input v-model="name" maxlength="80" required /></label>
        <label v-if="kind!=='projects'">{{ kind==='categories'?'父分类':'所在分类' }} <select v-model="parent"><option value="">项目根目录</option><option v-for="c in categories.filter(c=>c.id!==id)" :key="c.id" :value="c.id">{{ categoryPath(catalog,c.id) }}</option></select></label>
        <template v-if="kind==='types'">
          <label>默认参考分组 <input v-model="group" list="type-groups" placeholder="可留空，仅用于归类" @change="fsid='';shared=[]" /></label><datalist id="type-groups"><option v-for="g in groups" :key="g" :value="g" /></datalist>
          <label>默认首帧集 <select v-model="fsid" :disabled="!group.trim()"><option value="">暂未配置</option><option v-for="s in sets.filter(s=>(s.group||'').trim()===group.trim())" :key="s.id" :value="s.id">{{ s.name }}</option></select></label>
          <details v-if="group.trim()"><summary>默认共享参考动作（{{shared.length}}）</summary><label v-for="t in templates.filter(t=>(t.group||'').trim()!==group.trim())" :key="t.id"><input type="checkbox" v-model="shared" :value="t.id" />{{t.group||'未分组'}} / {{t.key}} {{t.variant||''}}</label></details>
          <p class="hint">默认绑定仅用于新建角色，不修改已有角色。第一阶段不创建标准动作清单；需要动作骨架时仍选择样板角色。类型已有角色时不能直接更换参考分组。</p>
        </template>
        <p v-if="error" class="error">{{ error }}</p><footer><button type="button" @click="reset()">清空 / 新建</button><button class="primary" :disabled="busy || (kind!=='projects' && !project)">保存</button></footer>
      </form>
    </div>
    <p class="hint">目录可任意嵌套，不能形成循环或跨项目移动。改名和调整目录不移动素材、不改变角色参考绑定；删除前须先移出所有引用。</p>
  </section></div>
</template>
<style scoped>
.catalog-mask{position:fixed;inset:0;background:#0009;z-index:100;display:flex;align-items:center;justify-content:center}.catalog-dialog{width:min(960px,95vw);max-height:90vh;overflow:auto;background:var(--bg-panel);border:1px solid var(--border);border-radius:10px;padding:24px}header,nav,footer{display:flex;align-items:center;gap:10px}header{justify-content:space-between}nav{margin:14px 0}.catalog-columns{display:grid;grid-template-columns:1fr 1.3fr;gap:22px}.catalog-records{max-height:55vh;overflow:auto}.catalog-records>div{display:flex;gap:6px;margin:8px 0}.catalog-records>div>button:first-child{flex:1;text-align:left;overflow-wrap:anywhere}label{display:flex;align-items:center;gap:8px;margin:12px 0}form input:not([type=checkbox]),select{min-width:0;flex:1;width:100%}details{max-height:200px;overflow:auto}footer{justify-content:flex-end}.error{color:var(--err);white-space:pre-wrap}.hint{line-height:1.7}@media(max-width:700px){.catalog-columns{grid-template-columns:1fr}}
</style>
