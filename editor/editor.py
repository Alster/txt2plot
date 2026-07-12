#!/usr/bin/env python3
"""
editor.py — Web editor for Hershey pen-plotter text.

Usage:
    python editor.py --font ../fonts/myfont_centerline/myfont_centerline.svg [options]

All layout options mirror text_to_gcode.py. Font and settings are fixed at
startup; only the text and start-line change at runtime.
"""

import sys
import signal
import argparse
import subprocess
import tempfile
import threading
from pathlib import Path

from flask import Flask, render_template, request, jsonify

# Import layout/render functions from parent directory
sys.path.insert(0, str(Path(__file__).parent.parent))
from text_to_gcode import (
    parse_svg_font,
    wrap_text,
    adjust_keyword_lines,
    split_pages,
    render_svg,
    VPYPE_CONFIG,
)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Global config — populated in main() before app.run()
# ---------------------------------------------------------------------------
CFG = {}       # layout parameters (margins, size, etc.)
FONT_DATA = {} # {'glyphs': ..., 'font_face': ..., 'fallback_advance': ..., 'scale': ...}
_plot_proc    = None  # currently running plot.py subprocess
_plot_last_line = ''  # last stdout line from plot.py
_pages_cache  = {}    # (text, start_line) → pages list


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def _compute_pages(text: str, start_line: int) -> list:
    """Run wrap → adjust → paginate. Returns list of pages (no SVG rendered).

    Results are cached by (text, start_line) so that navigating pages
    doesn't re-run the layout pipeline when nothing has changed.
    """
    key = (text, start_line)
    if key in _pages_cache:
        return _pages_cache[key]

    glyphs           = FONT_DATA['glyphs']
    fallback_advance = FONT_DATA['fallback_advance']
    space_w          = FONT_DATA['space_w']
    scale            = FONT_DATA['scale']
    letter_spacing   = CFG['letter_spacing']
    lines_per_page   = CFG['lines_per_page']

    all_lines = wrap_text(text, CFG['text_width'], glyphs, scale, fallback_advance,
                          letter_spacing=letter_spacing)
    all_lines = adjust_keyword_lines(all_lines, extra_indent=space_w * 2)

    if start_line:
        first_cap = lines_per_page - start_line
        pages = [all_lines[:first_cap]] + split_pages(all_lines[first_cap:], lines_per_page)
    else:
        pages = split_pages(all_lines, lines_per_page)

    # Keep only the last entry to bound memory usage
    _pages_cache.clear()
    _pages_cache[key] = pages
    return pages


def _render_page(pages: list, page_num: int, start_line: int) -> str:
    """Render a single page (1-indexed) to SVG string."""
    if page_num < 1 or page_num > len(pages):
        return ''
    return render_svg(
        pages[page_num - 1],
        FONT_DATA['glyphs'],
        FONT_DATA['font_face'],
        font_size_mm     = CFG['size'],
        line_height_mm   = CFG['line_height'],
        line_gap_mm      = CFG['line_gap'],
        margin_left      = CFG['margin_left'],
        margin_top       = CFG['margin_top'],
        layout_width     = CFG['layout_w'],
        layout_height    = CFG['layout_h'],
        fallback_advance = FONT_DATA['fallback_advance'],
        landscape        = CFG['landscape'],
        line_offset      = start_line if page_num == 1 else 0,
        letter_spacing   = CFG['letter_spacing'],
    )


def _parse_request(*keys):
    """Parse JSON body and return requested keys with defaults."""
    data = request.get_json(force=True)
    return (data.get(k, '') if k == 'text' else int(data.get(k, 0) or 0)
            for k in keys)


def _watch_plot_output(proc):
    """Background thread: capture last stdout line from plot.py."""
    global _plot_last_line
    for raw in proc.stdout:
        line = raw.decode(errors='replace').strip()
        if line:
            _plot_last_line = line


def _ensure_plotting():
    """Return 400 response if no plot is running, else None."""
    if not _plot_proc or _plot_proc.poll() is not None:
        return jsonify({'status': 'error', 'message': 'Not printing'}), 400
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get('/')
def index():
    return render_template('editor.html', cfg=CFG)


@app.post('/layout')
def layout():
    """Fast path: wrap + paginate only. Returns total page count."""
    text, start_line = _parse_request('text', 'start_line')
    pages = _compute_pages(text, start_line)
    return jsonify({
        'total_pages':    len(pages),
        'lines_per_page': CFG['lines_per_page'],
        'total_lines':    sum(len(p) for p in pages),
    })


@app.post('/page')
def page():
    """Render a single page to SVG."""
    text, page_num, start_line = _parse_request('text', 'page_num', 'start_line')
    pages = _compute_pages(text, start_line)
    return jsonify({'svg': _render_page(pages, page_num, start_line),
                    'total_pages': len(pages)})


@app.get('/status')
def status():
    printing = bool(_plot_proc and _plot_proc.poll() is None)
    return jsonify({'printing': printing, 'line': _plot_last_line})


@app.post('/stop')
def stop_plot():
    if err := _ensure_plotting():
        return err
    # subprocess.Popen.send_signal() on Windows only accepts SIGTERM,
    # CTRL_C_EVENT or CTRL_BREAK_EVENT — plain SIGINT raises ValueError there.
    sig = signal.CTRL_BREAK_EVENT if sys.platform == 'win32' else signal.SIGINT
    print(f"[stop] sending {sig!r} to PID {_plot_proc.pid}")
    _plot_proc.send_signal(sig)
    return jsonify({'status': 'stopped'})


