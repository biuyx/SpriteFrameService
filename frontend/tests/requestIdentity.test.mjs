import test from 'node:test'
import assert from 'node:assert/strict'
import { idempotentRequest } from '../src/requestIdentity.js'

function harness() {
  const map = new Map(); let n = 0
  return { storage: { getItem: k => map.get(k) || null,
    setItem: (k, v) => map.set(k, v), removeItem: k => map.delete(k) },
    createId: () => `request-${++n}` }
}

test('response loss and reload retry use the original identity', async () => {
  const options = harness(); const sent = []
  await assert.rejects(idempotentRequest('lost', { prompt: 'same' }, p => {
    sent.push(p.request_id); throw new Error('network response lost')
  }, options))
  await idempotentRequest('lost', { prompt: 'same' }, p => { sent.push(p.request_id); return {} }, options)
  assert.deepEqual(sent, ['request-1', 'request-1'])
})

test('parallel calls share one submission', async () => {
  const options = harness(); let calls = 0; let finish
  const send = p => { calls++; return new Promise(resolve => { finish = resolve }) }
  const first = idempotentRequest('parallel', { prompt: 'same' }, send, options)
  const second = idempotentRequest('parallel', { prompt: 'same' }, send, options)
  await Promise.resolve(); assert.equal(calls, 1); finish({ job_id: 'one' })
  assert.deepEqual(await Promise.all([first, second]), [{ job_id: 'one' }, { job_id: 'one' }])
})

test('a later acknowledged explicit operation gets a new identity', async () => {
  const options = harness(); const sent = []
  const send = p => { sent.push(p.request_id); return {} }
  await idempotentRequest('new-take', {}, send, options)
  await idempotentRequest('new-take', {}, send, options)
  assert.deepEqual(sent, ['request-1', 'request-2'])
})

test('uncertain server failure keeps identity; definitive rejection permits correction', async () => {
  const options = harness(); const sent = []
  const fail = status => p => { sent.push(p.request_id); throw Object.assign(new Error('failed'), { status }) }
  await assert.rejects(idempotentRequest('errors', {}, fail(503), options))
  await assert.rejects(idempotentRequest('errors', {}, fail(409), options))
  await idempotentRequest('errors', {}, p => sent.push(p.request_id), options)
  assert.deepEqual(sent, ['request-1', 'request-1', 'request-2'])
})

test('object key order does not change retry identity', async () => {
  const options = harness(); const sent = []
  await assert.rejects(idempotentRequest('canonical', { a: 1, b: 2 }, p => {
    sent.push(p.request_id); throw new Error('lost')
  }, options))
  await idempotentRequest('canonical', { b: 2, a: 1 }, p => sent.push(p.request_id), options)
  assert.deepEqual(sent, ['request-1', 'request-1'])
})

test('unavailable browser receipt storage fails before submission', async () => {
  let calls = 0
  await assert.rejects(idempotentRequest('storage', {}, () => { calls++ }, {
    storage: { getItem: () => null, setItem: () => { throw new Error('denied') } }, createId: () => 'id',
  }), /已阻止提交/)
  assert.equal(calls, 0)
})

test('changing inputs does not erase an earlier unacknowledged receipt', async () => {
  const options = harness(); const sent = []
  const lost = p => { sent.push(p.request_id); throw new Error('response lost') }
  await assert.rejects(idempotentRequest('switch-inputs', { prompt: 'a' }, lost, options))
  await assert.rejects(idempotentRequest('switch-inputs', { prompt: 'b' }, lost, options))
  await idempotentRequest('switch-inputs', { prompt: 'a' }, p => sent.push(p.request_id), options)
  assert.deepEqual(sent, ['request-1', 'request-2', 'request-1'])
})
