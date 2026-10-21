import test from 'node:test'
import assert from 'node:assert/strict'
import { canCancelJob, jobStatus, filterJobs } from '../src/jobManagement.js'

test('cancel is offered only for active jobs without a stop request', () => {
  assert.equal(canCancelJob({status:'running'}), true)
  assert.equal(canCancelJob({status:'queued',cancel_requested:true}), false)
  for (const status of ['done','error','cancelled']) assert.equal(canCancelJob({status}), false)
  assert.equal(jobStatus({status:'running',cancel_requested:true}), '正在停止')
  assert.equal(jobStatus({status:'cancelled',cancel_requested:true}), '已取消')
})
test('global filters find server-owned jobs by role, action, ID and type', () => {
  const jobs = [{id:'server-abc',type:'background',status:'running',context:{sprite_name:'店长4',action_name:'hawk'}},
    {id:'old-record',type:'export',status:'done',context:{}},
    {id:'cancelled-record',type:'extract',status:'cancelled'}]
  assert.equal(filterJobs(jobs,'active','店长4').length,1)
  assert.equal(filterJobs(jobs,'all','HAWK').length,1)
  assert.equal(filterJobs(jobs,'all','server-abc').length,1)
  assert.equal(filterJobs(jobs,'all','抠图').length,1)
  assert.equal(filterJobs(jobs,'done','','export')[0].id,'old-record')
  assert.equal(filterJobs(jobs,'active','','export').length,0)
  assert.equal(filterJobs(jobs,'all','').length,3)
})
