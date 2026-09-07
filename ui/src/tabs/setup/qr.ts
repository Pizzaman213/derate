// A QR encoder, because the alternative is a CDN and this product's premise is
// a machine that never needs one.
//
// The setup flow ends by handing you the endpoint. Typing `http://192.168.0.71:
// 8088/v1` into a phone by hand is the exact friction the flow exists to
// remove, so the last screen shows a symbol you point a camera at. Every way of
// getting one that is not this file fails on the machine we care most about:
//   - a CDN script is a network call, and a derate cluster is routinely on a
//     LAN with no route out. It would fail on exactly the air-gapped install
//     that has the least patience for a broken screen;
//   - an npm dependency is 50 KB and a supply-chain surface for something that
//     is ~200 lines of finite, specified arithmetic;
//   - rendering it server-side needs the same arithmetic in Python plus a new
//     entry in requirements.txt.
//
// So: byte mode, error correction level M, versions 1 to 10. That covers 213
// bytes, and the longest address this can be asked for is an IPv6 literal with
// a port and a path, which is nowhere near it. Anything longer throws and the
// caller shows the URL as text -- a QR is an affordance, never the only way to
// read the address.
//
// Verified against `segno` by `qr.check.mjs`: full matrix equality, every
// version boundary, not a spot check. The tables below are transcribed from
// ISO/IEC 18004 and any transcription error changes a matrix, so the checker is
// what makes them trustworthy rather than plausible.

/** Data codewords and per-block structure at level M, indexed by version.
 *  `[ecPerBlock, [blockCount, dataPerBlock], ...]` -- two groups where the
 *  spec has two, one where it has one. */
