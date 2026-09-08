# setup

The two things the first-run screen needs that nothing else in the product does:
a QR encoder written from scratch, and a stylesheet for the one screen with no
header, no sidebar and no roster. `SetupTab.tsx` itself lives one directory up —
this folder is what it imports.

The encoder exists because every other way of getting a QR code fails on exactly
the machine this product is for. `qr.ts` says so in its own header: a CDN script
is a network call and a derate cluster is routinely on a LAN with no route out;
an npm dependency is 50 KB and a supply-chain surface for ~200 lines of finite,
specified arithmetic; rendering it server-side needs the same arithmetic again in
Python plus a new entry in `requirements.txt`.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `qr.ts` | 551 | the encoder: byte mode, error correction M, versions 1–10, `encodeQr` and `qrPath` |
| `Qr.tsx` | 40 | the `<svg>`, and the decision to render nothing rather than a broken symbol |
| `qr.fixtures.mjs` | 705 | 16 symbols generated with `segno`, as matrices of `'0'`/`'1'` strings |
| `qr.check.mjs` | 350 | the whole gate on the encoder. Hermetic — no `// requires:` line |
| `setup.css` | 323 | the first-run stylesheet, separate from `derate.css` |

## `qr.ts`

Byte mode, error correction level M, versions 1 to 10. That covers 213 bytes,
and the longest address this is ever asked for is an IPv6 literal with a port and
a path — nowhere near it. `encodeQr(text, forcedMask?)` returns `{ size, modules,
version }` with the quiet zone deliberately excluded, because the sensible border
differs between an SVG in a card and a print; `qrPath(code)` flattens the dark
modules to one `<path>` of horizontal runs rather than several hundred `<rect>`s,
which matters because the symbol re-renders whenever the endpoint changes.
Anything past 213 bytes throws `QrTooLongError` rather than truncating or
quietly dropping to a weaker error-correction level: a QR that decodes to half an
address fails after the person has already walked over with their phone.

GF(256) is generated at module load from the primitive polynomial `0x11d` rather
than tabled — 512 bytes of derived data that a typo in a literal would corrupt
invisibly. The 15-bit format block is a real BCH(15,5) computation, XORed with
`0b101010000010010`; the 18-bit version block is four tabled constants
(`0x07c94`, `0x085bc`, `0x09a99`, `0x0a4d3`), on the reasoning that four
constants are cheaper to check than a second BCH routine to get wrong. `at()` is
the single place `noUncheckedIndexedAccess` is asserted away, so there is one
place to look if an index is ever out of range instead of sixty non-null
assertions scattered through the arithmetic.

**The three corner alignment patterns are named by position, never detected by
asking whether the cell is written.** From version 7 the timing pattern reaches
the second alignment centre, so "something is here already" is also true of a
perfectly ordinary alignment pattern that is supposed to be drawn over the
timing row. The occupancy test silently dropped two patterns per symbol from
version 7 up — and dropped them from the function-module map as well, so the
data walk then wrote message bits through the hole. Versions 1 to 6 were
unaffected and looked like proof.

## `Qr.tsx`

Forty lines around `encodeQr` and `qrPath`. It renders **nothing at all** when
the address will not fit, rather than a broken or half-drawn code: every caller
shows the address as text beside it, so the affordance degrades to the thing it
was a shortcut for. The `catch` is silent on purpose — `encodeQr` throws only for
a payload past version 10, and there is nothing to report to the person standing
there, because the address is on screen either way.

The fill is the literal `#1A1917` and not `var(--ink)`, with a comment saying
why: it is `--ink`'s light value, and the tile under it is pinned to the light
palette in both themes so the symbol always scans. The two halves of that
decision are one edit apart, in this file and at `.setup-qr` in `setup.css`.

## `qr.fixtures.mjs`

Ground truth, not a verifier: 16 exported `cases`, each `{ label, text, version,
matrix }`, generated with `segno` (BSD-3-Clause), an independent implementation
of ISO/IEC 18004. The regeneration command is in the header.

**`boost_error=False` and `micro=False` are load-bearing.** segno silently
upgrades the error-correction level when a shorter one would not change the
version, and picks a Micro QR symbol for short inputs — either would compare
level-M output against a symbol of a different kind and fail for a reason that
has nothing to do with a bug. The cases cover every version from 1 to 10 at its
capacity boundary, which is where an off-by-one in the block tables shows up,
plus the addresses this is actually asked to encode: `http://192.168.0.71:8088/v1`,
a `.local` hostname, an IPv6 literal, a UTF-8 multibyte query and the whole
printable ASCII range.

## `qr.check.mjs`

The whole gate on ~200 lines of transcribed specification — block tables,
alignment centres, two error-correction codes, eight mask patterns, four penalty
rules — every one of them a place a digit can be wrong in a way types cannot see
and a human cannot eyeball, because the output is a field of squares.

**It started out comparing matrices with segno module-for-module, and that test
was wrong.** It failed on symbols that scan perfectly. After the terminator the
spec pads to the byte boundary and then appends `0xEC`/`0x11` alternately, and
segno emits one extra `0x00` codeword before it starts. Both symbols carry the
same message and both decode; they disagree only about bytes a decoder never
looks at. Module equality was asserting that two encoders made the same
arbitrary choice, not that either was correct.

