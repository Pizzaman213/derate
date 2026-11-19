#!/usr/bin/env node
// The living style guide: design/index.html, one self-contained file that
// opens from file:// with zero dependencies at view time.
//
//   cd .omp && npm run styleguide:build
//
// Generated from the same sources the product ships: tokens.css (plus the
// class-scoped theme copies build.mjs produces for this file only), the UI's
// base/derate/setup stylesheets, icons.svg, icons.json, pairs.json results,
// registry.json, and the gallery states rendered with react-dom/server
// against the REAL components -- the precedent is ui/src/tabs/cluster/
// loading.check.mjs. Fonts fall back to the system stacks: @fontsource is
// imported by main.tsx, not by a stylesheet, and this file does not bundle
// 400 KB of woff2 to show a swatch. Importable: `buildStyleGuide()` returns
// the HTML so generated.check.mjs can byte-compare.

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build as esbuild } from 'esbuild'
import { build as buildTokens, loadTokens } from './tokens/build.mjs'
import { runPairs } from './tokens/check.mjs'
import { build as buildIcons } from './icons/build.mjs'
import { caseSensitiveRelative, EXTENSIONS } from '../tests/ui/lib/esbuild.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const repo = resolve(here, '..', '..')
const ui = join(repo, 'ui')
export const OUTPUT = join(here, 'index.html')

const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')

async function renderStates() {
  // Bundle the gallery states under ui/node_modules so `react` resolves to the
  // UI's single copy -- two copies do not share a dispatcher.
  const cache = join(ui, 'node_modules', '.cache', 'design-pack')
  mkdirSync(cache, { recursive: true })
  const outfile = join(cache, 'states.mjs')
  await esbuild({
    entryPoints: [join(here, '..', 'tests', 'ui', 'gallery', 'states.tsx')],
    bundle: true,
    format: 'esm',
    outfile,
    jsx: 'automatic',
    platform: 'neutral',
    mainFields: ['module', 'main'],
    resolveExtensions: EXTENSIONS,
    plugins: [caseSensitiveRelative],
    nodePaths: [join(ui, 'node_modules')],
    external: ['react', 'react-dom', 'react/jsx-runtime'],
    loader: { '.css': 'empty', '.woff2': 'empty', '.woff': 'empty' },
    logLevel: 'warning',
  })
  const { STATES } = await import(pathToFileURL(outfile).href)
  const { renderToStaticMarkup } = await import(pathToFileURL(join(ui, 'node_modules', 'react-dom', 'server.js')).href)
  const rendered = []
  // Components that word a time relative to now ("3h ago") would make the
  // output a function of the wall clock, and generated.check would call the
  // style guide stale every hour. Render at one fixed instant instead: just
  // after the newest fixture timestamp in states.tsx.
  const realNow = Date.now
  Date.now = () => STYLEGUIDE_NOW_MS
  try {
    for (const s of STATES) {
      if (s.interactive) { rendered.push({ ...s, html: null }); continue }
      rendered.push({ ...s, html: renderToStaticMarkup(s.render()) })
    }
  } finally {
    Date.now = realNow
  }
  return rendered
}

/** The instant the style guide is rendered at (ms): 2025-11-17T15:46:40Z. */
const STYLEGUIDE_NOW_MS = 1_763_394_400_000

