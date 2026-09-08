// Checks the chat transcript's per-model colouring: tabs/chat/tags.ts against
// the tokens it names in styles/tokens.css.
//
// Nothing here is visible to `tsc`. `tagsFor` returns a Map<string, number>
// whether it hands two models the same slot or not, `tagStyle` returns a
// CSSProperties whether `--tag-9` exists or not, and a var name that is not
// defined renders as no colour at all -- silently, on a screen whose whole
// point is that the colour means something.
//
// Three classes of bug, one per section below:
//
//   1. TWO MODELS DRAWN THE SAME. The one thing the colour must never do. It
//      is a set-uniqueness property over a list, which is to say exactly the
//      kind of thing a type cannot state.
//   2. A COLOUR THAT MOVES. A turn is drawn once and lives in the log; if
//      appending an exchange could renumber the slots, the transcript would
//      recolour itself behind the reader.
//   3. A COLOUR NOBODY CAN SEPARATE. This one was found by screenshot after
//      the file already passed: the first palette put blue on slot 1 and teal
//      on slot 2, 29 degrees apart, so a two-model transcript was correctly
//      coloured and unreadable at a glance -- every assertion here passed and
//      the feature did not work. Hue separation is now a number this file
//      checks, in both themes, along with distance from the status ramp.
//   4. A TOKEN THAT IS NOT THERE, or is there and means something else. The
//      module writes `var(--tag-N)` as a string, and only this file ever
//      compares that string to what tokens.css actually defines -- in every
//      theme block, since a token defined in one and missed in another is a
//      screen that loses its colours when the OS flips to dark.
//
//   cd ui && node src/tabs/chat/tags.check.mjs
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { load, report } from '../../check/harness.mjs'

const T = await load(import.meta.url, './tags.ts')
const { check, note, done } = report()

const css = readFileSync(
  fileURLToPath(new URL('../../styles/tokens.css', import.meta.url)),
  'utf8',
)

// ── 1. two models are never drawn the same ───────────────────────────────────

// The style is what the eye actually receives, so uniqueness is asserted on
// the whole declaration and not on the slot number: two slots that differed
// numerically and rendered identically would pass a slot-wise test and fail
// the reader.
const draw = (slot) => JSON.stringify(T.tagStyle(slot))
const drawings = new Set(Array.from({ length: T.TAG_SLOTS }, (_, i) => draw(i)))
check(
  drawings.size === T.TAG_SLOTS,
  `all ${T.TAG_SLOTS} slots draw differently (${drawings.size} distinct declarations)`,
)

// The second four are the first four again, dashed. That is the whole reason
// there are eight identities from four hues, and it is what keeps them apart
// in a greyscale screenshot.
const solid = Array.from({ length: T.TAG_HUES }, (_, i) => T.tagStyle(i))
const dashed = Array.from({ length: T.TAG_HUES }, (_, i) => T.tagStyle(i + T.TAG_HUES))
check(
  solid.every((s) => s.borderLeftStyle === 'solid') &&
    dashed.every((s) => s.borderLeftStyle === 'dashed'),
  'the first four slots are solid and the second four dashed -- so the eight ' +
    'identities survive a greyscale screenshot',
)
check(
  solid.every((s, i) => s.borderLeftColor === dashed[i].borderLeftColor),
  'each dashed slot reuses its solid partner’s hue, rather than being a fifth colour',
)

const many = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']
const full = T.tagsFor(many)
check(
  new Set(full.values()).size === many.length,
  `eight models in one transcript get eight different slots (${new Set(full.values()).size})`,
)

// ── 2. a colour never moves under the reader ─────────────────────────────────

check(
  T.tagsFor([]).size === 0,
  'an empty transcript assigns nothing',
)

const order = T.tagsFor(['qwen', 'qwen', 'llama', 'qwen', 'gemma'])
check(
  order.get('qwen') === 0 && order.get('llama') === 1 && order.get('gemma') === 2,
  `slots go by first appearance, not by name (${JSON.stringify([...order])})`,
)