So the property under test is that the symbol decodes to the string that went
in, checked twice: our encoder through this file's decoder, and *segno's*
matrices through the same decoder. The second is the independence — the decoder
re-states the placement walk, the masks and the format block from the spec and
deliberately keeps its own copy of the tables (`DEC_EC_M`, `DEC_ALIGN`,
`DEC_MASKS`) rather than importing `qr.ts`'s, because a shared wrong alignment
centre would move the function map in both at once and still line up with
itself. Reed-Solomon is checked directly instead: every block's syndromes must be
zero. Then the properties no fixture states — determinism, 21 modules at version
1, no quiet zone, the three finders, 214 bytes throwing and 213 not, and that
`qrPath` covers every dark module and no light one.

## `setup.css`

A separate stylesheet rather than more of `derate.css`, for two stated reasons:
four sessions edit that sheet at once, and this screen has no header, sidebar or
roster, so almost nothing in the shared sheet applies to it anyway. Every colour
is a token, and the one deliberate exception is called out where it happens.

Two rules here are arguments, not styling. `.setup-bar` has two span classes and
draws exactly one of them, because there are two honest states and conflating
them would mean inventing a number: `.is-known` is a real fraction of real
bytes, and `.is-open` sweeps at 40% width and claims no position at all — the
case where nothing downstream reports byte counts, since vLLM fetches through
Hugging Face and the control plane never sees a total. A determinate-looking bar
creeping at a made-up rate is the one thing this must not do, because people read
it as an estimate and plan around it. `.setup-log` uses `--on-fill-dim` and not
`--ink-muted` because it sits inside `.setup-plate`, which paints `--fill` and
swaps ink for `--on-fill`; the page's muted ink on the plate's ground is the
pairing that disappears in one of the two themes. Both animations have a
`prefers-reduced-motion` branch that keeps the state visible without the
movement.

## The seam with `SetupTab.tsx`

`SetupTab.tsx` is the only consumer of anything in this folder, and takes exactly
two things from it:

```tsx
import { Qr } from './setup/Qr'
import './setup/setup.css'
// ...
{endpoint && <Qr text={endpoint} label={`QR code for ${endpoint}`} />}
```

`endpoint` is not `window.location.origin`. It comes from
`backend.mintEnrollment({ ttl_s: 60 })` — `${e.join_url.replace(/\/+$/, '')}/v1`,
trailing slashes stripped so a coordinator whose join URL ends in one does not
produce `//v1` — for the same reason the join command uses it: this is the
address somebody types into another machine, and the browser's idea of it is
only right by coincidence. The origin is the fallback when that call fails.
Until it resolves, `endpoint` is `null`, the `Qr` is not rendered at all, and the
`<code>` beside it reads "finding this coordinator's address…".

`qr.check.mjs` is discovered by `ui/check.mjs`, which scans for `*.check.mjs` and
reads a `// requires:` line off the top of each. This one has none, which means
hermetic — it needs nothing but the checkout and a `node_modules` with esbuild
in it. It bundles `qr.ts` through esbuild's JS API rather than the launcher under
`node_modules/.bin`, because that shim is a POSIX script with no `.cmd` twin and
spawning it by path fails on Windows.

## Things that look like details and are not

**The pad sequence is keyed on its own position, not on `codewords.length`.**
How many codewords the payload already filled has nothing to do with which pad
byte comes first, and tying the two together puts `0x11` first whenever that
count happens to be odd.

**The zigzag's direction is a function of where the column pair is, not of how
many pairs have been drawn.** `((right + 1) & 2) === 0`. The vertical timing
column shifts the sequence by one, and a running toggle would then be upside
down for every column after it.

**`forcedMask` exists for `qr.check.mjs` and nothing else.** Pinning the mask
separates "the modules are placed right" from "the best mask was chosen" — two
independent ways to be wrong that a single comparison conflates.

**`.setup-qr` is deliberately not themed.** A QR drawn light-on-dark is an
inverted symbol and most phone cameras will not read one, so the tile keeps
`#f7f4ee` in both themes. Its border stays tokenised so it still sits in the
page.

## Failure behaviour

- **A payload past 213 bytes.** `encodeQr` throws `QrTooLongError` naming the
  byte count and the limit. `Qr.tsx` catches it and renders `null`; the address
  is already on screen as selectable text with a copy button.
- **No endpoint yet.** `SetupTab` guards on `endpoint &&`, so no symbol is drawn
  and the code element says what it is waiting for.
- **`mintEnrollment` fails.** The endpoint falls back to
  `window.location.origin + '/v1'` — right often enough to be useful, and never
  silently preferred over the coordinator's own answer.
- **Reduced motion.** `.setup-step`'s rise animation is off, `.is-open` stops
  sweeping and holds full width at 0.45 opacity, and the animated ellipsis
  becomes a literal `…`.
- **A wrong digit in a transcribed table.** `qr.check.mjs` exits non-zero and
  prints the failing case's label. Nothing else in the build would notice.

## Deliberately not built

**A QR dependency of any kind.** Named and rejected in `qr.ts`'s header: a CDN
script (a network call on an air-gapped LAN, failing on exactly the install with
the least patience for a broken screen), an npm package (50 KB and a
supply-chain surface), and server-side rendering (the same arithmetic again in
Python, plus a `requirements.txt` entry).

**Versions past 10, and modes other than byte.** The address this encodes is
never close to 213 bytes, and a larger table is more transcription to get wrong.
Past the limit is an exception, not a smaller symbol at a lower error-correction
level.

**Module-for-module equality with segno.** It was there, it failed on symbols
that scan, and it was replaced with round-trip decoding for the reason recorded
at the top of `qr.check.mjs`. Anyone reinstating it walks into the same padding
trap.
