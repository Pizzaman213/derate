#!/usr/bin/env node
// Cross-chart browser suites over dist/ (run charts:build first):
//
//   visual   every chart x basic+realistic x light/dark/high-contrast x 320/768/1280,
//            screenshot vs baseline (tests/visual/baseline; first run records it)
//   layout   every chart x every sample x 320/768/1280: nothing overflows, no clipped
//            text, no overlapping axis labels, legend fits, tooltip stays on screen
//   a11y     every chart x every sample: axe-core (WCAG 2.1 A/AA) zero violations,
//            keyboard reaches every mark and announces it, aria-label matches summary
//   perf     first-render budgets (<100 ms for 1k SVG marks, <500 ms for 50k canvas marks)
//            -> tests/perf-report.md
//
//   node tests/browser/run.mjs --suite visual,layout,a11y,perf [--type bar,pie] [--category trend]
//                              [--update] [--workers 4]
//
// Results: tests/.results/browser.json (per chart, per suite); exit 1 on any failure.

import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { PNG } from 'pngjs'
import pixelmatch from 'pixelmatch'
import { launch, newPage, renderIn, inventory, argv, ROOT } from './lib.mjs'
import { layoutCheck, markCenter, tooltipCheck } from './checks.mjs'
import { perfCases } from './perf-data.mjs'

const a = argv()
const suites = (a.get('suite') ?? 'visual,layout,a11y').split(',')
const only = a.get('type')?.split(',')
const category = a.get('category')
const update = a.has('update')
const workers = Number(a.get('workers') ?? 4)
const THEMES = ['light', 'dark', 'high-contrast']
const WIDTHS = [320, 768, 1280]
const VIS = join(ROOT, 'tests', 'visual')
const RESULTS = join(ROOT, 'tests', '.results', 'browser.json')
const AXE = join(ROOT, 'node_modules', 'axe-core', 'axe.min.js')

const { browser, url } = await launch()
const probe = await newPage(browser, url)
let charts = await inventory(probe.page)
await probe.ctx.close()
if (only) charts = charts.filter((c) => only.includes(c.id))
if (category) charts = charts.filter((c) => c.category === category)
if (!charts.length) { console.error('no charts match'); process.exit(2) }

const results = existsSync(RESULTS) ? JSON.parse(readFileSync(RESULTS, 'utf8')) : { charts: {} }
const failures = []
const record = (id, suite, ok, detail) => {
  const r = ((results.charts[id] ??= {})[suite] ??= { passed: 0, failed: 0, findings: [], at: '' })
  if (ok) r.passed++
  else { r.failed++; if (r.findings.length < 20) r.findings.push(detail) }
  r.at = new Date().toISOString()
  if (!ok) failures.push(`${id} ${suite}: ${detail}`)
}
const reset = (id, suite) => { if (results.charts[id]) delete results.charts[id][suite] }

// ---------- job queue across pages
async function pool(jobs, fn) {
  let next = 0
  const run = async () => {
    const p = await newPage(browser, url, { width: 1400, height: 1000 })
    await p.page.addScriptTag({ path: AXE })
    while (next < jobs.length) {
      const job = jobs[next++]
      p.errors.length = 0
      try { await fn(p, job) } catch (e) { record(job.id, job.suite, false, `${job.sample ?? ''} threw: ${String(e).split('\n')[0]}`) }
    }
    await p.ctx.close()
  }
  await Promise.all(Array.from({ length: Math.min(workers, jobs.length) }, run))
}
const cmpPng = (aBuf, bBuf) => {
  const A = PNG.sync.read(aBuf), B = PNG.sync.read(bBuf)
  if (A.width !== B.width || A.height !== B.height) return { ratio: 1, diff: null, size: `${A.width}x${A.height} vs ${B.width}x${B.height}` }
  const diff = new PNG({ width: A.width, height: A.height })
  const n = pixelmatch(A.data, B.data, diff.data, A.width, A.height, { threshold: 0.1 })
  return { ratio: n / (A.width * A.height), diff }
}

const t0 = Date.now()
for (const c of charts) for (const s of suites) reset(c.id, s)

