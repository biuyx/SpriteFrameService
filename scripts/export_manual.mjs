// Export the same structured content as the in-app manual into a standalone file.
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { dirname, resolve, extname } from 'node:path'
import { MANUAL, GROUPS } from '../frontend/src/manual.js'
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const esc = s => String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;')
async function block(b) {
  if (b.t === 'figure') {
    const file = resolve(root, 'frontend/public', b.src.replace(/^\//, ''))
    const mime = extname(file) === '.svg' ? 'image/svg+xml' : 'image/png'
    return `<figure><img src="data:${mime};base64,${(await readFile(file)).toString('base64')}" alt="${esc(b.alt)}"><figcaption>${esc(b.caption)}</figcaption></figure>`
  }
  if (b.t === 'kv') return `<table>${b.rows.map(r => `<tr><th>${esc(r[0])}</th><td>${esc(r[1])}</td></tr>`).join('')}</table>`
  if (b.t === 'ol' || b.t === 'ul') return `<${b.t}>${b.items.map(x => `<li>${esc(x)}</li>`).join('')}</${b.t}>`
  const tag = {p:'p',h:'h3',note:'aside',code:'pre'}[b.t]
  if (!tag) throw new Error(`Unknown manual block: ${b.t}`)
  return `<${tag}>${esc(b.text)}</${tag}>`
}
const sections = await Promise.all(MANUAL.map(async s => `<section id="${esc(s.id)}"><h2>${esc(s.title)}</h2>${(await Promise.all(s.body.map(block))).join('\n')}</section>`))
const nav = GROUPS.map(g => `<h3>${g}</h3>${MANUAL.filter(s=>s.group===g).map(s=>`<a href="#${s.id}">${esc(s.title)}</a>`).join('')}`).join('')
const html = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>精灵帧工作室 · 图文使用手册</title><style>
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f3f6fa;color:#20304a;font:16px/1.85 "Microsoft YaHei",sans-serif}nav{position:fixed;width:240px;height:100vh;overflow:auto;padding:22px;background:#172b4d;color:white}nav h3{color:#a8c4ed;font-size:14px;margin:20px 0 5px}nav a{display:block;color:#eef5ff;text-decoration:none;font-size:14px;padding:3px 0}main{margin-left:240px;max-width:1100px;padding:36px 45px}header,section{padding:26px 34px;background:white;border-radius:12px;margin-bottom:22px}h1{font-size:30px;margin:0}h2{font-size:24px;margin-top:0}h3{font-size:18px;color:#22599b}figure{margin:22px 0}img{max-width:100%;height:auto;border:1px solid #dce3ee;border-radius:8px}figcaption{font-size:13px;color:#586c86}table{border-collapse:collapse;width:100%}th,td{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid #e2e8f0}th{min-width:110px}aside{background:#fff5de;border-left:4px solid #e9ac3b;padding:12px 16px}pre{overflow:auto;background:#edf2f8;padding:16px}section{scroll-margin-top:20px}li{margin:6px 0}.sub{color:#61738c}@media(max-width:750px){nav{position:static;width:100%;height:auto}main{margin:0;padding:14px}header,section{padding:20px}}@media print{nav{display:none}main{margin:0;padding:0;max-width:none}body{background:white}section{break-inside:auto}figure{break-inside:avoid}h2,h3{break-after:avoid}a{color:inherit}}
</style><nav><strong>精灵帧工作室</strong>${nav}</nav><main><header><h1>图文使用手册</h1><p class="sub">更新于 2026-10-19 · 与应用内「? 手册」共用内容 · 图片内嵌，可离线打开</p><p>从角色建档、模板绑定和立绘标记，到生成、抽帧、抠图与导出。先阅读入门，再按当前工序查阅。</p></header>${sections.join('\n')}</main></html>`
await mkdir(resolve(root, 'docs'), {recursive:true})
await writeFile(resolve(root, 'docs/user-manual.html'), html)
console.log(`Exported ${MANUAL.length} sections with embedded illustrations to docs/user-manual.html`)
