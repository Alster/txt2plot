#!/usr/bin/env python3
"""
text_to_gcode.py — Convert text to per-page GCode for pen plotter.

Pipeline:
  input.txt → SVG pages (using Hershey SVG font) → GCode via vpype → plot.py

Usage:
    python3 text_to_gcode.py input.txt [options]

Vertical spacing parameters:
    --size MM         em size — controls glyph scale only
    --line-height MM  fixed slot per line (baseline-to-baseline). Default: size * 1.5
    --line-gap MM     extra leading between line slots. Default: 0
    Total advance per line = line-height + line-gap

Examples:
    python3 text_to_gcode.py poem.txt
    python3 text_to_gcode.py poem.txt --orientation landscape \\
        --margin-left 73 --margin-right 80 --margin-top 42 --margin-bottom 20 \\
        --size 5 --line-height 7 --line-gap 2
    python3 text_to_gcode.py poem.txt --size 6 --line-height 9 --svg-only
"""

import argparse
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

# vpype.toml: use local copy next to this script if it exists, otherwise fall
# back to the user-level config (~/.config/vpype/vpype.toml on Linux/macOS,
# %APPDATA%\vpype\vpype.toml on Windows).
_LOCAL_CONFIG = Path(__file__).parent / "vpype.toml"
VPYPE_CONFIG = str(
    _LOCAL_CONFIG if _LOCAL_CONFIG.exists()
    else Path.home() / ".config/vpype/vpype.toml"
)


# ---------------------------------------------------------------------------
# Font parsing
# ---------------------------------------------------------------------------

def parse_svg_font(font_path: str) -> tuple[dict, dict]:
    """Parse Hershey SVG font file.

    Returns:
        glyphs:     {char: {'d': path_data, 'advance': float}}
        font_face:  {'upm': float, 'ascent': float, 'descent': float}
    """
    tree = ET.parse(font_path)
    root = tree.getroot()

    def strip_ns(tag):
        return tag.split('}', 1)[-1] if '}' in tag else tag

    font_face = {'upm': 1000.0, 'ascent': 800.0, 'descent': -200.0}
    default_advance = 600.0
    glyphs = {}

    for el in root.iter():
        tag = strip_ns(el.tag)

        if tag == 'font':
            default_advance = float(el.get('horiz-adv-x', default_advance))

        elif tag == 'font-face':
            font_face['upm']     = float(el.get('units-per-em', 1000))
            font_face['ascent']  = float(el.get('ascent',  800))
            font_face['descent'] = float(el.get('descent', -200))

        elif tag == 'glyph':
            ch = el.get('unicode', '')
            if not ch:
                continue
            advance = float(el.get('horiz-adv-x', default_advance))
            d = el.get('d', '')
            glyphs[ch] = {'d': d, 'advance': advance}

    return glyphs, font_face


# ---------------------------------------------------------------------------
# Text layout
# ---------------------------------------------------------------------------

def measure_line(text: str, glyphs: dict, scale: float, fallback_advance: float) -> float:
    """Measure rendered width of a text line in mm."""
    width = 0.0
    for ch in text:
        g = glyphs.get(ch) or glyphs.get(' ')
        width += (g['advance'] if g else fallback_advance) * scale
    return width


def wrap_text(text: str, max_width_mm: float, glyphs: dict, scale: float,
              fallback_advance: float) -> list[str]:
    """Word-wrap text into lines that fit within max_width_mm.

    Preserves blank lines (paragraph breaks).
    """
    space_w = (glyphs[' ']['advance'] if ' ' in glyphs else fallback_advance) * scale
    all_lines = []

    for paragraph in text.split('\n'):
        if not paragraph.strip():
            all_lines.append('')
            continue

        words = paragraph.split()
        cur_words: list[str] = []
        cur_width = 0.0

        for word in words:
            word_w = measure_line(word, glyphs, scale, fallback_advance)
            if not cur_words:
                cur_words = [word]
                cur_width = word_w
            elif cur_width + space_w + word_w <= max_width_mm:
                cur_words.append(word)
                cur_width += space_w + word_w
            else:
                all_lines.append(' '.join(cur_words))
                cur_words = [word]
                cur_width = word_w

        if cur_words:
            all_lines.append(' '.join(cur_words))

    return all_lines


def split_pages(lines: list[str], lines_per_page: int) -> list[list[str]]:
    """Split a flat list of lines into page-sized chunks."""
    pages = []
    for i in range(0, len(lines), lines_per_page):
        pages.append(lines[i : i + lines_per_page])
    return pages


# ---------------------------------------------------------------------------
# SVG rendering
# ---------------------------------------------------------------------------

