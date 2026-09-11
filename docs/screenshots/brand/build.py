"""Regenerate the README's brand art.

    python3 docs/screenshots/brand/build.py

Writes six files: the bare lockup (`derate-lockup{,-dark}.svg`), the banner the
README heads with (`derate-banner{,-dark}.svg`), and a 16:9 card
(`derate-card{,-dark}.svg`) for contexts that want the mark on a fixed-ratio
field rather than a width-driven strip -- a social preview, a slide, a repo
thumbnail. All three are the same lockup on a field of the app's own panel
colour; the banner adds the project's one-line claim under it, the card does
not. Needs fonttools (with brotli, for woff2) and `cd ui && npm install`, which
is where the IBM Plex Sans files come from. Takes no arguments.

Everything here is a COPY of something that lives elsewhere -- the monogram is
drawn in `ui/src/shell/Header.tsx` and duplicated in `SetupTab.tsx`, the
wordmark is the app's `--font-sans`, and the colours are `--panel`, `--ink` and
`--ink-muted` from `ui/src/styles/tokens.css`. A README image cannot import
from a React component or a stylesheet, so instead of eyeballing a resemblance
this derives every proportion from what the running header actually does, and
stays a script so a change to the mark is one command away from the README.

`Header.tsx` renders a 32x24 svg and an 18px span in a flex row with
`gap: 14px; align-items: center` (derate.css `header`), and nudges the mark
down 2px. Three numbers fall out of that -- the ink-to-ink gap, the mark's
scale against the text, and where the mark sits relative to the baseline --
and they are computed here in ems, so the lockup can be drawn at any size.
"""

import re
from pathlib import Path

from fontTools.misc.transform import Transform
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

HERE = Path(__file__).resolve().parent


def _repo_root(start: Path) -> Path:
    """Walk up until the checkout's own marker turns up.

    A counted `.parent.parent` is right for exactly one location, and this
    folder has already moved once -- up one level, under `docs/screenshots/` --
    which silently repointed FONTS at `docs/ui/node_modules/` and left the
    build unable to find a font it was standing three directories away from.
    Searching for the marker means the next move costs nothing.
    """
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise SystemExit(f"no pyproject.toml above {start}: not inside the checkout")


REPO = _repo_root(HERE)
FONTS = REPO / "ui/node_modules/@fontsource/ibm-plex-sans/files"

TEXT = "derate"      # lowercase, as the header's own wordmark and ui/index.html
SIZE = 40.0          # the em the lockup is expressed in
PAD = 3.0            # margin around the lockup's ink, so centring centres the ink

# The line under the mark. Straight from the person who wrote the thing.
QUOTE = (
    "“I want it to be simple to run multiple models",
    "on your own machine, without recoding the API.”",
)

# --- the mark, from ui/src/shell/Header.tsx --------------------------------
# viewBox 0 0 64 50. The strokes are 6 wide with square caps, so the ink runs
# x 5..59, y 8..42 -- symmetric in the box, which is why flex centring in the
# header centres the ink and not merely the element.
MARK = [
    ("echo", "M26 25 V39 H56", 0.32),    # the derated branch, stepping down
    ("solid", "M8 25 H26 V11 H56", 1.0),  # full rate, stepping up
]
M_BOX_W, M_BOX_H = 64.0, 50.0
M_INK_L, M_INK_R, M_INK_T, M_INK_B = 5.0, 59.0, 8.0, 42.0

# --- what the header does --------------------------------------------------
H_FONT, H_SVG_W, H_SVG_H, H_GAP, H_NUDGE = 18.0, 32.0, 24.0, 14.0, 2.0

# ui/src/styles/tokens.css. An SVG loaded through <img> inherits no colour, so
# `currentColor` -- what Header.tsx uses -- would come out black and vanish
# against a dark README. Both themes are baked in and the README picks between
# them with <picture media="(prefers-color-scheme: dark)">.
# `flow` is the accent tokens.css reserves for "a request in flight. An accent,
# not a grey: it is a real measured event" -- which is exactly what the pulse
# running the mark depicts, so it is that token and not a colour picked here.
THEMES = {
    "": {
        "ink": "#1A1917", "muted": "#5C5851", "panel": "#EDE9E0",
        "flow": "#4A6FA5",
    },
    "-dark": {
        "ink": "#EDE9E0", "muted": "#979186", "panel": "#191817",
        "flow": "#7FA3D4",
    },
}

