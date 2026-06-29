#!/usr/bin/env python3
"""
imitation/render.py — Generate realistic handwriting PNG for printing.

Simulates pen-on-rough-paper physics:
  - Variable stroke width (curvature → pen pressure → width)
  - Ink blobs at stroke start/end (excess ink pooling)
  - Paper grain via multi-scale noise (white holes where ink skips over paper valleys)
  - Per-glyph baseline jitter

Usage:
    python imitation/render.py text.txt \\
        --font fonts/Caveat_centerline/Caveat_centerline.svg

    python imitation/render.py text.txt \\
        --font fonts/Caveat_centerline/Caveat_centerline.svg \\
        --dpi 300 --stroke-mm 0.4 --grain 0.6 --out-dir imitation/output
"""

import argparse
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import gaussian_filter, gaussian_filter1d
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from text_to_gcode import (
    parse_svg_font,
    render_svg,
    wrap_text,
    split_pages,
    adjust_keyword_lines,
)

MM_PER_INCH = 25.4

# ─────────────────────────────────────────────────────────────────────────────
# SVG path parsing
# ─────────────────────────────────────────────────────────────────────────────

_NUM_RE = re.compile(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?')


def _nums(s: str) -> list[float]:
    return list(map(float, _NUM_RE.findall(s)))


def _sample_bezier(p0, p1, p2, p3, steps: int = 20) -> np.ndarray:
    """Sample `steps` points on a cubic bezier (excluding p0). Returns (steps, 2)."""
    t = np.linspace(0, 1, steps + 1)[1:, None]
    return (1 - t)**3 * p0 + 3 * (1 - t)**2 * t * p1 + \
           3 * (1 - t) * t**2 * p2 + t**3 * p3


def parse_path_d(d: str) -> list[np.ndarray]:
    """Parse SVG path `d` → list of sub-paths.

    Each sub-path is an (N, 2) float64 array in font units.
    Handles M, L, C (absolute). Bezier curves sampled at 20 pts each.
    Multiple M commands produce separate sub-paths (= separate pen strokes).
    """
    parts = re.split(r'(?=[MmLlCcZz])', d.strip())

    sub_paths: list[np.ndarray] = []
    pts: list[list[float]] = []
    cx = cy = 0.0

    for part in parts:
        part = part.strip()
        if not part:
            continue
        cmd = part[0]
        nums = _nums(part[1:])

        if cmd == 'M':
            if pts:
                sub_paths.append(np.array(pts, dtype=np.float64))
            if len(nums) < 2:
                continue
            cx, cy = nums[0], nums[1]
            pts = [[cx, cy]]
            # Remaining pairs treated as implicit L
            for j in range(2, len(nums) - 1, 2):
                cx, cy = nums[j], nums[j + 1]
                pts.append([cx, cy])

        elif cmd == 'L':
            for j in range(0, len(nums) - 1, 2):
                cx, cy = nums[j], nums[j + 1]
                pts.append([cx, cy])

        elif cmd == 'C':
            for j in range(0, len(nums) - 5, 6):
                p0 = np.array([cx,       cy])
                p1 = np.array([nums[j],   nums[j + 1]])
                p2 = np.array([nums[j + 2], nums[j + 3]])
                p3 = np.array([nums[j + 4], nums[j + 5]])
                pts.extend(_sample_bezier(p0, p1, p2, p3).tolist())
                cx, cy = p3[0], p3[1]

        elif cmd in ('Z', 'z'):
            if pts:
                sub_paths.append(np.array(pts, dtype=np.float64))
            pts = []

    if pts:
        sub_paths.append(np.array(pts, dtype=np.float64))

    return sub_paths


# ─────────────────────────────────────────────────────────────────────────────
# SVG transform parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_transform(t: str) -> np.ndarray:
    """Parse compound SVG transform attribute → 3×3 affine matrix (float64).

    Handles translate, scale, matrix.
    Transforms are composed left-to-right as in SVG spec.
    """
    M = np.eye(3, dtype=np.float64)
    for func, args_str in re.findall(r'(\w+)\s*\(([^)]+)\)', t):
        nums = list(map(float, _NUM_RE.findall(args_str)))
        if func == 'translate':
            tx = nums[0]
            ty = nums[1] if len(nums) > 1 else 0.0
            T = np.array([[1, 0, tx], [0, 1, ty], [0, 0, 1]], dtype=np.float64)
        elif func == 'scale':
            sx = nums[0]
            sy = nums[1] if len(nums) > 1 else sx
            T = np.array([[sx, 0, 0], [0, sy, 0], [0, 0, 1]], dtype=np.float64)
        elif func == 'matrix':
            a, b, c, d, e, f = nums
            # SVG matrix(a,b,c,d,e,f): x'=a*x+c*y+e, y'=b*x+d*y+f
            T = np.array([[a, c, e], [b, d, f], [0, 0, 1]], dtype=np.float64)
        else:
            continue
        M = M @ T
    return M


def apply_transform(M: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Apply 3×3 affine matrix M to (N, 2) points array."""
    ones = np.ones((len(pts), 1), dtype=np.float64)
    return (M @ np.hstack([pts, ones]).T).T[:, :2]


# ─────────────────────────────────────────────────────────────────────────────
# Stroke rendering
# ─────────────────────────────────────────────────────────────────────────────

def _curvature(pts: np.ndarray) -> np.ndarray:
    """Approximate signed curvature magnitude at each point via finite differences."""
    if len(pts) < 3:
        return np.ones(len(pts))
    dx  = np.gradient(pts[:, 0])
    dy  = np.gradient(pts[:, 1])
    ddx = np.gradient(dx)
    ddy = np.gradient(dy)
    kappa = np.abs(dx * ddy - dy * ddx) / (dx**2 + dy**2 + 1e-10)**1.5
    return kappa


def _compute_speed(pts_px: np.ndarray) -> np.ndarray:
    """Local pen speed at each sample point (px / step).

    Fast on long straights → less ink → lighter.
    Slow at curves / short segments → more ink → darker.
    Smoothed to mimic plotter motor acceleration/deceleration.
    """
    if len(pts_px) < 2:
        return np.ones(len(pts_px), dtype=np.float32)
    diffs  = np.diff(pts_px, axis=0)
    speeds = np.sqrt((diffs ** 2).sum(axis=1)).astype(np.float32)
    speeds = np.concatenate([[speeds[0]], speeds])
    sigma  = max(2.0, len(speeds) / 8.0)
    return gaussian_filter1d(speeds, sigma=sigma)


def _speed_to_opacity(speeds: np.ndarray,
                      min_op: float = 0.78,
                      max_op: float = 1.00,
                      speed_lo: float = 2.0,
                      speed_hi: float = 12.0) -> np.ndarray:
    """Convert absolute speed to ink opacity using fixed thresholds.

    Uses absolute px/step values — not per-stroke normalization — so that
    short letter strokes (always slow) stay near full opacity, while only
    long straight segments thin out noticeably.

    Below speed_lo:  full ink (max_op).
    Above speed_hi:  thinnest ink (min_op).
    Linear between.
    """
    t = np.clip((speeds - speed_lo) / (speed_hi - speed_lo + 1e-8), 0.0, 1.0)
    return (max_op - t * (max_op - min_op)).astype(np.float32)


def _add_path_noise(pts: np.ndarray, noise_px: float, seed: int) -> np.ndarray:
    """Perturb path points perpendicularly with smooth correlated noise.

    Simulates pen nib vibration + paper surface micro-roughness that makes
    stroke edges uneven (clearly visible in real plotter output).
    """
    n = len(pts)
    if n < 3 or noise_px < 0.05:
        return pts
    dx = np.gradient(pts[:, 0])
    dy = np.gradient(pts[:, 1])
    lengths = np.sqrt(dx ** 2 + dy ** 2) + 1e-8
    perp_x = -dy / lengths
    perp_y =  dx / lengths
    rng    = np.random.default_rng(seed % (2 ** 31))
    raw    = rng.standard_normal(n)
    smooth = gaussian_filter1d(raw, sigma=max(2.0, n / 10.0)) * noise_px
    noisy  = pts.copy()
    noisy[:, 0] += perp_x * smooth
    noisy[:, 1] += perp_y * smooth
    return noisy


def _stroke_widths(pts_px: np.ndarray, base_w: float) -> np.ndarray:
    """Variable stroke width profile from curvature + position along stroke.

    Model:
      - Start of stroke: wider (ink pools as pen touches down)
      - End of stroke:   slightly narrower (pen lifts off)
      - High curvature:  slower movement → more ink → wider
    """
    n = len(pts_px)
    t = np.linspace(0.0, 1.0, n)

    kappa = _curvature(pts_px)
    kappa_norm = kappa / (kappa.max() + 1e-8)

    pressure = (
        1.0
        + 0.20 * np.exp(-t * 6.0)   # touchdown: nib settles onto paper
        - 0.10 * t                    # gradual lift-off taper
        + 0.12 * kappa_norm           # curvature → nib spreads slightly
    )
    return (base_w * pressure.clip(0.55, 1.55)).astype(np.float32)


def _draw_stroke(draw: ImageDraw.ImageDraw,
                 pts_px: np.ndarray,
                 widths: np.ndarray,
                 opacities: np.ndarray | None = None) -> None:
    """Draw a variable-width, variable-opacity stroke.

    opacities: per-point ink opacity in [0..1]. None = full opacity everywhere.
    Speed-based opacity makes fast (straight) segments lighter and grainy,
    slow (curved) segments darker and solid — matching real ballpoint pen behaviour.
    """
    pts = pts_px.astype(np.float64)
    if opacities is None:
        opacities = np.ones(len(pts), dtype=np.float32)

    for (x, y), w, op in zip(pts, widths, opacities):
        r    = max(0.5, float(w) * 0.5)
        fill = max(40, min(255, int(op * 255)))
        draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)

    for i in range(len(pts) - 1):
        p1   = (float(pts[i, 0]),     float(pts[i, 1]))
        p2   = (float(pts[i + 1, 0]), float(pts[i + 1, 1]))
        w    = max(1, int(round((float(widths[i]) + float(widths[i + 1])) * 0.5)))
        fill = max(40, min(255, int((opacities[i] + opacities[i + 1]) * 0.5 * 255)))
        draw.line([p1, p2], fill=fill, width=w)


def _draw_blob(draw: ImageDraw.ImageDraw,
               x: float, y: float, r: float) -> None:
    """Ink blob at pen touchdown: just a slightly wider circle, no halo."""
    draw.ellipse([x - r, y - r, x + r, y + r], fill=255)


def _translate_x(transform_str: str) -> float:
    """Extract tx from translate(tx, ty) — glyph's reading-direction x position (mm)."""
    m = re.search(r'translate\s*\(\s*([-\d.eE+]+)', transform_str)
    return float(m.group(1)) if m else 0.0


def _baseline_key(transform_str: str) -> float:
    """Extract the reading-direction baseline y (mm) for grouping glyphs by line.

    Portrait: ty from translate(tx, ty)
    Landscape: e from matrix(a,b,c,d,e,f)
    """
    m = re.search(r'translate\s*\([^,]+,\s*([-\d.eE+]+)', transform_str)
    if m:
        return round(float(m.group(1)), 1)
    m2 = re.search(r'matrix\s*\(([^)]+)\)', transform_str)
    if m2:
        ns = list(map(float, _NUM_RE.findall(m2.group(1))))
        if len(ns) == 6:
            return round(ns[4], 1)  # e = baseline_y for landscape
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Paper grain texture
# ─────────────────────────────────────────────────────────────────────────────

def _generate_grain(H: int, W: int, seed: int) -> np.ndarray:
    """Generate 0..1 paper surface height map via multi-scale filtered noise.

    1.0 = raised paper fiber (ink deposits normally)
    0.0 = valley between fibers (ink skips → white gap in stroke)

    Three scales:
      fine  (~1.2px) → individual fiber contact points (tiny white dots)
      mid   (~4.5px) → fiber bundles (slightly larger patches)
      coarse(~14px)  → paper surface waviness (slow ink-density drift)
    """
    rng  = np.random.default_rng(seed)
    base = rng.random((H, W)).astype(np.float32)

    fine   = gaussian_filter(base, sigma=1.2)
    mid    = gaussian_filter(base, sigma=4.5)
    coarse = gaussian_filter(base, sigma=14.0)

    grain = 0.50 * fine + 0.35 * mid + 0.15 * coarse
    lo, hi = grain.min(), grain.max()
    return (grain - lo) / (hi - lo + 1e-8)


def _apply_grain(ink: np.ndarray, grain: np.ndarray,
                 strength: float, threshold: float) -> np.ndarray:
    """Uniform grain — used as fallback / base layer."""
    mask = np.clip((grain - threshold) / (1.0 - threshold + 1e-8), 0.0, 1.0)
    return ink * (1.0 - strength * (1.0 - mask))


def _apply_grain_spatial(ink: np.ndarray,
                         grain: np.ndarray,
                         straight_map: np.ndarray,
                         grain_min: float = 0.02,
                         grain_max: float = 0.58,
                         threshold: float = 0.18) -> np.ndarray:
    """Spatially-varying grain driven by a pen-speed proxy map.

    straight_map: 0 = tight curve (slow pen → dense ink → little grain)
                  1 = straight segment (fast pen → thin ink → heavy grain)

    The grain texture (paper surface) determines WHERE holes appear;
    straight_map determines HOW STRONGLY grain removes ink at each location.
    """
    local_strength = grain_min + straight_map * (grain_max - grain_min)
    mask = np.clip((grain - threshold) / (1.0 - threshold + 1e-8), 0.0, 1.0)
    return ink * (1.0 - local_strength * (1.0 - mask))


# ─────────────────────────────────────────────────────────────────────────────
# Page renderer
# ─────────────────────────────────────────────────────────────────────────────

def render_page_image(
    svg_str: str,
    *,
    dpi: int = 300,
    ink_color: tuple[int, int, int] = (5, 6, 12),
    stroke_mm: float = 0.52,
    line_jitter_mm: float = 0.30,
    char_jitter_mm: float = 0.07,
    char_x_jitter_mm: float = 0.15,
    grain_strength: float = 0.32,
    grain_threshold: float = 0.18,
    blob_strength: float = 0.55,
    noise_mm: float = 0.14,
    speed_grain: float = 0.50,
    seed: int = 42,
) -> Image.Image:
    """Render one SVG page to a PIL RGB Image with pen-on-paper simulation.

    svg_str:          output of render_svg() — coordinate units are mm
    dpi:              output resolution
    ink_color:        RGB tuple for pen ink
    stroke_mm:        base stroke width in mm (before pressure modulation)
    line_jitter_mm:   σ of per-LINE baseline drift — whole line shifts together
    char_jitter_mm:   σ of tiny per-glyph vertical variation on top of line drift
    char_x_jitter_mm: σ of per-glyph horizontal jitter → variable inter-letter spacing
    grain_strength:   base grain on curved/slow segments (0=clean, 1=rough)
    grain_threshold:  paper height below which ink does not deposit
    blob_strength:    ink blob radius at glyph start (0=none, 1=large dot)
    noise_mm:         amplitude of perpendicular path noise (stroke edge roughness)
    speed_grain:      extra grain added on straight/fast segments on top of grain_strength
                      (0=no speed effect, 0.8=strong contrast between straights and curves)
    seed:             RNG seed for deterministic output
    """
    rng = np.random.default_rng(seed)

    root = ET.fromstring(svg_str)
    for el in root.iter():
        el.tag = el.tag.split('}')[-1] if '}' in el.tag else el.tag

    def _parse_mm(s: str) -> float:
        return float(re.sub(r'[^\d.\-]', '', s))

    w_mm = _parse_mm(root.get('width',  '210mm'))
    h_mm = _parse_mm(root.get('height', '297mm'))
    W = int(w_mm * dpi / MM_PER_INCH)
    H = int(h_mm * dpi / MM_PER_INCH)

    base_w_px = stroke_mm * dpi / MM_PER_INCH
    blob_r    = base_w_px * 1.5 * blob_strength
    noise_px  = noise_mm * dpi / MM_PER_INCH

    img  = Image.new('L', (W, H), color=0)
    draw = ImageDraw.Draw(img)

    # Curvature accumulation buffer (1/4 resolution for speed).
    # Tracks local pen straightness at each position: 0=tight curve, 1=straight.
    # Used after drawing to build a spatially-varying grain-strength map.
    _DS = 4
    curv_buf = np.zeros((H // _DS + 2, W // _DS + 2), dtype=np.float32)
    curv_cnt = np.zeros_like(curv_buf)
    # Absolute curvature thresholds (px⁻¹): below lo → straight, above hi → curved.
    # At 300 DPI: radius 20px circle → κ=0.05, radius 50px → κ=0.02.
    # Tight range captures real letter-stroke variation (straights vs loops).
    _KAPPA_LO, _KAPPA_HI = 0.002, 0.06

    path_els = root.findall('.//path')

    # Per-line jitter: whole-line vertical offset + slow sinusoidal drift along the line.
    # This simulates two real handwriting effects:
    #   1. Each line sits at a slightly different height (line_drift)
    #   2. The hand rises/falls slowly across a line (line_wave)
    line_keys = {_baseline_key(p.get('transform', '')) for p in path_els}
    line_drift = {k: float(rng.normal(0.0, line_jitter_mm)) for k in line_keys}
    # Slow sinusoidal wave per line: amplitude ~0.15-0.35mm, 1-2 cycles per line
    line_wave: dict[float, tuple[float, float, float]] = {}
    for k in line_keys:
        amp   = float(rng.uniform(0.10, 0.35))      # mm amplitude
        freq  = float(rng.uniform(0.7,  1.8))        # cycles across text width
        phase = float(rng.uniform(0.0,  2 * np.pi))
        line_wave[k] = (amp, freq, phase)

    text_w_mm = w_mm - 40.0  # approximate text area width (good enough for sine period)

    for path_el in path_els:
        d             = path_el.get('d', '')
        transform_str = path_el.get('transform', '')
        if not d:
            continue

        sub_paths = parse_path_d(d)
        M         = parse_transform(transform_str)

        lk      = _baseline_key(transform_str)
        glyph_x = _translate_x(transform_str)

        # Wave drift: slow y rise/fall along the line
        amp, freq, phase = line_wave.get(lk, (0.0, 1.0, 0.0))
        wave_y = amp * np.sin(2 * np.pi * freq * glyph_x / max(text_w_mm, 1) + phase)

        jx_mm = float(rng.normal(0.0, char_x_jitter_mm))
        jy_mm = line_drift.get(lk, 0.0) + wave_y + float(rng.normal(0.0, char_jitter_mm))
        M_j   = np.array([[1, 0, jx_mm], [0, 1, jy_mm], [0, 0, 1]], dtype=np.float64)
        M_eff = M_j @ M

        for idx, sub in enumerate(sub_paths):
            if len(sub) < 2:
                continue

            # Font units → mm (via SVG transform) → pixels
            pts_mm = apply_transform(M_eff, sub)
            pts_px = pts_mm * (dpi / MM_PER_INCH)

            if (pts_px[:, 0].max() < -5 or pts_px[:, 0].min() > W + 5 or
                    pts_px[:, 1].max() < -5 or pts_px[:, 1].min() > H + 5):
                continue

            # Perpendicular path noise → rough stroke edges
            noise_seed = int(abs(pts_px[0, 0] * 97 + pts_px[0, 1] * 137)) % (2 ** 31)
            pts_noisy  = _add_path_noise(pts_px, noise_px, seed=noise_seed)

            # Curvature at each sample point (absolute, px⁻¹)
            kappa = _curvature(pts_noisy)
            # Straightness: 0 = tight curve (slow), 1 = straight (fast)
            straightness = 1.0 - np.clip(
                (kappa - _KAPPA_LO) / (_KAPPA_HI - _KAPPA_LO + 1e-8), 0.0, 1.0
            )

            # Accumulate into downsampled curvature map
            for (x, y), s in zip(pts_noisy, straightness):
                ix = int(round(float(x))) // _DS
                iy = int(round(float(y))) // _DS
                if 0 <= ix < curv_buf.shape[1] and 0 <= iy < curv_buf.shape[0]:
                    curv_buf[iy, ix] += s
                    curv_cnt[iy, ix] += 1

            widths = _stroke_widths(pts_noisy, base_w_px)
            _draw_stroke(draw, pts_noisy, widths)

            if idx == 0 and blob_r > 0.3:
                _draw_blob(draw, float(pts_noisy[0, 0]), float(pts_noisy[0, 1]), blob_r)

    ink = np.asarray(img, dtype=np.float32) / 255.0
    ink = gaussian_filter(ink, sigma=0.55)

    # Build spatially-varying straightness map from accumulated curvature data.
    # Non-stroke pixels: 0 (no contribution), not 0.5 — preserving contrast.
    avg = np.where(curv_cnt > 0, curv_buf / (curv_cnt + 1e-8), 0.0)
    # Tiny blur at 1/4 scale (sigma=0.4 here ≈ 1.6px at full res) — just fill
    # the few-pixel gaps between sample points, without smearing across letters.
    avg = gaussian_filter(avg, sigma=0.4)
    straight_map_small = np.clip(avg, 0.0, 1.0)
    # Upsample to full resolution with minimal extra smoothing
    straight_map = np.array(
        Image.fromarray((straight_map_small * 255).astype(np.uint8))
            .resize((W, H), Image.BILINEAR),
        dtype=np.float32,
    ) / 255.0
    straight_map = gaussian_filter(straight_map, sigma=0.8)
    # Normalize to full [0,1] range so grain contrast is maximized
    _sm_lo, _sm_hi = straight_map.min(), straight_map.max()
    if _sm_hi > _sm_lo + 0.01:
        straight_map = (straight_map - _sm_lo) / (_sm_hi - _sm_lo + 1e-8)

    # Spatially-varying grain: heavy on fast (straight) segments, light on slow (curves).
    # grain_min → curved/slow segments (denser ink, less texture visible)
    # grain_max → straight/fast segments (thinner ink, paper texture shows through)
    grain = _generate_grain(H, W, seed=seed)
    ink   = _apply_grain_spatial(ink, grain, straight_map,
                                 grain_min=grain_strength * 0.10,
                                 grain_max=min(grain_strength + speed_grain, 0.95),
                                 threshold=grain_threshold)
    ink   = ink.clip(0.0, 1.0)

    paper = np.array([255.0, 255.0, 255.0])
    ink_c = np.array(ink_color, dtype=np.float64)
    rgb   = paper + ink[:, :, None] * (ink_c - paper)
    return Image.fromarray(rgb.clip(0, 255).astype(np.uint8), mode='RGB')


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description='Render handwriting-style PNG from text using a centerline SVG font.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # ── Text layout (mirrors text_to_gcode.py) ────────────────────────────
    ap.add_argument('input', help='Input text file (UTF-8)')
    ap.add_argument('--font', required=True, help='Centerline Hershey SVG font (.svg)')
    ap.add_argument('--size',        type=float, default=5.0,  metavar='MM', help='Font em-size in mm')
    ap.add_argument('--line-height', type=float, default=None, metavar='MM', help='Baseline-to-baseline distance (default: size×1.5)')
    ap.add_argument('--line-gap',    type=float, default=0.0,  metavar='MM', help='Extra leading between lines')
    ap.add_argument('--margin-top',    type=float, default=20.0, metavar='MM')
    ap.add_argument('--margin-bottom', type=float, default=20.0, metavar='MM')
    ap.add_argument('--margin-left',   type=float, default=20.0, metavar='MM')
    ap.add_argument('--margin-right',  type=float, default=20.0, metavar='MM')
    ap.add_argument('--page-width',  type=float, default=210.0, metavar='MM')
    ap.add_argument('--page-height', type=float, default=297.0, metavar='MM')
    ap.add_argument('--orientation', choices=['portrait', 'landscape'], default='portrait')
    ap.add_argument('--out-dir', default='imitation/output', metavar='DIR', help='Output directory')
    ap.add_argument('--prefix', default='page', help='Output filename prefix')
    ap.add_argument('--page', type=int, default=None, metavar='N', help='Render only page N (1-indexed)')

    # ── Pen simulation ────────────────────────────────────────────────────
    ap.add_argument('--dpi',             type=int,   default=300,  help='Output resolution in DPI')
    ap.add_argument('--stroke-mm',       type=float, default=0.52, metavar='MM',   help='Base stroke width in mm')
    ap.add_argument('--line-jitter-mm',  type=float, default=0.30, metavar='MM',   help='Per-line baseline drift σ (whole line shifts together)')
    ap.add_argument('--char-jitter-mm',  type=float, default=0.07, metavar='MM',   help='Per-glyph tiny variation σ on top of line drift')
    ap.add_argument('--grain',           type=float, default=0.32, metavar='0..1', help='Paper grain strength (0=clean, 1=rough)')
    ap.add_argument('--grain-threshold', type=float, default=0.18, metavar='0..1', help='Paper height below which ink does not deposit')
    ap.add_argument('--blob',             type=float, default=0.55, metavar='0..1', help='Ink blob radius at glyph start (0=none, 1=large dot)')
    ap.add_argument('--noise-mm',         type=float, default=0.14, metavar='MM',   help='Stroke edge roughness amplitude in mm')
    ap.add_argument('--char-x-jitter-mm', type=float, default=0.15, metavar='MM',   help='Per-glyph horizontal jitter σ → variable inter-letter spacing')
    ap.add_argument('--speed-grain',      type=float, default=0.50, metavar='0..1',
                    help='Extra grain on straight/fast segments (0=uniform grain, 0.8=strong speed contrast)')
    ap.add_argument('--ink-color', default='5,6,12', metavar='R,G,B',
                    help='Ink RGB color, e.g. "15,15,28" (dark navy) or "40,30,20" (dark sepia)')
    ap.add_argument('--seed', type=int, default=42, help='RNG seed for reproducible output')

    args = ap.parse_args()

    if args.line_height is None:
        args.line_height = args.size * 1.5

    ink_color = tuple(int(x) for x in args.ink_color.split(','))
    landscape = args.orientation == 'landscape'

    if landscape:
        layout_w = args.page_height
        layout_h = args.page_width
    else:
        layout_w = args.page_width
        layout_h = args.page_height

    # ── Font & text layout ────────────────────────────────────────────────
    glyphs, font_face = parse_svg_font(args.font)
    fallback_advance  = font_face['upm'] * 0.5
    scale             = args.size / font_face['upm']
    letter_spacing    = args.size * 0.1

    with open(args.input, encoding='utf-8') as f:
        text = f.read()

    text_width     = layout_w - args.margin_left - args.margin_right
    text_height    = layout_h - args.margin_top  - args.margin_bottom
    advance        = args.line_height + args.line_gap
    lines_per_page = max(1, int(text_height / advance))

    all_lines = wrap_text(text, text_width, glyphs, scale, fallback_advance,
                          letter_spacing=letter_spacing)
    space_w = (glyphs[' ']['advance'] if ' ' in glyphs else fallback_advance) * scale + letter_spacing
    all_lines = adjust_keyword_lines(all_lines, extra_indent=space_w * 2)
    pages = split_pages(all_lines, lines_per_page)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Font:   {args.font}  size={args.size}mm  dpi={args.dpi}")
    print(f"Page:   {layout_w}×{layout_h} mm  {lines_per_page} lines/page")
    print(f"Render: stroke={args.stroke_mm}mm  grain={args.grain}  speed-grain={args.speed_grain}  "
          f"blob={args.blob}  line-jitter={args.line_jitter_mm}mm  x-jitter={args.char_x_jitter_mm}mm  ink={ink_color}")
    print(f"Pages:  {len(pages)}")

    for page_num, page_lines in enumerate(pages, 1):
        if args.page is not None and page_num != args.page:
            continue

        svg_str = render_svg(
            page_lines, glyphs, font_face,
            font_size_mm     = args.size,
            line_height_mm   = args.line_height,
            line_gap_mm      = args.line_gap,
            margin_left      = args.margin_left,
            margin_top       = args.margin_top,
            layout_width     = layout_w,
            layout_height    = layout_h,
            fallback_advance = fallback_advance,
            landscape        = landscape,
            letter_spacing   = letter_spacing,
        )

        print(f"  [{page_num}/{len(pages)}] rendering...", end=' ', flush=True)
        img = render_page_image(
            svg_str,
            dpi               = args.dpi,
            ink_color         = ink_color,
            stroke_mm         = args.stroke_mm,
            line_jitter_mm    = args.line_jitter_mm,
            char_jitter_mm    = args.char_jitter_mm,
            char_x_jitter_mm  = args.char_x_jitter_mm,
            grain_strength    = args.grain,
            grain_threshold   = args.grain_threshold,
            blob_strength     = args.blob,
            noise_mm          = args.noise_mm,
            speed_grain       = args.speed_grain,
            seed              = args.seed + page_num,
        )

        out_path = out_dir / f"{args.prefix}_{page_num:04d}.png"
        img.save(str(out_path), dpi=(args.dpi, args.dpi))
        print(f"→ {out_path}  ({img.width}×{img.height} px)")


if __name__ == '__main__':
    main()
