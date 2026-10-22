// Keep one confirmed operation's identity through response loss and page reload.
// A successful acknowledgement releases it so a later confirmed new take works.
const inFlight = new Map()

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map(k => [k, canonical(value[k])]))
  }
  return value
}

function newId() {
  const bytes = new Uint8Array(16)
  globalThis.crypto.getRandomValues(bytes)
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
}

export function idempotentRequest(operation, payload, send, options = {}) {
  const signature = JSON.stringify(canonical(payload))
  const flight = `${operation}:${signature}`
  if (inFlight.has(flight)) return inFlight.get(flight)
  const key = `sprite-request:${flight}`
  let storage, record
  try {
    storage = options.storage || globalThis.sessionStorage
    const old = JSON.parse(storage.getItem(key) || 'null')
    record = old?.signature === signature ? old : {
      signature, id: payload.request_id || (options.createId || newId)(),
    }
    storage.setItem(key, JSON.stringify(record))
  } catch {
    return Promise.reject(new Error('无法保存请求编号，已阻止提交；请检查浏览器存储后再试，避免重复付费'))
  }
  const forget = () => {
    // Another operation may have changed the selection while this one waited.
    try {
      if (JSON.parse(storage.getItem(key) || 'null')?.id === record.id) storage.removeItem(key)
    } catch { /* Keeping a receipt is safer than creating a new identity. */ }
  }
  const pending = Promise.resolve().then(() => send({ ...payload, request_id: record.id }))
    .then(result => { forget(); return result })
    .catch(error => {
      if (error.status >= 400 && error.status < 500 && error.status !== 408) forget()
      throw error
    }).finally(() => inFlight.delete(flight))
  inFlight.set(flight, pending)
  return pending
}
