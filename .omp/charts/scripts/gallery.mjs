#!/usr/bin/env node
// gallery.html -- every chart x every sample x every theme, one self-contained file
// (bundle, data, fonts and icons inlined; opens from disk with no network). Charts render
// lazily as they scroll into view so the page stays responsive.
//
//   node scripts/gallery.mjs        (needs dist/: npm run charts:build)

import { readFileSync, writeFileSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { fontFaceCSS } from '../tests/browser/lib.mjs'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const need = (p) => { if (!existsSync(join(root, p))) { console.error(`${p} missing — run: npm run charts:build`); process.exit(1) } return readFileSync(join(root, p), 'utf8') }
const bundle = need('dist/charts.iife.js')
const data = need('dist/charts-data.js')
const sprite = existsSync(join(root, 'icons', 'icons.svg')) ? readFileSync(join(root, 'icons', 'icons.svg'), 'utf8') : '<svg style="display:none"></svg>'
const safe = (s) => s.replace(/<\/script/gi, '<\\/script')

const html = `<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chart pack gallery</title>
<style>
${fontFaceCSS(true)}
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;font-family:'IBM Plex Sans',ui-sans-serif,system-ui,sans-serif;background:#F7F4EE;color:#1A1917}
header{position:sticky;top:0;z-index:20;display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:10px 20px;background:#EDE9E0;border-bottom:1px solid #C9C2B4}
header h1{font-size:17px;margin:0 12px 0 0;font-weight:600}
header label{font-size:13px;display:flex;gap:6px;align-items:center}
header select,header input{font:inherit;font-size:13px;padding:3px 6px;border:1px solid #82796B;border-radius:3px;background:#F7F4EE;color:#1A1917}
header .count{font-size:12px;color:#5C5851;margin-left:auto}
nav{display:flex;flex-wrap:wrap;gap:4px 10px;padding:8px 20px;font-size:13px;border-bottom:1px solid #C9C2B4}
nav a{color:#3E6194;text-decoration:none}
main{padding:12px 20px 60px}
section.chart{margin:24px 0 8px;border-top:1px solid #C9C2B4;padding-top:12px}
section.chart h2{display:flex;gap:8px;align-items:center;font-size:16px;margin:0}
section.chart h2 svg{width:24px;height:24px;flex:none}
section.chart h2 code{font-size:12px;color:#5C5851;font-weight:400}
section.chart p{margin:4px 0 8px;font-size:13px;color:#423D37}
.intents{font-size:11.5px;color:#5C5851}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--cell,380px),1fr));gap:12px}
.sample{border:1px solid #C9C2B4;border-radius:6px;overflow:hidden;background:#F7F4EE}
.sample h3{font-size:12px;font-weight:600;margin:0;padding:6px 10px;border-bottom:1px solid #C9C2B4;display:flex;justify-content:space-between;gap:8px}
.sample h3 span{font-weight:400;color:#5C5851;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.themes{display:grid;grid-template-columns:1fr}
.themes[data-n="3"]{grid-template-columns:1fr}
.cell{padding:10px;min-height:120px}
.cell[data-theme="light"]{background:#EDE9E0}
.cell[data-theme="dark"]{background:#191817}
.cell[data-theme="high-contrast"]{background:#FFFFFF}
.cell .tag{font:11px 'IBM Plex Mono',monospace;color:#82796B;margin-bottom:4px}
.cell[data-theme="dark"] .tag{color:#979186}
.pending{display:flex;align-items:center;justify-content:center;height:200px;font-size:12px;color:#82796B}
.err{color:#9A3327;font-size:12px;white-space:pre-wrap}
</style>
</head>
<body>
${sprite}
<header>
  <h1>Chart pack gallery</h1>
  <label>Theme <select id="theme"><option value="all">all three</option><option value="light">light</option><option value="dark">dark</option><option value="high-contrast">high contrast</option></select></label>
  <label>Width <select id="width"><option value="380">380 px</option><option value="320">320 px</option><option value="560">560 px</option><option value="760">760 px</option></select></label>
  <label>Category <select id="category"><option value="">all</option></select></label>
  <label>Samples <select id="samples"><option value="all">all</option><option value="main">basic + realistic</option><option value="edge">edge cases</option></select></label>
  <label>Find <input id="q" type="search" placeholder="id, name or intent"></label>
  <span class="count" id="count"></span>
</header>
<nav id="nav" aria-label="Chart types"></nav>
<main id="main"></main>
<script>${safe(bundle)}</script>
<script>${safe(data)}</script>
<script>
(function(){
  var C = OmpCharts, charts = C.listCharts(), handles = new Map()
  var main = document.getElementById('main'), nav = document.getElementById('nav')
  var cats = Array.from(new Set(charts.map(function(c){return c.category})))
  var catSel = document.getElementById('category')
  cats.forEach(function(c){ var o = document.createElement('option'); o.value = c; o.textContent = c; catSel.appendChild(o) })
  var io = new IntersectionObserver(function(entries){
    entries.forEach(function(e){ if (e.isIntersecting) { io.unobserve(e.target); draw(e.target) } })
  }, { rootMargin: '400px' })
  function draw(cell){
    var host = cell.querySelector('.host'); host.innerHTML = ''
    try {
      var s = C.getSample(cell.dataset.type, cell.dataset.sample)
      var h = C.chart(cell.dataset.type, host, s.data, Object.assign({}, s.options || {}, { theme: cell.dataset.theme }))
      handles.set(cell, h)
    } catch (err) { host.innerHTML = '<div class="err"></div>'; host.firstChild.textContent = String(err && err.stack || err) }
  }
  function build(){
    handles.forEach(function(h){ h.destroy() }); handles.clear()
    main.replaceChildren(); nav.replaceChildren()
    var theme = document.getElementById('theme').value, width = document.getElementById('width').value
    var cat = catSel.value, q = document.getElementById('q').value.trim().toLowerCase(), which = document.getElementById('samples').value
    document.documentElement.style.setProperty('--cell', (theme === 'all' ? Math.max(+width, 320) : +width) + 'px')
    var themes = theme === 'all' ? ['light', 'dark', 'high-contrast'] : [theme]
    var shown = 0, renders = 0
    charts.forEach(function(c){
      if (cat && c.category !== cat) return
      if (q && (c.id + ' ' + c.name + ' ' + c.intents.join(' ') + ' ' + c.category).toLowerCase().indexOf(q) < 0) return
      shown++
      var a = document.createElement('a'); a.href = '#' + c.id; a.textContent = c.id; nav.appendChild(a)
      var sec = document.createElement('section'); sec.className = 'chart'; sec.id = c.id
      sec.innerHTML = '<h2><svg aria-hidden="true"><use href="#' + c.id + '"></use></svg><span></span><code></code></h2><p></p><div class="intents"></div><div class="grid"></div>'
      sec.querySelector('h2 span').textContent = c.name
      sec.querySelector('h2 code').textContent = c.id + ' · ' + c.category
      sec.querySelector('p').textContent = c.summary
      sec.querySelector('.intents').textContent = 'intents: ' + c.intents.join(', ')
      var grid = sec.querySelector('.grid')
      C.listSamples(c.id).forEach(function(name){
        if (which === 'main' && name !== 'basic' && name !== 'realistic') return
        if (which === 'edge' && name.indexOf('edge-') !== 0) return
        var s = C.getSample(c.id, name)
        var card = document.createElement('div'); card.className = 'sample'
        card.innerHTML = '<h3><b></b><span></span></h3><div class="themes"></div>'
        card.querySelector('b').textContent = name
        card.querySelector('h3 span').textContent = s.description || ''
        card.querySelector('h3 span').title = s.description || ''
        var box = card.querySelector('.themes')
        themes.forEach(function(t){
          var cell = document.createElement('div'); cell.className = 'cell'; cell.dataset.theme = t
          cell.dataset.type = c.id; cell.dataset.sample = name
          cell.innerHTML = '<div class="tag">' + t + '</div><div class="host"><div class="pending">rendering…</div></div>'
          box.appendChild(cell); io.observe(cell); renders++
        })
        grid.appendChild(card)
      })
      main.appendChild(sec)
    })
    document.getElementById('count').textContent = shown + ' chart types · ' + renders + ' renders'
  }
  ;['theme','width','category','samples'].forEach(function(id){ document.getElementById(id).addEventListener('change', build) })
  document.getElementById('q').addEventListener('input', build)
  window.__galleryRenderAll = function(){ document.querySelectorAll('.cell').forEach(function(cell){ if (!handles.has(cell)) { io.unobserve(cell); draw(cell) } }); return handles.size }
  build()
})()
</script>
</body>
</html>
`
writeFileSync(join(root, 'gallery.html'), html)
console.log(`gallery: gallery.html ${(html.length / 1024 / 1024).toFixed(1)} MB, self-contained`)