const EC_M: Record<number, [number, [number, number][]]> = {
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

/** Row/column centres of the alignment patterns. Every pairing is used except
 *  the three that collide with a finder. */
const ALIGN: Record<number, number[]> = {
  1: [],
  2: [6, 18],
  3: [6, 22],
  4: [6, 26],
  5: [6, 30],
  6: [6, 34],
  7: [6, 22, 38],
  8: [6, 24, 42],
  9: [6, 26, 46],
  10: [6, 28, 50],
}

/** The 18-bit version block, versions 7 and up. Golay (18,6), and tabled
 *  rather than computed: four constants are cheaper to check than a second
 *  BCH routine to get wrong. */
const VERSION_BITS: Record<number, number> = {
  7: 0x07c94,
  8: 0x085bc,
  9: 0x09a99,
  10: 0x0a4d3,
}

// `noUncheckedIndexedAccess` is on for this project, so every `a[i]` is
// `T | undefined`. That is the right default for data off the wire and pure
// noise in a numeric kernel, where each index is in range by construction --
// the bounds are the loop bounds two lines above. Rather than scatter sixty
// non-null assertions through the arithmetic, every indexed read goes through
// `at`, which is the one place the claim is made and the one place to look if
// it is ever wrong.
function at<T>(list: { readonly [i: number]: T }, index: number): T {
  return list[index] as T
}

// ── GF(256), the field Reed-Solomon works in ────────────────────────────────
// Generated once at module load rather than tabled: 512 bytes of derived data
// that a typo in a literal table would corrupt invisibly.

const EXP = new Uint8Array(512)
const LOG = new Uint8Array(256)
{
  let x = 1
  for (let i = 0; i < 255; i++) {
    EXP[i] = x
    LOG[x] = i
    x <<= 1
    if (x & 0x100) x ^= 0x11d // the primitive polynomial the spec names
  }
  for (let i = 255; i < 512; i++) EXP[i] = at(EXP, i - 255)
}

function mul(a: number, b: number): number {
  if (a === 0 || b === 0) return 0
  return at(EXP, at(LOG, a) + at(LOG, b))
}

/** The generator polynomial for `degree` error-correction codewords. */
function generator(degree: number): number[] {
  let poly = [1]
  for (let i = 0; i < degree; i++) {
    const next = new Array(poly.length + 1).fill(0)
    for (let j = 0; j < poly.length; j++) {
      next[j] ^= at(poly, j)
      next[j + 1] ^= mul(at(poly, j), at(EXP, i))
    }
    poly = next
  }
  return poly
}

/** `count` error-correction codewords for one block. */
function ecCodewords(data: number[], count: number): number[] {
  const gen = generator(count)
  const out = new Array(count).fill(0)
  for (const byte of data) {
    const factor = byte ^ at<number>(out, 0)
    out.shift()
    out.push(0)
    if (factor !== 0) {
      for (let i = 0; i < gen.length - 1; i++) {
        out[i] ^= mul(at(gen, i + 1), factor)
      }
    }
  }
  return out
}

// ── Bit stream ──────────────────────────────────────────────────────────────

class Bits {
  readonly bits: number[] = []

  push(value: number, length: number): void {
    for (let i = length - 1; i >= 0; i--) this.bits.push((value >>> i) & 1)
  }

  get length(): number {
    return this.bits.length
  }
}

// ── Public shape ────────────────────────────────────────────────────────────

export interface QrCode {
  /** Modules per side, quiet zone NOT included. The caller decides the border,
   *  because the sensible border differs between an SVG in a card and a print. */
  size: number
  /** `modules[y][x]`, true where the module is dark. */
  modules: boolean[][]
  version: number
}

export class QrTooLongError extends Error {
  constructor(bytes: number) {
    super(
      `${bytes} bytes is more than a version-10 QR code holds at error ` +
        `correction level M (213). Show the address as text instead.`,
    )
    this.name = 'QrTooLongError'
  }
}

/** Encode `text` as a QR code. UTF-8, byte mode, level M.
 *
 *  Throws `QrTooLongError` rather than silently truncating or dropping to a
 *  lower error-correction level: a QR that decodes to half an address is worse
 *  than no QR, because it fails after the person has already walked over with
 *  their phone.
 *
 *  `forcedMask` exists for `qr.check.mjs` and nothing else. Pinning the mask is
 *  what lets the verifier compare against another implementation mask for mask,
 *  separating "the modules are placed right" from "the best mask was chosen" --
 *  two independent ways to be wrong that a single comparison conflates. */
export function encodeQr(text: string, forcedMask?: number): QrCode {
  const data = Array.from(new TextEncoder().encode(text))

  let version = 0
  for (let v = 1; v <= 10; v++) {
    const entry = at(EC_M, v)
    const groups = entry[1]
    const capacity = groups.reduce((n, [blocks, per]) => n + blocks * per, 0)
    // 4 mode bits + the character-count field + the payload, in bytes.
    const countBits = v <= 9 ? 8 : 16
    if (Math.ceil((4 + countBits) / 8) + data.length <= capacity) {
      version = v
      break
    }
  }
  if (version === 0) throw new QrTooLongError(data.length)

  const [ecPerBlock, groups] = at(EC_M, version)
  const totalData = groups.reduce((n, [blocks, per]) => n + blocks * per, 0)

  // -- the bit stream --------------------------------------------------------
  const bits = new Bits()
  bits.push(0b0100, 4) // byte mode
  bits.push(data.length, version <= 9 ? 8 : 16)
  for (const byte of data) bits.push(byte, 8)

  // Terminator, up to four bits, then pad to a byte boundary.
  const capacityBits = totalData * 8
  bits.push(0, Math.min(4, capacityBits - bits.length))
  while (bits.length % 8 !== 0) bits.bits.push(0)

  // Then the two alternating pad codewords the spec names, forever.
  const codewords: number[] = []
  for (let i = 0; i < bits.length; i += 8) {
    let byte = 0
    for (let j = 0; j < 8; j++) byte = (byte << 1) | at(bits.bits, i + j)
    codewords.push(byte)
  }
  // The pad sequence starts at 0xEC and alternates from there. Keyed on its own
  // position, NOT on `codewords.length`: how many codewords the payload already
  // filled has nothing to do with which pad byte comes first, and tying the two
  // together puts 0x11 first whenever that count happens to be odd.
  const PAD = [0xec, 0x11]
  for (let pad = 0; codewords.length < totalData; pad++) {
    codewords.push(at(PAD, pad % 2))
  }

  // -- blocks, and their error correction ------------------------------------
  const dataBlocks: number[][] = []
  const ecBlocks: number[][] = []
  let cursor = 0
  for (const [blockCount, perBlock] of groups) {
    for (let i = 0; i < blockCount; i++) {
      const block = codewords.slice(cursor, cursor + perBlock)
      cursor += perBlock
      dataBlocks.push(block)
      ecBlocks.push(ecCodewords(block, ecPerBlock))
    }
  }

  // Interleaved: one codeword from each block in turn, so a burst of damage is
  // spread across blocks instead of destroying one of them.
  const final: number[] = []
  const longestData = Math.max(...dataBlocks.map((b) => b.length))
  for (let i = 0; i < longestData; i++) {
    for (const block of dataBlocks) if (i < block.length) final.push(at(block, i))
  }
  for (let i = 0; i < ecPerBlock; i++) {
    for (const block of ecBlocks) final.push(at(block, i))
  }

  return { size: 17 + version * 4, modules: draw(version, final, forcedMask), version }
}

// ── The matrix ──────────────────────────────────────────────────────────────

/** A square of modules, flat. Three states, because placement needs to know
 *  which cells the function patterns already own: -1 unset, 0 light, 1 dark. */
class Matrix {
  readonly size: number
  private readonly cells: Int8Array

  constructor(size: number) {
    this.size = size
    this.cells = new Int8Array(size * size).fill(-1)
  }

  get(x: number, y: number): number {
    return at(this.cells, y * this.size + x)
  }

  set(x: number, y: number, dark: boolean): void {
    this.cells[y * this.size + x] = dark ? 1 : 0
  }

  unset(x: number, y: number): boolean {
    return this.get(x, y) === -1
  }

  /** Every unset cell resolved to light, as a plain boolean grid. */
  toBooleans(): boolean[][] {
    const out: boolean[][] = []
    for (let y = 0; y < this.size; y++) {
      const row: boolean[] = []
      for (let x = 0; x < this.size; x++) row.push(this.get(x, y) === 1)
      out.push(row)
    }
    return out
  }

  clone(): Matrix {
    const copy = new Matrix(this.size)
    copy.cells.set(this.cells)
    return copy
  }
}

/** The eight mask patterns. Each says whether the module at (x, y) flips. */
const MASKS: readonly ((x: number, y: number) => boolean)[] = [
  (x, y) => (x + y) % 2 === 0,
  (_x, y) => y % 2 === 0,
  (x) => x % 3 === 0,
  (x, y) => (x + y) % 3 === 0,
  (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0,
  (x, y) => ((x * y) % 2) + ((x * y) % 3) === 0,
  (x, y) => (((x * y) % 2) + ((x * y) % 3)) % 2 === 0,
  (x, y) => (((x + y) % 2) + ((x * y) % 3)) % 2 === 0,
]

function draw(version: number, codewords: number[], forcedMask?: number): boolean[][] {
  const size = 17 + version * 4
  const grid = new Matrix(size)

  const finder = (col: number, row: number) => {
    for (let r = -1; r <= 7; r++) {
      for (let c = -1; c <= 7; c++) {
        const y = row + r
        const x = col + c
        if (y < 0 || y >= size || x < 0 || x >= size) continue
        const ring = Math.max(Math.abs(r - 3), Math.abs(c - 3))
        // The separator (the -1/7 ring) is light; then dark, light, dark.
        grid.set(x, y, r >= 0 && r <= 6 && c >= 0 && c <= 6 && ring !== 2)
      }
    }
  }
  finder(0, 0)
  finder(size - 7, 0)
  finder(0, size - 7)

  // Timing: alternating, along the sixth row and the sixth column.
  for (let i = 8; i < size - 8; i++) {
    grid.set(i, 6, i % 2 === 0)
    grid.set(6, i, i % 2 === 0)
  }

  // Alignment patterns at every pairing of centres, except the three corners a
  // finder already occupies.
  //
  // Those three are named by position, NOT detected by asking whether the cell
  // is already written. From version 7 the timing pattern reaches the second
  // centre, so "something is here already" is also true of a perfectly ordinary
  // alignment pattern that happens to cross the timing row -- which it is
  // supposed to be drawn over. Testing for occupancy silently dropped two
  // patterns per symbol from version 7 up, and dropped them from the
  // function-module map as well, so the data walk then wrote message bits
  // through the hole. Versions 1 to 6 were unaffected and looked like proof.
  const centres = at(ALIGN, version)
  const last = centres.length > 0 ? at(centres, centres.length - 1) : 0
  for (const row of centres) {
    for (const col of centres) {
      const onFinder =
        (row === 6 && col === 6) ||
        (row === 6 && col === last) ||
        (row === last && col === 6)
      if (onFinder) continue
      for (let r = -2; r <= 2; r++) {
        for (let c = -2; c <= 2; c++) {
          grid.set(col + c, row + r, Math.max(Math.abs(r), Math.abs(c)) !== 1)
        }
      }
    }
  }

  // The one module that is always dark and has no other job.
  grid.set(8, size - 8, true)

  // The version block, versions 7 and up, in both of its copies.
  if (version >= 7) {
    const bits = at(VERSION_BITS, version)
    for (let i = 0; i < 18; i++) {
      const on = ((bits >> i) & 1) === 1
      const a = size - 11 + (i % 3)
      const b = Math.floor(i / 3)
      grid.set(a, b, on)
      grid.set(b, a, on)
    }
  }

  // Reserve the format areas so data placement steps over them. Written light
  // for now; the real bits go in per mask, after the data is placed.
  for (let i = 0; i < 9; i++) {
    if (grid.unset(8, i)) grid.set(8, i, false)
    if (grid.unset(i, 8)) grid.set(i, 8, false)
  }
  for (let i = 0; i < 8; i++) {
    if (grid.unset(8, size - 1 - i)) grid.set(8, size - 1 - i, false)
    if (grid.unset(size - 1 - i, 8)) grid.set(size - 1 - i, 8, false)
  }

  // Everything set so far is a function pattern, and masking must leave it be.
  const reserved: boolean[] = []
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) reserved.push(!grid.unset(x, y))
  }
  const isFunction = (x: number, y: number) => at(reserved, y * size + x)

  // -- data, up the right edge in a two-column zigzag -------------------------
  let bit = 0
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right-- // the vertical timing column is not a data column
    // Which way this column pair runs is a function of where it is, not of how
    // many pairs have been drawn -- the timing column shifts the sequence by
    // one and a running toggle would then be upside down for the rest.
    const upward = ((right + 1) & 2) === 0
    for (let vert = 0; vert < size; vert++) {
      const y = upward ? size - 1 - vert : vert
      for (const x of [right, right - 1]) {
        if (isFunction(x, y)) continue
        const byte = codewords[bit >> 3]
        // Past the end of the stream the remainder bits are light, which is
        // what the spec's remainder-bit rule amounts to.
        grid.set(x, y, byte !== undefined && ((byte >> (7 - (bit & 7))) & 1) === 1)
        bit++
      }
    }
  }

  // -- masking ---------------------------------------------------------------
  const candidates = forcedMask === undefined ? [0, 1, 2, 3, 4, 5, 6, 7] : [forcedMask]
  let best: boolean[][] | null = null
  let bestPenalty = Infinity
  for (const mask of candidates) {
    const candidate = grid.clone()
    const flips = at(MASKS, mask)
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        if (!isFunction(x, y) && flips(x, y)) {
          candidate.set(x, y, candidate.get(x, y) !== 1)
        }
      }
    }
    writeFormat(candidate, mask)
    const grid2 = candidate.toBooleans()
    const penalty = score(grid2)
    if (penalty < bestPenalty) {
      bestPenalty = penalty
      best = grid2
    }
  }
  return best as boolean[][]
}

