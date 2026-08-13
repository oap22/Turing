#!/usr/bin/env python3
"""Render the Turing Desktop app icon.

Single source of truth for the mark: the constants below drive both the
vector master (`icon.svg`) and the 1024px raster (`icon-source.png`) that
`npx tauri icon` fans out into the platform icon set. Re-run after editing:

    python3 make_icon.py
    cd ../.. && npx tauri icon src-tauri/icons/icon-source.png

The mark is a capital T bracketed like a shell prompt, drawn in the default
`turing` palette from `webui/src/index.css`.
"""

from pathlib import Path

from PIL import Image, ImageDraw

S = 1024  # canvas
SS = 4  # supersample factor

BG = "#060809"  # --t-bg
EDGE = "#1d2329"  # --t-edge
FG = "#c9d1d9"  # --t-fg
ACCENT = "#22d3ee"  # --t-accent

# rounded-square plate, inset so macOS' icon grid leaves it room to breathe
PLATE = (100, 100, 924, 924)
PLATE_R = 185
EDGE_W = 8

# the two brackets, as open polylines with the stroke centred on the points
BRACKETS = (
    [(330, 260), (250, 260), (250, 764), (330, 764)],
    [(694, 260), (774, 260), (774, 764), (694, 764)],
)
BRACKET_W = 56

# the T: crossbar wide enough to read as a capital, stem the same weight
CROSSBAR = (342, 344, 682, 436)
STEM = (466, 344, 558, 696)
T_R = 14

HERE = Path(__file__).parent


def render_png(path: Path) -> None:
    img = Image.new("RGBA", (S * SS, S * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def sc(box):
        return [v * SS for v in box]

    d.rounded_rectangle(
        sc(PLATE), radius=PLATE_R * SS, fill=BG, outline=EDGE, width=EDGE_W * SS
    )

    w = BRACKET_W * SS
    for points in BRACKETS:
        d.line([(x * SS, y * SS) for x, y in points], fill=FG, width=w, joint="curve")
        for x, y in (points[0], points[-1]):  # round caps (PIL only rounds joints)
            cx, cy, r = x * SS, y * SS, w / 2
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=FG)

    for box in (CROSSBAR, STEM):
        d.rounded_rectangle(sc(box), radius=T_R * SS, fill=ACCENT)

    img.resize((S, S), Image.LANCZOS).save(path)


def render_svg(path: Path) -> None:
    x0, y0, x1, y1 = PLATE

    def poly(points):
        return " ".join(
            f"{'M' if i == 0 else 'L'}{x} {y}" for i, (x, y) in enumerate(points)
        )

    def rect(box):
        bx0, by0, bx1, by1 = box
        return (
            f'  <rect x="{bx0}" y="{by0}" width="{bx1 - bx0}" '
            f'height="{by1 - by0}" rx="{T_R}" fill="{ACCENT}"/>\n'
        )

    brackets = "".join(
        f'  <path d="{poly(p)}" fill="none" stroke="{FG}" '
        f'stroke-width="{BRACKET_W}" stroke-linecap="round" '
        f'stroke-linejoin="round"/>\n'
        for p in BRACKETS
    )
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{S}" height="{S}" '
        f'viewBox="0 0 {S} {S}">\n'
        f'  <rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" '
        f'rx="{PLATE_R}" fill="{BG}" stroke="{EDGE}" stroke-width="{EDGE_W}"/>\n'
        f"{brackets}{rect(CROSSBAR)}{rect(STEM)}"
        f"</svg>\n"
    )


if __name__ == "__main__":
    render_png(HERE / "icon-source.png")
    render_svg(HERE / "icon.svg")
    print("wrote icon-source.png + icon.svg")
