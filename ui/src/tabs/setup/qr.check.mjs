// Verifier for the QR encoder. There is no test runner in this repo (see
// layout.check.mjs), so this is the whole gate:
//
//   node src/tabs/setup/qr.check.mjs
//
// What it guards: qr.ts is ~200 lines of transcribed specification -- block
// tables, alignment centres, two BCH codes, eight mask patterns and four
// penalty rules. Every one of those is a place a digit can be wrong in a way
// that types cannot see and a human cannot eyeball, because the output is a
// field of squares.
//
// **This started out comparing matrices with `segno` module-for-module, and
// that test was wrong.** It failed on symbols that scan perfectly. The cause is
// worth recording, because the same trap is waiting for anyone who reinstates
// it: after the terminator, the spec pads to the byte boundary and then appends
// 0xEC/0x11 alternately, and segno emits one extra 0x00 codeword before it
// starts. Both symbols carry the same message and both decode; they simply
// disagree about bytes a decoder never looks at, because the terminator has
// already ended the message. Module equality was asserting that two encoders
// made the same arbitrary choice, not that either was correct.
//
// So the property under test is the one that matters: **the symbol decodes to
// the string that went in.** That is checked two ways, and the second is what
// makes it more than a self-consistency check:
//
//   1. our encoder -> this file's decoder -> the original string
//   2. SEGNO's matrices -> this file's decoder -> the original string
//
// (2) is the independence. The decoder below re-states the placement walk, the
// mask patterns and the format block from the specification, separately from
// qr.ts. If it can read symbols produced by an unrelated implementation, then
// that shared reading of the spec is right; if qr.ts and the decoder were both
// wrong in the same way, segno's symbols would not read. Neither file can
// launder a mistake past the other.
//
// Reed-Solomon is checked directly instead: every block's syndromes must be
// zero, which is the definition of a valid codeword and is not something a
// decoder that ignores parity would notice.

import { build as bundleWithEsbuild } from 'esbuild'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { cases } from './qr.fixtures.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const out = mkdtempSync(join(tmpdir(), 'qr-check-'))
const bundle = join(out, 'qr.mjs')

// esbuild's JS API rather than the launcher under node_modules/.bin: that shim
// is a POSIX script with no .cmd twin, so spawning it by path fails on Windows.
// router.check.mjs and rows.check.mjs bundle the same way.
await bundleWithEsbuild({
  entryPoints: [join(here, 'qr.ts')],
  bundle: true,
  format: 'esm',
  outfile: bundle,
  logLevel: 'warning',
})

const { encodeQr, qrPath, QrTooLongError } = await import(pathToFileURL(bundle).href)

let failures = 0
const check = (label, got, want) => {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    failures++
    console.error(`FAIL ${label}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`)
  }
}

// ── A decoder, stated independently of qr.ts ────────────────────────────────
// Deliberately a second copy of the tables rather than an import. If it shared
// qr.ts's constants, a wrong alignment centre would move the function-pattern
// map in both at once and the walk would still line up with itself. Duplication
// is the mechanism here, not an oversight.

const DEC_EC_M = {
  1: [10, [[1, 16]]],
  2: [16, [[1, 28]]],
  3: [26, [[1, 44]]],
  4: [18, [[2, 32]]],
  5: [24, [[2, 43]]],
  6: [16, [[4, 27]]],
  7: [18, [[4, 31]]],
  8: [22, [[2, 38], [2, 39]]],
  9: [22, [[3, 36], [2, 37]]],
  10: [26, [[4, 43], [1, 44]]],
}
const DEC_ALIGN = {
  1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
  6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}