/** The 15-bit format block: two error-correction bits, three mask bits, BCH
 *  (15,5), then XOR with the spec's fixed mask so it is never all light. */
function writeFormat(grid: Matrix, mask: number): void {
  const size = grid.size
  const data = (0b00 << 3) | mask // 00 = error correction level M
  let rem = data
  for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >> 9) * 0b10100110111)
  const bits = (((data << 10) | rem) ^ 0b101010000010010) & 0x7fff

  for (let i = 0; i < 15; i++) {
    const on = ((bits >> i) & 1) === 1
    // Copy one wraps the top-left finder: down column 8, then left along row 8.
    // The two cells the timing patterns own are stepped over, which is why this
    // is a ladder of cases and not two loops.
    if (i < 6) grid.set(8, i, on)
    else if (i === 6) grid.set(8, 7, on)
    else if (i === 7) grid.set(8, 8, on)
    else if (i === 8) grid.set(7, 8, on)
    else grid.set(14 - i, 8, on)
    // Copy two is split: the low bits run right-to-left along row 8 beside the
    // top-right finder, the high bits run up column 8 from the bottom-left one.
    if (i < 8) grid.set(size - 1 - i, 8, on)
    else grid.set(8, size - 15 + i, on)
  }
  grid.set(8, size - 8, true) // the always-dark module, restated after masking
}