// The stability property, stated as the thing that actually happens: every
// prefix of a transcript agrees with the whole about every model it contains.
// This is what makes a turn's colour fixed at the moment it is drawn.
const grew = ['qwen', 'qwen', 'llama', 'qwen', 'gemma', 'llama', 'qwen']
const whole = T.tagsFor(grew)
let moved = null
for (let n = 1; n <= grew.length && moved === null; n++) {
  const prefix = T.tagsFor(grew.slice(0, n))
  for (const [name, slot] of prefix) {
    if (whole.get(name) !== slot) moved = `${name} was ${slot} at turn ${n}, ${whole.get(name)} at the end`
  }
}
check(moved === null, `no model's slot moves as the transcript grows (${moved ?? 'checked every prefix'})`)

// A turn with no destination -- one drawn before a model was ever picked --
// must not eat a slot, or the first real model would come out uncoloured for
// no reason the reader can see.
const withNulls = T.tagsFor([null, 'qwen', null, 'llama'])
check(
  withNulls.size === 2 && withNulls.get('qwen') === 0 && withNulls.get('llama') === 1,
  'a turn with no model is skipped rather than given a slot of its own',
)

// ── 2b. a ninth model is plain, never a repeat ───────────────────────────────

const nine = T.tagsFor(['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i'])
check(
  !nine.has('i') && nine.size === T.TAG_SLOTS,
  `a ninth model gets no slot rather than the first model's (map holds ${nine.size})`,
)
check(
  T.tagStyle(undefined) === undefined && T.tagInk(undefined) === 'var(--ink-muted)',
  'an unslotted model draws as an ordinary turn: no rule, no tint, muted label',
)

// ── 3+4. the tokens exist everywhere, and are separable from each other ──────