// ---------- visual
if (suites.includes('visual')) {
  const jobs = charts.flatMap((c) => ['basic', 'realistic'].filter((s) => c.samples.includes(s)).flatMap((sample) => THEMES.flatMap((theme) => WIDTHS.map((width) => ({ id: c.id, suite: 'visual', sample, theme, width })))))
  let created = 0
  await pool(jobs, async ({ page, errors }, j) => {
    const r = await renderIn(page, { type: j.id, sample: j.sample, width: j.width, theme: j.theme, options: { animate: false } })
    if (!r.ok) return record(j.id, 'visual', false, `${j.sample} ${j.theme} ${j.width}: ${r.error}`)
    const shot = await page.locator('#frame').screenshot()
    const name = `${j.sample}-${j.theme}-${j.width}.png`
    const base = join(VIS, 'baseline', j.id, name)
    mkdirSync(dirname(base), { recursive: true })
    if (!existsSync(base) || update) { writeFileSync(base, shot); created++; return record(j.id, 'visual', true) }
    const { ratio, diff, size } = cmpPng(readFileSync(base), shot)
    if (ratio <= 0.002) return record(j.id, 'visual', true)
    for (const [dir, buf] of [['actual', shot], ['diff', diff ? PNG.sync.write(diff) : null]]) {
      if (!buf) continue
      mkdirSync(join(VIS, dir, j.id), { recursive: true })
      writeFileSync(join(VIS, dir, j.id, name), buf)
    }
    record(j.id, 'visual', false, `${name}: ${size ?? `${(ratio * 100).toFixed(2)}% of pixels differ`} (tests/visual/diff/${j.id}/${name}; --update to accept)`)
    if (errors.length) record(j.id, 'visual', false, `${name}: page errors ${errors.join(' | ')}`)
  })
  console.log(`visual: ${jobs.length} screenshots${created ? `, ${created} baselines recorded` : ''}`)
}

// ---------- layout
if (suites.includes('layout')) {
  const jobs = charts.flatMap((c) => c.samples.flatMap((sample) => WIDTHS.map((width) => ({ id: c.id, suite: 'layout', sample, width, theme: 'light' }))))
  for (const c of charts) for (const sample of ['basic', 'realistic']) if (c.samples.includes(sample)) for (const theme of ['dark', 'high-contrast']) jobs.push({ id: c.id, suite: 'layout', sample, width: 768, theme })
  await pool(jobs, async ({ page, errors }, j) => {
    const r = await renderIn(page, { type: j.id, sample: j.sample, width: j.width, theme: j.theme, options: { animate: false } })
    const tag = `${j.sample} ${j.theme} ${j.width}`
    if (!r.ok) return record(j.id, 'layout', false, `${tag}: ${r.error}`)
    const f = await page.evaluate(layoutCheck, '#host')
    if (r.state === 'ok' && r.renderer === 'svg') {
      for (const which of [0, -1]) {
        const idx = which === 0 ? 0 : Math.max(0, r.marks - 1)
        const c = await page.evaluate(markCenter, ['#host', idx])
        if (!c) continue
        await page.mouse.move(c.x, c.y)
        f.push(...(await page.evaluate(tooltipCheck, '#host')).map((x) => `hover mark ${idx}: ${x}`))
        await page.mouse.move(1, 1)
      }
    }
    if (errors.length) f.push(`page errors: ${errors.join(' | ')}`)
    if (f.length) for (const x of f) record(j.id, 'layout', false, `${tag}: ${x}`)
    else record(j.id, 'layout', true)
  })
  console.log(`layout: ${jobs.length} renders checked`)
}