const DEC_MASKS = [
  (x, y) => (x + y) % 2 === 0,
  (_x, y) => y % 2 === 0,
  (x) => x % 3 === 0,
  (x, y) => (x + y) % 3 === 0,
  (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0,
  (x, y) => ((x * y) % 2) + ((x * y) % 3) === 0,
  (x, y) => (((x * y) % 2) + ((x * y) % 3)) % 2 === 0,
  (x, y) => (((x + y) % 2) + ((x * y) % 3)) % 2 === 0,
]

/** Which cells belong to function patterns, from the spec, for this version. */
function functionMap(size, version) {
  const fn = Array.from({ length: size }, () => new Array(size).fill(false))
  const box = (x0, y0, w, h) => {
    for (let y = y0; y < y0 + h; y++)
      for (let x = x0; x < x0 + w; x++)
        if (x >= 0 && x < size && y >= 0 && y < size) fn[y][x] = true
  }
  box(0, 0, 8, 8)
  box(size - 8, 0, 8, 8)
  box(0, size - 8, 8, 8)
  for (let i = 0; i < size; i++) {
    fn[6][i] = true
    fn[i][6] = true
  }
  for (let i = 0; i < 9; i++) {
    fn[8][i] = true
    fn[i][8] = true
  }
  for (let i = 0; i < 8; i++) {
    fn[8][size - 1 - i] = true
    fn[size - 1 - i][8] = true
  }
  const centres = DEC_ALIGN[version]
  for (const row of centres)
    for (const col of centres) {
      const onFinder =
        (row <= 8 && col <= 8) ||
        (row <= 8 && col >= size - 9) ||
        (row >= size - 9 && col <= 8)
      if (!onFinder) box(col - 2, row - 2, 5, 5)
    }
  if (version >= 7) {
    box(size - 11, 0, 3, 6)
    box(0, size - 11, 6, 3)
  }
  return fn
}

/** Read the mask number back out of the format block. */
function readMask(grid) {
  // The five data bits sit at the top of the 15-bit block; unmask with the
  // spec's constant and take bits 0..2.
  let bits = 0
  for (let i = 0; i < 15; i++) {
    let on
    if (i < 6) on = grid[i][8]
    else if (i === 6) on = grid[7][8]
    else if (i === 7) on = grid[8][8]
    else if (i === 8) on = grid[8][7]
    else on = grid[8][14 - i]
    if (on) bits |= 1 << i
  }
  const unmasked = (bits ^ 0b101010000010010) >> 10
  return unmasked & 0b111
}

/** Matrix in, the codeword stream out, de-interleaved back into message order. */
function readCodewords(grid, version) {
  const size = grid.length
  const fn = functionMap(size, version)
  const mask = DEC_MASKS[readMask(grid)]

  const bits = []
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right--
    const upward = ((right + 1) & 2) === 0
    for (let vert = 0; vert < size; vert++) {
      const y = upward ? size - 1 - vert : vert
      for (const x of [right, right - 1]) {
        if (fn[y][x]) continue
        bits.push((grid[y][x] !== mask(x, y)) === true ? 1 : 0)
      }
    }
  }

  const all = []
  for (let i = 0; i + 8 <= bits.length; i += 8) {
    let byte = 0
    for (let j = 0; j < 8; j++) byte = (byte << 1) | bits[i + j]
    all.push(byte)
  }

  // Undo the interleave: rebuild each block's data run, then read them in order.
  const [ecPerBlock, groups] = DEC_EC_M[version]
  const sizes = []
  for (const [count, per] of groups) for (let i = 0; i < count; i++) sizes.push(per)
  const blocks = sizes.map(() => [])
  let cursor = 0
  const longest = Math.max(...sizes)
  for (let i = 0; i < longest; i++) {
    for (let b = 0; b < sizes.length; b++) {
      if (i < sizes[b]) blocks[b].push(all[cursor++])
    }
  }
  const parity = sizes.map(() => [])
  for (let i = 0; i < ecPerBlock; i++) {
    for (let b = 0; b < sizes.length; b++) parity[b].push(all[cursor++])
  }
  return { data: blocks.flat(), blocks, parity }
}

/** The message, parsed out of the data codewords. Byte mode only, which is all
 *  this encoder emits. */
function decode(grid, version) {
  const { data } = readCodewords(grid, version)
  let bit = 0
  const take = (n) => {
    let v = 0
    for (let i = 0; i < n; i++, bit++) {
      v = (v << 1) | ((data[bit >> 3] >> (7 - (bit & 7))) & 1)
    }
    return v
  }
  const mode = take(4)
  if (mode !== 0b0100) throw new Error(`mode ${mode.toString(2)} is not byte mode`)
  const count = take(version <= 9 ? 8 : 16)
  const bytes = []
  for (let i = 0; i < count; i++) bytes.push(take(8))
  return new TextDecoder().decode(Uint8Array.from(bytes))
}

// ── Reed-Solomon, checked directly ──────────────────────────────────────────