// Every theme block, by name. `:root` is light; the media query and the
// attribute selector are the two ways dark is reached, and a token defined in
// one of them and forgotten in the other is a screen that loses its colours
// on an OS setting.
const BLOCKS = [
  [':root {', 'light'],
  ["  :root:not([data-theme='light']) {", 'dark, by OS preference'],
  [":root[data-theme='dark'] {", 'dark, by explicit choice'],
]
const values = {}
for (const [open, label] of BLOCKS) {
  const at = css.indexOf(open)
  const body = at === -1 ? '' : css.slice(at + open.length, css.indexOf('\n}', at))
  const found = {}
  for (const m of body.matchAll(/--(tag-\d+):\s*(#[0-9A-Fa-f]{6})/g)) found[m[1]] = m[2]
  values[label] = found
  const wanted = Array.from({ length: T.TAG_HUES }, (_, i) => `tag-${i + 1}`)
  const missing = wanted.filter((n) => !(n in found))
  check(
    at !== -1 && missing.length === 0,
    `tokens.css defines every hue in the ${label} theme (${missing.length} missing${
      missing.length ? ': ' + missing.join(', ') : ''
    })`,
  )
}

// The module's own strings, resolved against what was just parsed. This is the
// join the two files have no other way to make: rename --tag-3 in the CSS and
// only this comparison notices.
const named = new Set()
for (let i = 0; i < T.TAG_SLOTS; i++) {
  named.add(T.tagStyle(i).borderLeftColor)
  named.add(T.tagInk(i))
}
const unresolved = [...named].filter((v) => {
  const name = /^var\(--(tag-\d+)\)$/.exec(v)
  return name === null || !(name[1] in values.light)
})
check(
  unresolved.length === 0,
  `every var() the module writes is a token tokens.css defines (${
    unresolved.length ? unresolved.join(', ') : [...named].join(', ')
  })`,
)
check(
  named.size === T.TAG_HUES,
  `the module names exactly the ${T.TAG_HUES} hues that exist, and the rule and ` +
    `the label read the same one (${named.size} distinct)`,
)

// Identity is not a verdict. A model coloured --fault is a model that looks
// broken, and this is the assertion that keeps the two ramps apart by value
// and not only by intention.
const statusOf = (label) => {
  const at = css.indexOf(BLOCKS.find(([, l]) => l === label)[0])
  const body = css.slice(at, css.indexOf('\n}', at))
  const out = {}
  for (const m of body.matchAll(/--(live|warn|fault|live-solid|warn-solid|fault-solid):\s*(#[0-9A-Fa-f]{6})/g))
    out[m[1]] = m[2].toUpperCase()
  return out
}

const lin = (c) => (c / 255 <= 0.04045 ? c / 255 / 12.92 : ((c / 255 + 0.055) / 1.055) ** 2.4)
const lum = (hex) => {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16))
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
}
const ratio = (a, b) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p)
  return (x + 0.05) / (y + 0.05)
}
const panelOf = (label) => {
  const at = css.indexOf(BLOCKS.find(([, l]) => l === label)[0])
  return /--panel:\s*(#[0-9A-Fa-f]{6})/.exec(css.slice(at, css.indexOf('\n}', at)))[1]
}

for (const label of ['light', 'dark, by explicit choice']) {
  const tags = Object.entries(values[label])
  const status = statusOf(label)
  const clash = tags.filter(([, v]) =>
    Object.values(status).includes(v.toUpperCase()),
  )
  check(
    clash.length === 0,
    `no identity hue is a status hue in ${label} -- a model must not be ` +
      `readable as a verdict (${clash.length ? clash.map(([k]) => k).join(', ') : 'none of ' + tags.length})`,
  )

  // They are the LABEL as well as the rule, so the bar is SC 1.4.3's 4.5:1 for
  // text, not 3:1 for a graphical mark. Measured against --panel; the block's
  // own 6% tint of the same hue moves the ground by less than the headroom
  // these carry, which is why they were tuned above 5:1 rather than at it.
  const panel = panelOf(label)
  const worst = tags
    .map(([k, v]) => [k, ratio(v, panel)])
    .sort((a, b) => a[1] - b[1])[0]
  check(
    worst[1] >= 4.5,
    `every identity hue is readable as text on --panel in ${label} ` +
      `(worst ${worst[0]} at ${worst[1].toFixed(2)}:1 on ${panel})`,
  )
  note(tags.map(([k, v]) => `${k} ${v} ${ratio(v, panel).toFixed(2)}:1`).join('  '))
}


// ── 3. the hues are far enough apart to be told apart ────────────────────────

// The check that would have caught the palette this file first shipped with.
// Contrast says each hue is readable against the page; it says nothing about
// whether two of them are readable against EACH OTHER, which is the only
// question the reader is actually asking. Hue angle is the crude measure and
// the crude measure is the one that failed.
const hueOf = (hex) => {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
  const max = Math.max(r, g, b)
  const d = max - Math.min(r, g, b)
  if (d === 0) return 0
  const h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4
  return h * 60
}
const apart = (a, b) => {
  const d = Math.abs(a - b)
  return Math.min(d, 360 - d)
}

// 40 degrees, not a rounder number: the palette that failed sat at 29 and the
// one that replaced it at 43, so the threshold is set between the version
// somebody could not read and the version they could. Raising it means
// repicking the ramp, which is the point -- it is not a knob to turn when a
// new colour will not fit.
const APART = 40

for (const label of ['light', 'dark, by explicit choice']) {
  const tags = Object.entries(values[label])
  let worst = null
  for (let i = 0; i < tags.length; i++) {
    for (let j = i + 1; j < tags.length; j++) {
      const gap = apart(hueOf(tags[i][1]), hueOf(tags[j][1]))
      if (worst === null || gap < worst.gap) worst = { gap, a: tags[i][0], b: tags[j][0] }
    }
  }
  check(
    worst !== null && worst.gap >= APART,
    `no two identity hues are within ${APART} degrees in ${label} -- two models ` +
      `must not merely BE different colours, they must look it ` +
      `(worst ${worst?.a}/${worst?.b} at ${worst?.gap.toFixed(0)} deg)`,
  )

  // Same measure, pointed at the other rule: --live, --warn and --fault are
  // what this palette must not be mistaken for. Value-inequality above is not
  // enough -- a magenta two degrees off --fault is a different value and the
  // same colour.
  const status = Object.entries(statusOf(label))
  let nearest = null
  for (const [tag, tv] of tags) {
    for (const [st, sv] of status) {
      const gap = apart(hueOf(tv), hueOf(sv))
      if (nearest === null || gap < nearest.gap) nearest = { gap, tag, st }
    }
  }
  check(
    nearest !== null && nearest.gap >= APART,
    `no identity hue is within ${APART} degrees of a status hue in ${label} ` +
      `(nearest ${nearest?.tag} to --${nearest?.st} at ${nearest?.gap.toFixed(0)} deg)`,
  )
}

done()