/** The spec's four penalty rules. Lower is better; the mask with the lowest
 *  total is the one that ships. */
function score(grid: boolean[][]): number {
  const size = grid.length
  const cell = (x: number, y: number): boolean => at(at(grid, y), x)
  let penalty = 0

  // 1: runs of five or more of one colour, each direction.
  for (let i = 0; i < size; i++) {
    for (const line of [
      (j: number) => cell(j, i),
      (j: number) => cell(i, j),
    ]) {
      let run = 1
      for (let j = 1; j < size; j++) {
        if (line(j) === line(j - 1)) {
          run++
          if (run === 5) penalty += 3
          else if (run > 5) penalty += 1
        } else run = 1
      }
    }
  }

  // 2: every 2x2 block of one colour.
  for (let y = 0; y < size - 1; y++) {
    for (let x = 0; x < size - 1; x++) {
      const v = cell(x, y)
      if (v === cell(x + 1, y) && v === cell(x, y + 1) && v === cell(x + 1, y + 1)) {
        penalty += 3
      }
    }
  }

  // 3: the finder-lookalike pattern, with its light run on either side.
  const A = [true, false, true, true, true, false, true, false, false, false, false]
  const B = [false, false, false, false, true, false, true, true, true, false, true]
  for (let i = 0; i < size; i++) {
    for (const line of [
      (j: number) => cell(j, i),
      (j: number) => cell(i, j),
    ]) {
      for (let j = 0; j + 11 <= size; j++) {
        let matchA = true
        let matchB = true
        for (let k = 0; k < 11; k++) {
          const v = line(j + k)
          if (v !== at(A, k)) matchA = false
          if (v !== at(B, k)) matchB = false
        }
        if (matchA || matchB) penalty += 40
      }
    }
  }

  // 4: how far the dark proportion strays from half.
  let dark = 0
  for (const row of grid) for (const v of row) if (v) dark++
  const percent = (dark * 100) / (size * size)
  penalty += Math.floor(Math.abs(percent - 50) / 5) * 10

  return penalty
}

/** The path data for one `<path>` covering every dark module, as horizontal
 *  runs. One element instead of several hundred rects, which matters because
 *  this is re-rendered whenever the endpoint changes. */
export function qrPath(code: QrCode): string {
  const parts: string[] = []
  for (let y = 0; y < code.size; y++) {
    const row = at(code.modules, y)
    let x = 0
    while (x < code.size) {
      if (!at(row, x)) {
        x++
        continue
      }
      let run = 1
      while (x + run < code.size && at(row, x + run)) run++
      parts.push(`M${x} ${y}h${run}v1h-${run}z`)
      x += run
    }
  }
  return parts.join('')
}