#: One cycle of the pulse. Both branches share it, so they are sequenced with
#: keyTimes inside a single duration rather than with `begin` offsets -- a
#: `begin` offset under repeatCount="indefinite" repeats on the animation's own
#: period, not on the cycle's, and the two would drift apart.
PULSE_DUR = "5s"
#: When each branch runs, as a fraction of the cycle. The solid goes first,
#: the derated one answers, then both sit off the path for the rest.
PULSE_WINDOWS = {"solid": (0.00, 0.34), "echo": (0.42, 0.76)}


class Face:
    """One font file, able to outline a string to an SVG path.

    Outlined rather than left as <text> because these files have to draw the
    same everywhere they are opened, and IBM Plex Sans is not installed on most
    machines that will render this README -- including the one it was built on.
    """

    def __init__(self, filename: str) -> None:
        self.font = TTFont(FONTS / filename)
        self.glyphs = self.font.getGlyphSet()
        self.cmap = self.font.getBestCmap()
        self.hmtx = self.font["hmtx"]
        self.upem = self.font["head"].unitsPerEm
        self.ascent = self.font["hhea"].ascent / self.upem
        self.descent = -self.font["hhea"].descent / self.upem

    def outline(self, text: str, size: float, tracking_em: float = 0.0):
        """Return (svg path, ink bounds, advance width). Baseline at y=0."""
        scale = size / self.upem
        track = tracking_em * size

        def run(pen):
            x = 0.0
            for ch in text:
                name = self.cmap[ord(ch)]
                # font space is y-up, SVG is y-down
                self.glyphs[name].draw(
                    TransformPen(pen, Transform(scale, 0, 0, -scale, x, 0))
                )
                x += self.hmtx[name][0] * scale + track
            return x - track

        path = SVGPathPen(self.glyphs, ntos=lambda v: f"{v:.2f}")
        advance = run(path)
        bounds = BoundsPen(self.glyphs)
        run(bounds)
        return path.getCommands(), bounds.bounds, advance


MEDIUM = Face("ibm-plex-sans-latin-500-normal.woff2")   # the wordmark
REGULAR = Face("ibm-plex-sans-latin-400-normal.woff2")  # the quote

# Header.tsx: fontWeight 500, letterSpacing '-.3px' at fontSize 18.
word, (w_l, w_t, w_r, w_b), _ = MEDIUM.outline(TEXT, SIZE, -0.3 / 18.0)

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
centre_em = -((MEDIUM.ascent + MEDIUM.descent) / 2 - MEDIUM.descent) + H_NUDGE / H_FONT
MARK_CENTRE = centre_em * SIZE

# --- lay the lockup out ----------------------------------------------------
m_ink_w = (M_INK_R - M_INK_L) * MARK_SCALE
m_half_h = (M_INK_B - M_INK_T) / 2 * MARK_SCALE

lock_top = min(w_t, MARK_CENTRE - m_half_h)       # the wordmark's ascenders, just
lock_bottom = max(w_b, MARK_CENTRE + m_half_h)    # the mark's echo stroke
LOCK_H = lock_bottom - lock_top
baseline = -lock_top                              # from the lockup's own top edge

mark_x = -M_INK_L * MARK_SCALE
mark_y = baseline + MARK_CENTRE - (M_INK_T + M_INK_B) / 2 * MARK_SCALE
word_x = m_ink_w + GAP - w_l
LOCK_W = word_x + w_r


def path_length(d: str) -> float:
    """Length of an axis-aligned M/H/V path, which is all the mark is made of.

    Derived rather than written down, so the pulse cannot keep running the
    length of a mark that has since been redrawn. Raises on anything curved,
    because a wrong number here would be a silently drifting animation.
    """
    x = y = total = 0.0
    seen = ""
    for cmd, nums in re.findall(r"([A-Za-z])\s*((?:-?[\d.]+[\s,]*)*)", d):
        vals = [float(v) for v in nums.replace(",", " ").split()]
        seen += cmd
        if cmd == "M":
            x, y = vals[0], vals[1]
        elif cmd == "H":
            total += abs(vals[0] - x)
            x = vals[0]
        elif cmd == "V":
            total += abs(vals[0] - y)
            y = vals[0]
        else:
            raise ValueError(f"path_length handles M/H/V only, got {cmd!r} in {d!r}")
    assert seen.startswith("M"), d
    return total


