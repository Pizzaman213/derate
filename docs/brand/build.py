"""Regenerate derate-lockup.svg and derate-lockup-dark.svg.

    python3 docs/brand/build.py

Needs fonttools (with brotli, for woff2) and `cd ui && npm install`, which is
where the IBM Plex Sans file comes from. It writes nothing else and takes no
arguments.

The two files are a COPY of things that live elsewhere -- the monogram is
drawn in `ui/src/shell/Header.tsx` and duplicated in `SetupTab.tsx`, and the
wordmark is the app's `--font-sans` at the weight that header uses. A README
image cannot import from a React component, so instead of eyeballing a
resemblance this derives every proportion from what the running header
actually does, and stays a script so a change to the mark is one command away
from reaching the README.

`Header.tsx` renders a 32x24 svg and an 18px span in a flex row with
`gap: 14px; align-items: center` (derate.css `header`), and nudges the mark
down 2px. Three numbers fall out of that -- the ink-to-ink gap, the mark's
scale against the text, and where the mark sits relative to the baseline --
and they are computed here in ems, so the lockup can be drawn at any size.
"""

from pathlib import Path

from fontTools.misc.transform import Transform
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
FONT = REPO / "ui/node_modules/@fontsource/ibm-plex-sans/files/ibm-plex-sans-latin-500-normal.woff2"

TEXT = "derate"      # lowercase, as the header's own wordmark and ui/index.html
SIZE = 40.0          # the em everything below is expressed in
PAD = 3.0            # margin around the ink, so <p align="center"> centres the ink

# --- the mark, from ui/src/shell/Header.tsx --------------------------------
# viewBox 0 0 64 50. The strokes are 6 wide with square caps, so the ink runs
# x 5..59, y 8..42 -- symmetric in the box, which is why flex centring in the
# header centres the ink and not merely the element.
MARK = [
    ('M26 25 V39 H56', 0.32),   # the faint echo
    ('M8 25 H26 V11 H56', 1.0),  # the solid stroke
]
M_BOX_W, M_BOX_H = 64.0, 50.0
M_INK_L, M_INK_R, M_INK_T, M_INK_B = 5.0, 59.0, 8.0, 42.0

# --- what the header does --------------------------------------------------
H_FONT, H_SVG_W, H_SVG_H, H_GAP, H_NUDGE = 18.0, 32.0, 24.0, 14.0, 2.0

# --font-sans ink, from ui/src/styles/tokens.css. An SVG loaded through <img>
# inherits no colour, so `currentColor` -- what Header.tsx uses -- would come
# out black and vanish against a dark README. Both values are baked in and the
# README picks between them with <picture media="(prefers-color-scheme: dark)">.
INKS = {'derate-lockup.svg': '#1A1917', 'derate-lockup-dark.svg': '#EDE9E0'}


def wordmark(font_path, text, size, tracking_em):
    """Outline `text` to one SVG path, baseline at y=0. Returns (path, bounds).

    Outlined rather than left as <text> because the file has to draw the same
    everywhere it is opened, and IBM Plex Sans is not installed on most
    machines that will render this README -- including this one.
    """
    font = TTFont(font_path)
    glyphs, cmap = font.getGlyphSet(), font.getBestCmap()
    hmtx = font['hmtx']
    scale = size / font['head'].unitsPerEm
    track = tracking_em * size
    ascent = font['hhea'].ascent / font['head'].unitsPerEm
    descent = -font['hhea'].descent / font['head'].unitsPerEm

    def run(pen):
        x = 0.0
        for ch in text:
            name = cmap[ord(ch)]
            # font space is y-up, SVG is y-down
            glyphs[name].draw(TransformPen(pen, Transform(scale, 0, 0, -scale, x, 0)))
            x += hmtx[name][0] * scale + track

    path = SVGPathPen(glyphs, ntos=lambda v: f'{v:.2f}')
    run(path)
    bounds = BoundsPen(glyphs)
    run(bounds)
    return path.getCommands(), bounds.bounds, ascent, descent


# Header.tsx: fontWeight 500, letterSpacing '-.3px' at fontSize 18.
word, (w_l, w_t, w_r, w_b), ASC, DESC = wordmark(FONT, TEXT, SIZE, -0.3 / 18.0)

# width=32 height=24 over a 64x50 viewBox is not a uniform box (0.5 vs 0.48).
# preserveAspectRatio defaults to `meet`, so the mark draws at the SMALLER of
# the two and is letterboxed 0.64px either side -- which the gap has to account
# for, or the wordmark sits that much too far out.
h_scale = min(H_SVG_W / M_BOX_W, H_SVG_H / M_BOX_H)
h_letterbox = (H_SVG_W - M_BOX_W * h_scale) / 2
MARK_SCALE = h_scale / H_FONT * SIZE

# Ink to ink, which is not the 14px flex gap: the svg box carries the letterbox
# plus the mark's own right margin, and the 'd' carries a left side bearing.
gap_em = ((H_SVG_W + H_GAP + w_l * (H_FONT / SIZE))
          - (h_letterbox + M_INK_R * h_scale)) / H_FONT
GAP = gap_em * SIZE

# `align-items: center` centres the svg box on the text's LINE box, not on its
# baseline, and normal line-height is ascent+descent. Then Header.tsx pushes the
# mark down 2px: "The monogram's solid stroke sits above the icon's own
# bounding-box centre -- the faint echo stroke below it doesn't carry the same
# visual weight -- so flex centring against the wordmark leaves the mark looking
# high." Same judgement, same fraction of the mark, at this size.
centre_em = -((ASC + DESC) / 2 - DESC) + H_NUDGE / H_FONT
MARK_CENTRE = centre_em * SIZE

# --- lay it out ------------------------------------------------------------
m_ink_w = (M_INK_R - M_INK_L) * MARK_SCALE
m_half_h = (M_INK_B - M_INK_T) / 2 * MARK_SCALE

top = min(w_t, MARK_CENTRE - m_half_h)       # the wordmark's ascenders, just
bottom = max(w_b, MARK_CENTRE + m_half_h)    # the mark's echo stroke
height = round(bottom - top + 2 * PAD, 2)
baseline = PAD - top

mark_x = PAD - M_INK_L * MARK_SCALE
mark_y = baseline + MARK_CENTRE - (M_INK_T + M_INK_B) / 2 * MARK_SCALE
word_x = PAD + m_ink_w + GAP - w_l
width = round(word_x + w_r + PAD, 2)

paths = ''.join(
    f'<path d="{d}"{"" if o == 1.0 else f" opacity=\"{o}\""}/>' for d, o in MARK
)

for name, ink in INKS.items():
    (HERE / name).write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}"'
        f' width="{width}" height="{height}" role="img" aria-label="{TEXT}">'
        f'<title>{TEXT}</title>'
        f'<g transform="translate({mark_x:.3f} {mark_y:.3f}) scale({MARK_SCALE:.4f})"'
        f' fill="none" stroke="{ink}" stroke-width="6" stroke-linecap="square">{paths}</g>'
        f'<path transform="translate({word_x:.3f} {baseline:.3f})" fill="{ink}" d="{word}"/>'
        f'</svg>\n'
    )
    print(f'wrote {name}')

print(f'  viewBox 0 0 {width} {height}')
print(f'  mark    scale {MARK_SCALE:.4f} at ({mark_x:.2f}, {mark_y:.2f})')
print(f'  word    baseline y={baseline:.2f}, x={word_x:.2f}')
print(f'  derived gap {gap_em:.4f} em, mark centre {centre_em:.4f} em from the baseline')