@app.post('/pause')
def pause_plot():
    if err := _ensure_plotting():
        return err
    print(f"[pause] sending Enter to PID {_plot_proc.pid}")
    _plot_proc.stdin.write(b'\n')
    _plot_proc.stdin.flush()
    return jsonify({'status': 'ok'})


@app.post('/print')
def print_page():
    """Generate G-code for one page and start plot.py."""
    global _plot_proc

    # Guard before the expensive vpype call
    if _plot_proc and _plot_proc.poll() is None:
        return jsonify({'status': 'error', 'message': 'Already printing'}), 409

    text, page_num, start_line = _parse_request('text', 'page_num', 'start_line')
    pages = _compute_pages(text, start_line)
    if page_num < 1 or page_num > len(pages):
        return jsonify({'status': 'error', 'message': f'Page {page_num} does not exist'}), 400

    svg = _render_page(pages, page_num, start_line)

    tmp_dir    = Path(tempfile.gettempdir())
    svg_path   = tmp_dir / f'editor_page_{page_num:04d}.svg'
    gcode_path = tmp_dir / f'editor_page_{page_num:04d}.gcode'
    svg_path.write_text(svg, encoding='utf-8')

    cmd = ['vpype', '-c', VPYPE_CONFIG,
           'read', str(svg_path),
           'linesort',
           'gwrite', '--profile', 'drawcore', str(gcode_path)]
    print(f"[print] vpype: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[print] vpype ERROR: {result.stderr.strip()}")
        return jsonify({'status': 'error', 'message': result.stderr.strip()}), 500

    plot_cmd = [sys.executable, str(Path(__file__).parent.parent / 'plot.py'), str(gcode_path)]
    print(f"[print] plot.py: {' '.join(plot_cmd)}")
    global _plot_last_line
    _plot_last_line = ''
    # New process group on Windows so CTRL_BREAK_EVENT (sent from /stop) only
    # reaches plot.py, not this Flask server sharing the same console.
    popen_kwargs = {}
    if sys.platform == 'win32':
        popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    _plot_proc = subprocess.Popen(
        plot_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        **popen_kwargs,
    )
    threading.Thread(target=_watch_plot_output, args=(_plot_proc,), daemon=True).start()
    return jsonify({'status': 'printing', 'page': page_num, 'pid': _plot_proc.pid})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Web editor for Hershey pen-plotter text.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('--font', required=True,
                        help='Path to Hershey SVG font (.svg)')
    parser.add_argument('--orientation', choices=['portrait', 'landscape'],
                        default='portrait')
    parser.add_argument('--size',        type=float, default=5.0,   metavar='MM')
    parser.add_argument('--line-height', type=float, default=None,  metavar='MM')
    parser.add_argument('--line-gap',    type=float, default=0.0,   metavar='MM')
    parser.add_argument('--margin-top',    type=float, default=20.0, metavar='MM')
    parser.add_argument('--margin-bottom', type=float, default=20.0, metavar='MM')
    parser.add_argument('--margin-left',   type=float, default=20.0, metavar='MM')
    parser.add_argument('--margin-right',  type=float, default=20.0, metavar='MM')
    parser.add_argument('--page-width',  type=float, default=210.0, metavar='MM')
    parser.add_argument('--page-height', type=float, default=297.0, metavar='MM')
    parser.add_argument('--port', type=int, default=5000,
                        help='HTTP port to listen on')
    args = parser.parse_args()

    if args.line_height is None:
        args.line_height = args.size * 1.5

    landscape = (args.orientation == 'landscape')
    layout_w  = args.page_height if landscape else args.page_width
    layout_h  = args.page_width  if landscape else args.page_height

    glyphs, font_face = parse_svg_font(args.font)
    fallback_advance  = font_face['upm'] * 0.5
    scale             = args.size / font_face['upm']
    letter_spacing    = args.size * 0.1
    space_w = (glyphs[' ']['advance'] if ' ' in glyphs else fallback_advance) * scale + letter_spacing

    FONT_DATA.update({
        'glyphs':           glyphs,
        'font_face':        font_face,
        'fallback_advance': fallback_advance,
        'scale':            scale,
        'space_w':          space_w,
    })

    text_width  = layout_w - args.margin_left - args.margin_right
    text_height = layout_h - args.margin_top  - args.margin_bottom
    advance     = args.line_height + args.line_gap

    CFG.update({
        'font_path':      args.font,
        'orientation':    args.orientation,
        'landscape':      landscape,
        'size':           args.size,
        'line_height':    args.line_height,
        'line_gap':       args.line_gap,
        'margin_top':     args.margin_top,
        'margin_bottom':  args.margin_bottom,
        'margin_left':    args.margin_left,
        'margin_right':   args.margin_right,
        'layout_w':       layout_w,
        'layout_h':       layout_h,
        'text_width':     text_width,
        'text_height':    text_height,
        'letter_spacing': letter_spacing,
        'lines_per_page': int(text_height / advance),
    })

    print(f"Font:  {args.font}  ({len(glyphs)} glyphs)")
    print(f"Page:  {layout_w}×{layout_h} mm ({args.orientation})  "
          f"margins T{args.margin_top} B{args.margin_bottom} "
          f"L{args.margin_left} R{args.margin_right}")
    print(f"Lines per page: {CFG['lines_per_page']}")
    print(f"Open: http://127.0.0.1:{args.port}/")

    app.run(host='127.0.0.1', port=args.port, debug=False)


if __name__ == '__main__':
    main()