def pulse_path(name: str, d: str, colour: str, opacity: float) -> str:
    """One branch's travelling block, as an overlay on the drawn mark.

    `stroke-linecap` is butt and not the mark's `square`: a square cap adds 3
    units at each end, which would draw a 6-unit dash 12 long. The dash itself
    is 6 -- the mark's own stroke-width -- so the block is square by
    construction rather than by a tuned number, and the gap is the whole path,
    so exactly one block is ever on it.

    Dash lengths are in the path's own user space, so the banner's scale on the
    enclosing group carries the block along with the stroke.
    """
    length = path_length(d)
    start, end = PULSE_WINDOWS[name]
    # From just before the path start to just past its end -- both invisible --
    # so the block enters and leaves rather than popping.
    travel, rest = f"6;{-length:g}", f"{-length:g}"
    if start == 0.0:
        values, keys = f"{travel};{rest}", f"0;{end:g};1"
    else:
        values, keys = f"6;{travel};{rest}", f"0;{start:g};{end:g};1"
    return (
        f'<path class="pulse" d="{d}" stroke="{colour}" stroke-linecap="butt"'
        f' stroke-dasharray="6 {length:g}" opacity="{opacity}">'
        f'<animate attributeName="stroke-dashoffset" values="{values}"'
        f' keyTimes="{keys}" dur="{PULSE_DUR}" repeatCount="indefinite"/>'
        f"</path>"
    )


def lockup(
    ink: str, dx: float = 0.0, dy: float = 0.0, scale: float = 1.0,
    pulse: str | None = None,
) -> str:
    """The mark and the wordmark, ink-flush to (dx, dy) at the given scale.

    At scale 1 the offset is folded into the two inner transforms rather than
    wrapped in a group, so the bare lockup file carries no transform that does
    nothing -- it is the artifact people will open and read.

    `pulse` is an accent colour, or None for a still mark. Only the banner
    passes one; the bare lockup stays exactly as it was.
    """
    paths = "".join(
        f'<path d="{d}"{"" if o == 1.0 else f" opacity=\"{o}\""}/>'
        for _, d, o in MARK
    )
    if pulse:
        # Inside the same <g>, so the pulse inherits the mark's transform and
        # stroke-width and can never be laid over a differently placed mark.
        paths += "".join(
            pulse_path(name, d, pulse, 1.0 if name == "solid" else 0.5)
            for name, d, _ in MARK
        )
    flat = scale == 1.0
    ox, oy = (dx, dy) if flat else (0.0, 0.0)
    body = (
        f'<g transform="translate({mark_x + ox:.3f} {mark_y + oy:.3f})'
        f' scale({MARK_SCALE:.4f})"'
        f' fill="none" stroke="{ink}" stroke-width="6" stroke-linecap="square">'
        f"{paths}</g>"
        f'<path transform="translate({word_x + ox:.3f} {baseline + oy:.3f})"'
        f' fill="{ink}" d="{word}"/>'
    )
    if flat:
        return body
    return f'<g transform="translate({dx:.3f} {dy:.3f}) scale({scale:.5f})">{body}</g>'


#: SMIL is not driven by CSS animation properties, so the only way to honour
#: reduced motion is to not render the pulse at all.
#:
#: It does NOT take effect in the README, and that was measured rather than
#: assumed: an SVG loaded through <img> never sees the host's
#: prefers-reduced-motion. Probed in Chromium with a square that the query
#: recolours -- it flips when the same markup is inlined into the page, and
#: never flips through <img>. prefers-color-scheme DOES propagate that way;
#: reduced-motion does not.
#:
#: Kept anyway: it is seventy bytes, and it does apply wherever the file is
#: inlined or opened on its own. The honest mitigation for the README is the
#: motion itself -- one small block, most of a five second cycle at rest, and a
#: mark that is never caught incomplete.
REDUCED_MOTION = (
    "<style>@media (prefers-reduced-motion:reduce){.pulse{display:none}}</style>"
)


def svg(width: float, height: float, body: str, label: str, head: str = "") -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}"'
        f' width="{width}" height="{height}" role="img" aria-label="{label}">'
        f"<title>{label}</title>{head}{body}</svg>\n"
    )