def render_svg(page_lines: list[str], glyphs: dict, font_face: dict,
               font_size_mm: float, line_height_mm: float, line_gap_mm: float,
               margin_left: float, margin_top: float,
               layout_width: float, layout_height: float,
               fallback_advance: float, landscape: bool = False,
               line_offset: int = 0) -> str:
    """Render one page of text as an SVG string.

    layout_width/layout_height are the text-area dimensions in reading
    orientation (e.g. 297×210 for landscape A4).

    Vertical spacing:
      - line_height_mm : fixed slot per line (baseline-to-baseline).
                         Glyphs are scaled to font_size_mm independently.
      - line_gap_mm    : extra leading added after each line slot.
      - Total advance  : line_height_mm + line_gap_mm (constant, content-independent).
      - First baseline : margin_top + line_height_mm
                         (top of the first line slot lands at margin_top).

    Portrait mode (landscape=False):
      - SVG dimensions: layout_width × layout_height
      - Glyph transform: translate(cursor_x, baseline_y) scale(s, -s)

    Landscape mode (landscape=True):
      - Paper is physically portrait on the plotter (short side × long side).
      - SVG is generated as portrait so vpype + vertical_flip work correctly.
      - Content is rotated 90° CCW — turn paper 90° CCW to read.
      - Glyph transform: matrix(0, -s, -s, 0, baseline_y, layout_width - cursor_x)
    """
    upm    = font_face['upm']
    ascent = font_face['ascent']
    scale  = font_size_mm / upm
    advance = line_height_mm + line_gap_mm  # fixed per-line advance

    path_els = []
    baseline_y = margin_top + line_height_mm + line_offset * advance

    for line in page_lines:
        cursor_x = margin_left

        for ch in line:
            glyph = glyphs.get(ch)

            if glyph is None:
                sp = glyphs.get(' ')
                cursor_x += (sp['advance'] if sp else fallback_advance) * scale
                continue

            if glyph['d']:
                s = scale
                if landscape:
                    # matrix(a,b,c,d,e,f): font (u,v) → svg (a*u+c*v+e, b*u+d*v+f)
                    e  = f"{baseline_y:.4f}"
                    f_ = f"{layout_width - cursor_x:.4f}"
                    ns = f"{-s:.6f}"
                    transform = f"matrix(0,{ns},{ns},0,{e},{f_})"
                else:
                    tx = f"{cursor_x:.4f}"
                    ty = f"{baseline_y:.4f}"
                    ps = f"{s:.6f}"
                    ns = f"{-s:.6f}"
                    transform = f"translate({tx},{ty}) scale({ps},{ns})"

                path_els.append(
                    f'<path d="{glyph["d"]}"'
                    f' transform="{transform}"'
                    f' fill="none" stroke="black" stroke-width="10"'
                    f' stroke-linecap="round" stroke-linejoin="round"/>'
                )

            cursor_x += glyph['advance'] * scale

        baseline_y += advance

    body = "\n  ".join(path_els)

    # Landscape: SVG is portrait (short × long), i.e. height × width in layout terms
    if landscape:
        svg_w, svg_h = layout_height, layout_width
    else:
        svg_w, svg_h = layout_width, layout_height

    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg"\n'
        f'     width="{svg_w}mm" height="{svg_h}mm"\n'
        f'     viewBox="0 0 {svg_w} {svg_h}">\n'
        f'  {body}\n'
        f'</svg>\n'
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Convert text to per-page GCode for pen plotter.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('input', help='Input text file (UTF-8)')
    parser.add_argument('--orientation', choices=['portrait', 'landscape'],
                        default='portrait',
                        help='Page orientation for reading. '
                             'Landscape rotates content 90° CCW — '
                             'turn the printed paper 90° CCW to read it.')
    parser.add_argument('--font', required=True,
                        help='Path to Hershey SVG font (.svg)')
    parser.add_argument('--size', type=float, default=5.0,
                        help='Font size in mm')
    parser.add_argument('--line-height', type=float, default=None,
                        help='Fixed height per line slot in mm, '
                             'i.e. baseline-to-baseline distance (default: size × 1.5)')
    parser.add_argument('--line-gap', type=float, default=0.0, metavar='MM',
                        help='Extra leading between line slots in mm (default: 0). '
                             'Total advance = line-height + line-gap.')
    parser.add_argument('--margin-top',    type=float, default=20.0, metavar='MM',
                        help='Top margin in reading orientation')
    parser.add_argument('--margin-bottom', type=float, default=20.0, metavar='MM',
                        help='Bottom margin in reading orientation')
    parser.add_argument('--margin-left',   type=float, default=20.0, metavar='MM',
                        help='Left margin in reading orientation')
    parser.add_argument('--margin-right',  type=float, default=20.0, metavar='MM',
                        help='Right margin in reading orientation')
    parser.add_argument('--page-width',  type=float, default=210.0, metavar='MM',
                        help='Physical paper short side (A4 = 210)')
    parser.add_argument('--page-height', type=float, default=297.0, metavar='MM',
                        help='Physical paper long side (A4 = 297)')
    parser.add_argument('--output-dir', default='.', metavar='DIR',
                        help='Directory for output files')
    parser.add_argument('--prefix', default='page',
                        help='Filename prefix for output files')
    parser.add_argument('--start-line', type=int, default=0, metavar='N',
                        help='Skip first N lines on page 1 (continue on partially filled page)')
    parser.add_argument('--svg-only', action='store_true',
                        help='Generate SVG files only, skip GCode conversion')
    args = parser.parse_args()

    if args.line_height is None:
        args.line_height = args.size * 1.5

    landscape = (args.orientation == 'landscape')

    # Layout dimensions are in reading orientation:
    #   portrait  → short × long  (e.g. 210 × 297)
    #   landscape → long  × short (e.g. 297 × 210)
    if landscape:
        layout_w = args.page_height  # long side becomes reading width
        layout_h = args.page_width   # short side becomes reading height
    else:
        layout_w = args.page_width
        layout_h = args.page_height

    # ── Load font ──────────────────────────────────────────────────────────
    print(f"Font:  {args.font}")
    glyphs, font_face = parse_svg_font(args.font)
    fallback_advance = font_face['upm'] * 0.5
    scale = args.size / font_face['upm']
    print(f"       UPM={font_face['upm']:.0f}, ascent={font_face['ascent']:.0f}, "
          f"glyphs={len(glyphs)}")

    # ── Read text ──────────────────────────────────────────────────────────
    with open(args.input, encoding='utf-8') as f:
        text = f.read()

    # ── Layout ────────────────────────────────────────────────────────────
    text_width  = layout_w - args.margin_left - args.margin_right
    text_height = layout_h - args.margin_top  - args.margin_bottom
    advance     = args.line_height + args.line_gap
    lines_per_page = int(text_height / advance)

    print(f"Page:  {layout_w}×{layout_h} mm ({args.orientation})  "
          f"margins T{args.margin_top} B{args.margin_bottom} "
          f"L{args.margin_left} R{args.margin_right}")
    print(f"Area:  {text_width}×{text_height} mm")
    print(f"Font:  size={args.size} mm  line-height={args.line_height} mm  "
          f"line-gap={args.line_gap} mm  →  advance={advance} mm, "
          f"{lines_per_page} lines/page")

    # ── Wrap & paginate ────────────────────────────────────────────────────
    all_lines = wrap_text(text, text_width, glyphs, scale, fallback_advance)
    pages = split_pages(all_lines, lines_per_page)

    start_line = args.start_line
    if start_line:
        print(f"Lines: {len(all_lines)} total → {len(pages)} page(s)  "
              f"(page 1 starts from line {start_line})")
    else:
        print(f"Lines: {len(all_lines)} total → {len(pages)} page(s)")

    # ── Generate output ────────────────────────────────────────────────────
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    generated_gcodes = []

    for page_num, page_lines in enumerate(pages, 1):
        svg_path   = out_dir / f"{args.prefix}_{page_num:04d}.svg"
        gcode_path = out_dir / f"{args.prefix}_{page_num:04d}.gcode"

        svg = render_svg(
            page_lines, glyphs, font_face,
            font_size_mm   = args.size,
            line_height_mm = args.line_height,
            line_gap_mm    = args.line_gap,
            margin_left    = args.margin_left,
            margin_top     = args.margin_top,
            layout_width   = layout_w,
            layout_height  = layout_h,
            fallback_advance = fallback_advance,
            landscape      = landscape,
            line_offset    = start_line if page_num == 1 else 0,
        )
        svg_path.write_text(svg, encoding='utf-8')
        print(f"  [{page_num}/{len(pages)}] SVG  → {svg_path}")

        if args.svg_only:
            continue

        cmd = [
            'vpype', '-c', VPYPE_CONFIG,
            'read', str(svg_path),
            # 'linemerge', '--tolerance', '0.5mm',
            # 'linesort',
            'gwrite', '--profile', 'drawcore', str(gcode_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"         ERROR (vpype): {result.stderr.strip()}")
        else:
            print(f"         GCode → {gcode_path}")
            generated_gcodes.append(gcode_path)

    # ── Print instructions ─────────────────────────────────────────────────
    print()
    if generated_gcodes:
        print("To plot each page:")
        for p in generated_gcodes:
            print(f"  python3 plot.py {p}")
    elif args.svg_only:
        print("SVG files generated. To convert to GCode manually:")
        print(f"  vpype -c {VPYPE_CONFIG} read PAGE.svg linemerge --tolerance 0.5mm "
              f"linesort gwrite --profile drawcore PAGE.gcode")


if __name__ == '__main__':
    main()