const EXP = new Uint8Array(512)
const LOG = new Uint8Array(256)
{
  let x = 1
  for (let i = 0; i < 255; i++) {
    EXP[i] = x
    LOG[x] = i
    x <<= 1
    if (x & 0x100) x ^= 0x11d
  }
  for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255]
}
const gmul = (a, b) => (a === 0 || b === 0 ? 0 : EXP[LOG[a] + LOG[b]])

/** A codeword is valid exactly when every syndrome is zero. Nothing about the
 *  message can tell you this; only the parity can. */
function syndromesZero(block, parityBlock, ecCount) {
  const full = [...block, ...parityBlock]
  for (let i = 0; i < ecCount; i++) {
    let acc = 0
    for (const byte of full) acc = gmul(acc, EXP[i]) ^ byte
    if (acc !== 0) return false
  }
  return true
}

// ── 1. Our encoder round trips ──────────────────────────────────────────────

for (const c of cases) {
  let code
  try {
    code = encodeQr(c.text)
  } catch (err) {
    failures++
    console.error(`FAIL ${c.label}: encode threw ${err.message}`)
    continue
  }
  check(`${c.label}: version`, code.version, c.version)
  check(`${c.label}: size`, code.size, 17 + code.version * 4)
  try {
    check(`${c.label}: round trips`, decode(code.modules, code.version), c.text)
  } catch (err) {
    failures++
    console.error(`FAIL ${c.label}: decode threw ${err.message}`)
  }
}

// ── 2. And reads an independent encoder's symbols ───────────────────────────

for (const c of cases) {
  const grid = c.matrix.map((row) => Array.from(row, (ch) => ch === '1'))
  try {
    check(`${c.label}: segno's symbol decodes`, decode(grid, c.version), c.text)
  } catch (err) {
    failures++
    console.error(`FAIL ${c.label}: decoding segno threw ${err.message}`)
  }
}

// ── 3. The parity is real ───────────────────────────────────────────────────

for (const c of cases) {
  const code = encodeQr(c.text)
  const { blocks, parity } = readCodewords(code.modules, code.version)
  const [ecPerBlock] = DEC_EC_M[code.version]
  const bad = blocks.filter((b, i) => !syndromesZero(b, parity[i], ecPerBlock)).length
  check(`${c.label}: all ${blocks.length} block(s) have valid parity`, bad, 0)
}

// ── 4. Properties no fixture states ─────────────────────────────────────────

check('deterministic', encodeQr('http://x/v1').modules, encodeQr('http://x/v1').modules)

const one = encodeQr('x')
check('version 1 is 21 modules', one.size, 21)
check('no quiet zone included', one.modules.length, one.size)
for (const [y, x] of [[0, 0], [0, one.size - 7], [one.size - 7, 0]]) {
  check(`finder at ${y},${x}`, one.modules[y][x], true)
  check(`finder centre ${y + 3},${x + 3}`, one.modules[y + 3][x + 3], true)
  check(`finder ring ${y + 1},${x + 1}`, one.modules[y + 1][x + 1], false)
}

// Too long is an error, not a truncation. A QR that decodes to half an address
// fails after somebody has already walked over with their phone.
let threw = null
try {
  encodeQr('x'.repeat(214))
} catch (err) {
  threw = err
}
check('214 bytes throws', threw instanceof QrTooLongError, true)
check('213 bytes does not', encodeQr('x'.repeat(213)).version, 10)

// The path is what actually renders, so an error here is visible even when the
// matrix behind it is right.
const code = encodeQr('http://192.168.0.71:8088/v1')
const covered = new Set()
for (const [, xs, ys, run] of qrPath(code).matchAll(/M(\d+) (\d+)h(\d+)v1h-\d+z/g)) {
  for (let i = 0; i < Number(run); i++) covered.add(`${Number(xs) + i},${ys}`)
}
let dark = 0
for (let y = 0; y < code.size; y++)
  for (let x = 0; x < code.size; x++)
    if (code.modules[y][x]) {
      dark++
      if (!covered.has(`${x},${y}`)) {
        failures++
        console.error(`FAIL path misses dark module ${x},${y}`)
      }
    }
check('path covers no light modules', covered.size, dark)

rmSync(out, { recursive: true, force: true })

if (failures) {
  console.error(`\n${failures} check(s) failed`)
  process.exit(1)
}
console.log(
  `qr.check: ${cases.length} symbols round trip, ${cases.length} of segno's decode, ` +
    `parity verified, plus properties. OK`,
)