export async function buildStyleGuide() {
  const tokens = buildTokens(loadTokens())
  const pairs = JSON.parse(readFileSync(join(here, 'tokens', 'pairs.json'), 'utf8'))
  const ratios = runPairs(tokens.flat, pairs)
  const icons = buildIcons()
  const registry = JSON.parse(readFileSync(join(here, 'components', 'registry.json'), 'utf8'))
  const states = await renderStates()
  const css = ['base.css', 'derate.css'].map((f) => readFileSync(join(ui, 'src', 'styles', f), 'utf8')).join('\n') + '\n' + readFileSync(join(ui, 'src', 'tabs', 'setup', 'setup.css'), 'utf8')

  const byName = new Map(tokens.named.map((n) => [n.css.slice(2), n]))
  const themed = (name) => { const v = tokens.flat[name]; return typeof v === 'object' ? v : { light: v, dark: v, 'high-contrast': v } }
  const contrastFor = (name) => ratios.filter((r) => r.fg === name).map((r) => `${r.theme[0]}:${r.ratio.toFixed(1)}${r.ok ? '' : '✗'} on ${r.bg}`).join(' · ')

  const swatches = (names) => names.map((name) => {
    const t = themed(name), n = byName.get(name)
    const bg = (v) => (/^(#|rgb|hsl|color-mix)/.test(String(v)) ? `background:${v}` : '')
    return `<div class="sw"><div class="sw-chips"><span style="${bg(t.light)}" title="light"></span><span style="${bg(t.dark)}" title="dark"></span><span style="${bg(t['high-contrast'])}" title="high-contrast"></span></div><code>--${name}</code><small>${esc(t.light)} · ${esc(t.dark)} · ${esc(t['high-contrast'])}</small><small class="desc">${esc(n?.leaf.$description ?? '')}</small>${contrastFor(name) ? `<small class="cr">${esc(contrastFor(name))}</small>` : ''}</div>`
  }).join('')

  const sem = tokens.named.filter((n) => n.layer === 'semantic')
  const colourNames = sem.filter((n) => n.path[1] === 'color').map((n) => n.css.slice(2))
  const typeNames = sem.filter((n) => n.path[1] === 'type' && n.css.startsWith('--size-')).map((n) => n.css.slice(2))
  const spaceNames = tokens.named.filter((n) => n.path[0] === 'space').map((n) => n.css.slice(2))
  const radiusNames = sem.filter((n) => n.css.startsWith('--radius')).map((n) => n.css.slice(2))
  const motion = sem.filter((n) => n.path[1] === 'motion')
  const compNames = tokens.named.filter((n) => n.layer === 'component')

  const typeScale = typeNames.map((n) => `<div class="ty"><code>--${n}</code><span class="ty-sans" style="font-size:var(--${n})">Fits on spark-01 at 32768 ctx</span><span class="ty-mono mono" style="font-size:var(--${n})">119.7 GB · 2.94 GB/s</span><small>${esc(tokens.flat[n])}</small></div>`).join('')
  const spacing = spaceNames.map((n) => `<div class="sp"><code>--${n}</code><i style="width:var(--${n})"></i><small>${esc(tokens.flat[n])}</small></div>`).join('')
  const radii = radiusNames.map((n) => `<div class="rd"><i style="border-radius:var(--${n})"></i><code>--${n}</code><small>${esc(tokens.flat[n])}</small></div>`).join('')
  const shadows = `<div class="sh"><i style="box-shadow:var(--shadow-raised)"></i><code>--shadow-raised</code><small>${esc(JSON.stringify(themed('shadow-raised')))}</small></div>`
  const motionCards = motion.map((n) => `<div class="mo"><code>${n.css}</code><small>${esc(tokens.flat[n.css.slice(2)])}</small><small class="desc">${esc(n.leaf.$description)}</small>${n.css.startsWith('--dur') ? `<i class="mo-demo" style="transition-duration:var(${n.css})"></i>` : ''}</div>`).join('')
  const componentTokens = compNames.map((n) => `<tr><td><code>${n.css}</code></td><td><code>${esc(n.leaf.$value)}</code></td><td>${esc(tokens.flat[n.css.slice(2)]?.light ?? tokens.flat[n.css.slice(2)])}</td></tr>`).join('')

  const iconCards = icons.meta.icons.map((i) => `<div class="ic"><svg width="24" height="24"><use href="#icon-${i.name}"/></svg><code>${i.name}</code><small>${i.category} · ${esc(i.tags.join(', '))}</small>${i.replaces.length ? `<small class="desc">replaces ${esc(i.replaces.join(' '))}</small>` : ''}</div>`).join('')

  const components = registry.components.filter((c) => c.kind !== 'screen').map((c) => {
    const mine = states.filter((s) => s.component === c.name)
    const anchor = c.spec.split('#')[1]
    const cells = mine.map((s) => `<figure class="st"><figcaption><code>${esc(s.variant)}</code> · ${esc(s.state)}</figcaption>${s.html === null ? `<div class="interactive">interactive — rendered in the browser gallery (<code>npm run ui:check</code>)</div>` : `<div class="st-body">${s.html}</div>`}</figure>`).join('')
    return `<section class="comp" id="c-${c.name.toLowerCase()}"><h3>${c.name} <small>${c.kind} · <code>${esc(c.path)}</code> · <a href="DESIGN.md#${anchor}">spec</a> · <code>${esc(c.tests ?? 'no test')}</code></small></h3><div class="st-grid">${cells || '<p class="unit">no gallery states</p>'}</div></section>`
  }).join('')

  const pairRows = ratios.map((r) => `<tr class="${r.ok ? '' : 'bad'}"><td>${r.theme}</td><td>${r.fg}</td><td>${r.bg}</td><td class="num">${r.ratio.toFixed(2)}</td><td class="num">${r.min}</td><td>${r.ok ? 'pass' : 'FAIL'}</td></tr>`).join('')

  const guideCss = `
.g-top{position:sticky;top:0;z-index:var(--z-overlay);display:flex;gap:var(--space-8);align-items:center;padding:var(--space-8) var(--space-20);background:var(--panel-raised);border-bottom:1px solid var(--rule)}
.g-top b{margin-right:auto}.g-note{font-size:var(--size-unit);color:var(--ink-muted);padding:var(--space-8) var(--space-20)}
.g-main{padding:0 var(--space-20) var(--space-48);max-width:1200px}
.g-main h2{margin:var(--space-36) 0 var(--space-12);font-size:var(--size-readout-sm);font-weight:500}
.g-main h3{margin:var(--space-24) 0 var(--space-8)}.g-main h3 small{font-weight:400;color:var(--ink-muted);font-size:var(--size-unit)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:var(--space-12)}
.sw{display:grid;gap:var(--space-4);font-size:var(--size-unit)}.sw-chips{display:flex;height:40px;border:1px solid var(--edge);border-radius:var(--radius)}.sw-chips span{flex:1}
.sw small,.ic small,.ty small,.mo small{color:var(--ink-muted);font-size:var(--size-micro)}.desc{display:block}.cr{font-family:var(--font-mono)}
.ty{display:grid;grid-template-columns:200px 1fr 1fr 60px;gap:var(--space-12);align-items:baseline;padding:var(--space-6) 0;border-bottom:1px solid var(--rule)}
.sp{display:flex;gap:var(--space-12);align-items:center;font-size:var(--size-unit)}.sp i{display:block;height:12px;background:var(--fill)}.sp code{width:110px}
.rd{display:flex;gap:var(--space-8);align-items:center}.rd i{display:block;width:40px;height:40px;border:1px solid var(--edge);background:var(--panel-raised)}
.sh i{display:block;width:120px;height:60px;background:var(--panel-raised);margin:var(--space-12) 0}
.mo{display:grid;gap:var(--space-4)}.mo-demo{display:block;width:40px;height:8px;background:var(--flow);transition-property:transform;transition-timing-function:var(--ease)}.mo:hover .mo-demo{transform:translateX(120px)}
.ic{display:grid;gap:var(--space-4);justify-items:start;font-size:var(--size-unit)}
.comp{border-top:1px solid var(--rule);padding-top:var(--space-8)}.st-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:var(--space-12)}
.st{margin:0;border:1px solid var(--rule);border-radius:var(--radius);background:var(--panel)}.st figcaption{font-size:var(--size-micro);color:var(--ink-muted);padding:var(--space-4) var(--space-8);border-bottom:1px solid var(--rule)}.st-body{padding:var(--space-12);overflow:auto}
.interactive{padding:var(--space-12);font-size:var(--size-unit);color:var(--ink-muted)}
table.pairs{font-size:var(--size-unit)}table.pairs .bad td{color:var(--fault)}
.side-by-side .themes{display:grid;grid-template-columns:repeat(3,1fr);gap:var(--space-12)}.themes>div{padding:var(--space-12);background:var(--panel);color:var(--ink);border:1px solid var(--rule)}
.themes>div{display:none}.themes>div.active,.side-by-side .themes>div{display:block}
`

  const tokenSections = `
<h2 id="colour">Colour <small>(${colourNames.length} semantic roles; chips are light · dark · high-contrast; contrast per theme from pairs.json)</small></h2>
<div class="grid">${swatches(colourNames)}</div>
<h2 id="type">Type scale</h2>${typeScale}
<h2 id="space">Spacing</h2><div style="display:grid;gap:var(--space-4)">${spacing}</div>
<h2 id="radius">Radii</h2><div class="grid">${radii}</div>
<h2 id="shadow">Elevation</h2>${shadows}
<h2 id="motion">Motion <small>(hover a duration card)</small></h2><div class="grid">${motionCards}</div>`

  const html = `<!doctype html>
<!-- GENERATED by .omp/design/styleguide.mjs — edit tokens.json, icons/, registry.json, tests/ui/gallery/states.tsx or the UI stylesheets, then: cd .omp && npm run styleguide:build -->
<html lang="en"><head><meta charset="utf-8"><title>derate — style guide</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>${tokens.css}</style>
<style>${tokens.themeClassCss}</style>
<style>${css}</style>
<style>${guideCss}</style>
</head><body>
${icons.sprite}
<div class="g-top"><b>derate — style guide</b>
<button type="button" data-theme-set="light">Light</button><button type="button" data-theme-set="dark">Dark</button><button type="button" data-theme-set="high-contrast">High contrast</button><button type="button" data-theme-set="">System</button>
<label><input type="checkbox" id="sbs"> side by side</label></div>
<p class="g-note">Generated from the shipped sources. IBM Plex is loaded by the app's <code>main.tsx</code> through @fontsource, not by a stylesheet, so this page falls back to the system sans and mono stacks. Component figures are <code>react-dom/server</code> output of the real components; interactive ones are exercised by the browser gallery.</p>
<main class="g-main">
<nav><a href="#colour">Colour</a> · <a href="#type">Type</a> · <a href="#space">Spacing</a> · <a href="#radius">Radii</a> · <a href="#shadow">Elevation</a> · <a href="#motion">Motion</a> · <a href="#icons">Icons</a> · <a href="#components">Components</a> · <a href="#component-tokens">Component tokens</a> · <a href="#pairs">Contrast</a> · <a href="DESIGN.md">DESIGN.md</a></nav>
<div class="themes"><div class="theme-light active">${tokenSections}</div><div class="theme-dark">${tokenSections}</div><div class="theme-high-contrast">${tokenSections}</div></div>
<h2 id="icons">Icons <small>(${icons.icons.length}; 24-box, 1.5px, currentColor)</small></h2><div class="grid">${iconCards}</div>
<h2 id="components">Components <small>(${registry.components.filter((c) => c.kind !== 'screen').length} registered; ${states.length} states)</small></h2>${components}
<h2 id="component-tokens">Component tokens</h2><table class="pairs"><thead><tr><th>name</th><th>reference</th><th>light value</th></tr></thead><tbody>${componentTokens}</tbody></table>
<h2 id="pairs">Contrast pairs <small>(${ratios.filter((r) => r.ok).length}/${ratios.length} pass)</small></h2><table class="pairs"><thead><tr><th>theme</th><th>fg</th><th>bg</th><th class="num">ratio</th><th class="num">min</th><th></th></tr></thead><tbody>${pairRows}</tbody></table>
</main>
<script>
for (const b of document.querySelectorAll('[data-theme-set]')) b.addEventListener('click', () => {
  const t = b.getAttribute('data-theme-set')
  if (t) document.documentElement.setAttribute('data-theme', t); else document.documentElement.removeAttribute('data-theme')
  for (const d of document.querySelectorAll('.themes > div')) d.classList.toggle('active', d.classList.contains('theme-' + (t || 'light')))
})
document.getElementById('sbs').addEventListener('change', (e) => document.body.classList.toggle('side-by-side', e.target.checked))
</script>
</body></html>
`
  return html
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const html = await buildStyleGuide()
  writeFileSync(OUTPUT, html)
  console.log(`style guide: wrote design/index.html (${(html.length / 1024).toFixed(0)} KiB)`)
}