// ---------- a11y
if (suites.includes('a11y')) {
  const jobs = charts.flatMap((c) => c.samples.map((sample) => ({ id: c.id, suite: 'a11y', sample, theme: 'light', width: 768 })))
  for (const c of charts) for (const theme of ['dark', 'high-contrast']) if (c.samples.includes('basic')) jobs.push({ id: c.id, suite: 'a11y', sample: 'basic', theme, width: 768 })
  await pool(jobs, async ({ page }, j) => {
    const r = await renderIn(page, { type: j.id, sample: j.sample, width: j.width, theme: j.theme, options: { animate: false } })
    const tag = `${j.sample} ${j.theme}`
    if (!r.ok) return record(j.id, 'a11y', false, `${tag}: ${r.error}`)
    const f = []
    const axe = await page.evaluate(async () => {
      const res = await window.axe.run(document.getElementById('host'), { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'] } })
      return res.violations.map((v) => `${v.id} (${v.impact}): ${v.help} — ${v.nodes.slice(0, 2).map((n) => n.target.join(' ')).join('; ')}`)
    })
    f.push(...axe.map((x) => `axe ${x}`))
    const meta = await page.evaluate(() => {
      const h = window.__h, root = h.root
      const svg = root.querySelector('svg.oc-svg')
      const plate = root.querySelector('.oc-state')
      return { state: h.state, label: svg?.getAttribute('aria-label') ?? plate?.getAttribute('aria-label') ?? plate?.textContent ?? '', summary: h.summary(), role: svg?.getAttribute('role'), name: OmpCharts.listCharts().find((c) => c.id === h.type)?.name, title: '' }
    })
    if (meta.state === 'ok') {
      if (meta.role !== 'img') f.push('svg role is not img')
      if (!meta.label || meta.label !== meta.summary) f.push(`aria-label does not match the summary: "${meta.label}" vs "${meta.summary}"`)
      if (!meta.label.startsWith(meta.name)) f.push(`aria-label does not start with the chart name "${meta.name}": "${meta.label}"`)
      if (/undefined|NaN|\[object/.test(meta.label)) f.push(`aria-label contains junk: "${meta.label}"`)
      // keyboard traversal: every mark when there are few, ends + paging when many
      const n = r.marks
      await page.focus('#host svg.oc-svg')
      const announce = () => page.evaluate(() => document.querySelector('#host [aria-live]').textContent)
      const steps = n <= 400 ? n : 50
      for (let i = 0; i < steps; i++) {
        await page.keyboard.press('ArrowRight')
        if (n <= 400 || i < 3) {
          const t = await announce()
          if (!t.startsWith(`${i + 1} of ${n}: `) || t.length <= `${i + 1} of ${n}: `.length) { f.push(`keyboard: step ${i + 1} announced "${t}"`); break }
        }
      }
      if (n > 400) {
        await page.keyboard.press('End')
        if (!(await announce()).startsWith(`${n} of ${n}: `)) f.push('keyboard: End did not reach the last mark')
        await page.keyboard.press('Home')
        if (!(await announce()).startsWith(`1 of ${n}: `)) f.push('keyboard: Home did not reach the first mark')
      }
      const ring = await page.evaluate(() => document.querySelector('#host .oc-focus-ring')?.getAttribute('visibility'))
      if (ring !== 'visible') f.push('keyboard: focus ring not visible on the active mark')
      const table = await page.evaluate(() => document.querySelectorAll('#host table tbody tr').length)
      if (!table) f.push('no data table fallback')
    } else if (meta.state === 'empty' && !meta.label) f.push('empty state has no accessible name')
    if (f.length) for (const x of f) record(j.id, 'a11y', false, `${tag}: ${x}`)
    else record(j.id, 'a11y', true)
  })
  console.log(`a11y: ${jobs.length} renders audited`)
}

// ---------- perf
if (suites.includes('perf')) {
  const rows = []
  const p = await newPage(browser, url, { width: 1400, height: 1000 })
  for (const c of charts) {
    // warm the chart's code path once, then time first renders
    await renderIn(p.page, { type: c.id, sample: 'basic', width: 960, options: { animate: false } })
    for (const sample of ['basic', 'realistic', 'edge-large'].filter((s) => c.samples.includes(s))) {
      const r = await renderIn(p.page, { type: c.id, sample, width: 960, options: { animate: false } })
      if (!r.ok) { record(c.id, 'perf', false, `${sample}: ${r.error}`); continue }
      // large inputs (edge-large: >5k rows or marks) get the 500 ms budget whatever they draw
      const budget = r.renderer === 'canvas' || sample === 'edge-large' ? 500 : r.marks <= 1000 ? 100 : 250
      rows.push({ id: c.id, sample, marks: r.marks, renderer: r.renderer, ms: r.ms, budget })
      record(c.id, 'perf', r.ms <= budget, `${sample}: ${r.ms.toFixed(1)} ms > ${budget} ms (${r.marks} ${r.renderer} marks)`)
    }
  }
  for (const pc of perfCases().filter((x) => charts.some((c) => c.id === x.type))) {
    const r = await p.page.evaluate(({ type, n, gen }) => {
      const data = new Function('n', gen)(n)
      const app = document.getElementById('app')
      window.__h?.destroy()
      app.innerHTML = '<div id="host" style="width:960px"></div>'
      OmpCharts.chart(type, document.getElementById('host'), data.slice(0, 50), { animate: false }).destroy()
      const t0 = performance.now()
      const h = OmpCharts.chart(type, document.getElementById('host'), data, { animate: false })
      const ms = performance.now() - t0
      window.__h = h
      return { ms, marks: h.markCount(), renderer: h.renderer, state: h.state }
    }, { type: pc.type, n: pc.n, gen: pc.gen })
    const budget = pc.n >= 50000 ? 500 : 100
    rows.push({ id: pc.type, sample: `synthetic ${pc.n.toLocaleString('en-US')}`, marks: r.marks, renderer: r.renderer, ms: r.ms, budget })
    const okMarks = r.marks >= pc.n * 0.98
    record(pc.type, 'perf', r.ms <= budget && okMarks && (pc.n < 50000 || r.renderer === 'canvas'), `synthetic ${pc.n}: ${r.ms.toFixed(1)} ms (budget ${budget}), ${r.marks} ${r.renderer} marks, state ${r.state}`)
  }
  await p.ctx.close()
  const over = rows.filter((r) => r.ms > r.budget)
  const md = [
    '# Performance report',
    '',
    `Generated ${new Date().toISOString()} by \`npm run charts:perf\` — headless Chromium (Playwright ${JSON.parse(readFileSync(join(ROOT, 'node_modules', 'playwright-core', 'package.json'), 'utf8')).version}), 960 px wide, animation off, one warm-up render per chart. Time is \`chart()\` to return: validation, layout and painting the SVG/canvas.`,
    '',
    'Budgets: **< 100 ms** for up to 1,000 SVG marks; **< 500 ms** on the canvas path (above 5,000 marks, tested at 50,000) and for every `edge-large` input (>5k rows even when they reduce to few marks, e.g. a KDE over 16k values); 250 ms in between.',
    '',
    `Result: ${rows.length - over.length}/${rows.length} within budget.`,
    '',
    '| Chart | Input | Marks | Renderer | First render (ms) | Budget (ms) | |',
    '|---|---|---:|---|---:|---:|---|',
    ...rows.map((r) => `| ${r.id} | ${r.sample} | ${r.marks.toLocaleString('en-US')} | ${r.renderer} | ${r.ms.toFixed(1)} | ${r.budget} | ${r.ms <= r.budget ? 'ok' : '**over**'} |`),
    '',
  ].join('\n')
  writeFileSync(join(ROOT, 'tests', 'perf-report.md'), md)
  console.log(`perf: ${rows.length} measurements, ${over.length} over budget -> tests/perf-report.md`)
}

await browser.close()
mkdirSync(dirname(RESULTS), { recursive: true })
results.generated = new Date().toISOString()
writeFileSync(RESULTS, JSON.stringify(results, null, 2))
const secs = ((Date.now() - t0) / 1000).toFixed(0)
if (failures.length) {
  console.log(`\n${failures.length} failure(s) in ${secs}s:`)
  for (const f of failures.slice(0, 200)) console.log(`FAIL ${f}`)
  if (failures.length > 200) console.log(`… ${failures.length - 200} more in tests/.results/browser.json`)
  process.exit(1)
}
console.log(`\nbrowser suites (${suites.join(', ')}) passed for ${charts.length} chart(s) in ${secs}s`)
