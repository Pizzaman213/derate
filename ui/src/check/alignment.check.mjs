// requires: browser
//
// The alignment audit (check/alignment.mjs) against hand-built pages, so the
// rule the screens verifier enforces is proved to catch the bounce it was
// written for -- and to stay quiet on the layouts that only look like it --
// without a coordinator.

import { chromium } from 'playwright-core'
import { alignmentAudit } from './alignment.mjs'
import { findBrowser } from './browser.mjs'
import { report } from './harness.mjs'

const { check, done } = report()
const found = findBrowser()
if (!found.path) {
  console.error(`no browser: ${found.why}`)
  process.exit(1)
}
const engine = await chromium.launch({ executablePath: found.path })
const page = await engine.newPage({ viewport: { width: 900, height: 600 } })

const audit = async (body) => {
  await page.setContent(`<!doctype html><body style="font: 14px sans-serif">${body}</body>`)
  return page.evaluate(alignmentAudit)
}

const row = (inner, extra = '') =>
  `<div style="display:flex;align-items:center;gap:8px;${extra}">${inner}</div>`
const chips = (margin) =>
  `<span style="display:flex;gap:6px;margin:${margin}"><button style="font-size:11px">24h</button><button style="font-size:11px">7d</button></span>`
const select = `<label>Key <select style="padding:6px 12px"><option>all</option></select></label>`

// The /requests filter bar as it shipped: the chip group's bottom margin is
// centred with it, so its buttons hang above the selects. The line's median is
// the reference, so with more selects than chips it is the chips that are named.
let got = await audit(row(chips('0 0 10px') + select + select + select))
check(got.length === 2 && got.every((f) => f.includes('above')), `a margin-hung chip group is flagged, both chips (${got.join('; ')})`)
check(await audit(row(chips('0') + select)).then((f) => f.length === 0), 'the same row with the margin cleared is clean')

// Smaller controls are fine when they are centred: the rule is the centre
// line, not equal heights.
got = await audit(row(`<button style="font-size:10px">a</button><select style="padding:12px"><option>b</option></select>`))
check(got.length === 0, `controls of different heights centred on one line are clean (${got.length})`)

// A top-aligned row of unequal controls is the bounce too.
got = await audit(
  row(`<button style="font-size:10px">a</button><select style="padding:12px"><option>b</option></select>`).replace(
    'align-items:center',
    'align-items:flex-start',
  ),
)
check(got.length > 0, 'unequal controls top-aligned in a row are flagged')

// A wrapping row is judged line by line: the second line's controls are not
// compared with the first's.
got = await audit(
  row(
    `<button style="width:420px">one</button><button style="width:420px">two</button>` +
      `<select style="padding:14px"><option>x</option></select><button>y</button>`,
    'flex-wrap:wrap;width:860px',
  ),
)
check(got.length === 0, `each wrapped line of a row is its own line (${got.length})`)

// An item whose controls stack is a column, not a line.
got = await audit(row(`<div><button>up</button><br><button>down</button></div><button>side</button>`))
check(got.length === 0, `a stacked item inside a row is not one line (${got.length})`)

// Checkboxes align to their label's text; they are not controls here.
got = await audit(row(`<label><input type="checkbox"> only</label>` + select))
check(got.length === 0, `a checkbox beside a select is not judged (${got.length})`)

// Tables: a two-line cell centred by the UA lifts its first line off the row.
const table = (align) =>
  `<table><tr style="vertical-align:${align}"><td style="font-size:11px">11/18/2026</td><td>qwen</td>` +
  `<td style="font-family:monospace">502<div style="font-size:11px">upstream_timeout</div></td></tr></table>`
got = await audit(table('middle'))
check(got.length === 1 && got[0].includes('502'), `a two-line cell centred in its row is flagged (${got.join('; ')})`)
got = await audit(table('baseline'))
check(got.length === 0, `the same row baseline-aligned is clean, mixed fonts and all (${got.join('; ')})`)

await engine.close()
done()