# --- the bare lockup -------------------------------------------------------
for suffix, theme in THEMES.items():
    (HERE / f"derate-lockup{suffix}.svg").write_text(
        svg(
            round(LOCK_W + 2 * PAD, 2),
            round(LOCK_H + 2 * PAD, 2),
            lockup(theme["ink"], PAD, PAD),
            TEXT,
        )
    )

# --- the banner ------------------------------------------------------------
BANNER_W = 1200.0
LOGO_INK_W = 380.0                  # the lockup's ink width on the banner
QUOTE_SIZE = 34.0
QUOTE_LEADING = 1.45                # line height, as a multiple of the em
BANNER_PAD = 84.0                   # top margin, and the bottom's to match
LOGO_TO_QUOTE = 58.0                # ink gap, mark's echo stroke to cap height

logo_scale = LOGO_INK_W / LOCK_W
logo_h = LOCK_H * logo_scale
logo_x = (BANNER_W - LOGO_INK_W) / 2

lines = [REGULAR.outline(line, QUOTE_SIZE) for line in QUOTE]
line_step = QUOTE_SIZE * QUOTE_LEADING
# The first baseline sits one cap height below the gap, measured off the real
# ink rather than assumed: the line opens with a quotation mark, whose top is
# higher than the 'I' beside it.
first_baseline = BANNER_PAD + logo_h + LOGO_TO_QUOTE - min(b[1] for _, b, _ in lines)
# The bottom margin is measured to the last BASELINE, not to the descenders
# below it. Only two glyphs in the quote descend, and padding from them leaves
# the banner reading bottom-heavy against a logo whose top edge is a solid
# stroke. The baseline is where the eye puts the bottom of a line of text.
BANNER_H = round(first_baseline + line_step * (len(lines) - 1) + BANNER_PAD, 2)

for suffix, theme in THEMES.items():
    quote = "".join(
        f'<path transform="translate({(BANNER_W - advance) / 2:.2f}'
        f' {first_baseline + i * line_step:.2f})" fill="{theme["muted"]}" d="{path}"/>'
        for i, (path, _, advance) in enumerate(lines)
    )
    (HERE / f"derate-banner{suffix}.svg").write_text(
        svg(
            BANNER_W,
            BANNER_H,
            f'<rect width="{BANNER_W}" height="{BANNER_H}" rx="18"'
            f' fill="{theme["panel"]}"/>'
            + lockup(theme["ink"], logo_x, BANNER_PAD, logo_scale,
                     pulse=theme["flow"])
            + quote,
            f"derate — {QUOTE[0][1:]} {QUOTE[1][:-1]}",
            head=REDUCED_MOTION,
        )
    )

# --- the 16:9 card ----------------------------------------------------------
CARD_W = 1600.0
CARD_H = CARD_W * 9 / 16
CARD_INK_W = 640.0          # the lockup's ink width on the card: 40% of CARD_W
CARD_RADIUS = 24.0

card_scale = CARD_INK_W / LOCK_W
card_logo_h = LOCK_H * card_scale
card_x = (CARD_W - CARD_INK_W) / 2
card_y = (CARD_H - card_logo_h) / 2

for suffix, theme in THEMES.items():
    (HERE / f"derate-card{suffix}.svg").write_text(
        svg(
            CARD_W,
            CARD_H,
            f'<rect width="{CARD_W}" height="{CARD_H}" rx="{CARD_RADIUS}"'
            f' fill="{theme["panel"]}"/>'
            + lockup(theme["ink"], card_x, card_y, card_scale,
                     pulse=theme["flow"]),
            TEXT,
            head=REDUCED_MOTION,
        )
    )

print(f"lockup  {LOCK_W + 2 * PAD:.2f} x {LOCK_H + 2 * PAD:.2f}")
print(f"banner  {BANNER_W:.0f} x {BANNER_H:.2f}, logo scale {logo_scale:.3f}")
print(f"card    {CARD_W:.0f} x {CARD_H:.2f}, logo scale {card_scale:.3f}")
print(f"derived gap {gap_em:.4f} em, mark centre {centre_em:.4f} em from the baseline")
for name in sorted(p.name for p in HERE.glob("derate-*.svg")):
    print(f"  wrote {name}")
