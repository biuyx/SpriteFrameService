import { test } from 'node:test'
import assert from 'node:assert/strict'
import { catalogTree, categoryPath, membershipLabel, matchesScope } from '../src/libraryModel.js'
const data = {
  projects:[{id:'pr_a',name:'旅馆'},{id:'pr_b',name:'另一项目'}],
  categories:[{id:'cat_1',name:'人物',project_id:'pr_a',parent_id:null},{id:'cat_2',name:'工作人员',project_id:'pr_a',parent_id:'cat_1'}],
  types:[{id:'st_shop',name:'店长',project_id:'pr_a',category_id:'cat_2'},{id:'st_other',name:'店长',project_id:'pr_b',category_id:null}],
}
const role={project_id:'pr_a',sprite_type_id:'st_shop'}
test('tree preserves multiple levels and project isolation',()=>{
  const tree=catalogTree(data,'pr_a')
  assert.equal(tree[0].children[0].children[0].id,'st_shop')
  assert.equal(JSON.stringify(tree).includes('st_other'),false)
})
test('parent selection includes all descendants, not foreign same-name types',()=>{
  assert.equal(matchesScope(data,role,'pr_a','cat_1'),true)
  assert.equal(matchesScope(data,role,'pr_a','cat_2'),true)
  assert.equal(matchesScope(data,role,'pr_a','st_shop'),true)
  assert.equal(matchesScope(data,{project_id:'pr_b',sprite_type_id:'st_other'},'pr_a','cat_1'),false)
})
test('legacy and imported unresolved ownership remain discoverable',()=>{
  assert.equal(matchesScope(data,{},'unassigned'),true)
  assert.equal(matchesScope(data,{project_id:'missing'},'unassigned'),true)
  assert.equal(matchesScope(data,{project_id:'pr_a'},'pr_a','untyped'),true)
  assert.equal(matchesScope(data,role,'unassigned'),false)
})
test('breadcrumb distinguishes same names and follows rename without changing role',()=>{
  assert.equal(membershipLabel(data,role),'旅馆 / 人物 / 工作人员 / 店长')
  assert.equal(membershipLabel(data,{}),'待归类')
  assert.equal(categoryPath(data,'cat_2'),'人物 / 工作人员')
})
test('malformed old hierarchy cannot make navigation recurse forever',()=>{
  const broken={...data,categories:[{id:'loop',name:'Loop',project_id:'pr_a',parent_id:'loop'}]}
  assert.equal(categoryPath(broken,'loop'),'Loop')
  assert.deepEqual(catalogTree(broken,'pr_a'),[])
})
