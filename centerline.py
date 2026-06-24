"""
centerline.py — Convert outline font glyphs to single-stroke (centerline) paths.

Pipeline per glyph:
  1. Render glyph bitmap with FreeType at high resolution
  2. Skeletonize (Zhang-Suen thinning) with scikit-image
  3. Trace skeleton pixels into polyline strokes via graph traversal
  4. Simplify polylines with Ramer-Douglas-Peucker
  5. Fit cubic Bézier curves through the simplified points
  6. Write per-glyph SVG files + a combined specimen SVG
  7. Write a single-stroke UFO font (openable in FontForge / RoboFont)

Usage:
    python3 centerline.py myfont.otf
    python3 centerline.py myfont.otf --glyphs "ABCаб" --render-size 400
"""

import sys
import os
import json
import argparse
import math
import collections
import numpy as np
import freetype
from skimage.morphology import skeletonize
from fontTools.ttLib import TTFont


# ---------------------------------------------------------------------------
# 1.  Render a single glyph with FreeType → binary image
# ---------------------------------------------------------------------------

def render_glyph_bitmap(face: freetype.Face, char: str, size_px: int) -> tuple[np.ndarray, dict]:
    """Return (binary_image, metrics).

    binary_image: H×W bool array, True = ink
    metrics: dict with advance, bearingX, bearingY in pixels
    """
    face.set_pixel_sizes(0, size_px)
    face.load_char(char, freetype.FT_LOAD_RENDER | freetype.FT_LOAD_TARGET_MONO)
    bm = face.glyph.bitmap
    if bm.width == 0 or bm.rows == 0:
        return np.zeros((size_px, size_px), dtype=bool), {}

    # FreeType MONO bitmap: 1 bit per pixel packed in bytes
    buf = np.frombuffer(bytes(bm.buffer), dtype=np.uint8)
    if bm.pixel_mode == freetype.FT_PIXEL_MODE_MONO:
        # unpack bits
        img = np.unpackbits(buf).reshape(bm.rows, bm.pitch * 8)[:, : bm.width]
    else:
        img = buf.reshape(bm.rows, bm.width)
        img = (img > 127).astype(np.uint8)

    binary = img.astype(bool)
    metrics = {
        "advance_x": face.glyph.advance.x >> 6,
        "bearing_x": face.glyph.bitmap_left,
        "bearing_y": face.glyph.bitmap_top,
        "width":     bm.width,
        "height":    bm.rows,
    }
    return binary, metrics


# ---------------------------------------------------------------------------
# 2.  Skeletonize
# ---------------------------------------------------------------------------

def thin(binary: np.ndarray, spur_size: int = 12, min_component: int = 10) -> np.ndarray:
    """Skeletonize + remove tiny isolated components + prune short spur artefacts.

    For thin fonts (stroke half-width < 3 px) a Gaussian pre-blur is applied
    automatically to suppress staircase artefacts that otherwise create hundreds
    of spurious junction pixels in the skeleton.

    min_component: remove skeleton components smaller than N pixels after pruning.
    spur_size:     prune dead-end branches shorter than N pixels.
    """
    from skimage.morphology import remove_small_objects
    from scipy.ndimage import gaussian_filter, distance_transform_edt

    # Auto-detect stroke width and blur if strokes are very thin
    if binary.any():
        mean_hw = distance_transform_edt(binary)[binary].mean()
    else:
        mean_hw = 99.0
    if mean_hw < 3.5:
        # Thin font: smooth before skeletonising to suppress staircase junctions
        sigma = min(2.5, max(0.8, 3.5 - mean_hw))
        smoothed = gaussian_filter(binary.astype(np.float32), sigma=sigma)
        binary = smoothed > 0.3

    skel = skeletonize(binary)
    skel = _prune_spurs(skel, max_spur=spur_size)
    # Remove tiny fragments left after spur pruning
    # max_size removes components with size ≤ value (skimage ≥ 0.26 API)
    skel = remove_small_objects(skel, max_size=min_component - 1, connectivity=2)
    skel = _close_skeleton_gaps(skel, max_gap=3)
    return skel


# ---------------------------------------------------------------------------
# 3.  Skeleton → strokes  (Euler-path algorithm, minimum pen lifts)
# ---------------------------------------------------------------------------
#
# Theory: the skeleton is a planar graph. The minimum number of strokes needed
# to draw it equals the number of odd-degree vertices / 2  (Euler's theorem).
# We achieve this by:
#   1. Compressing degree-2 "chain" pixels into single graph edges.
#   2. Pairing odd-degree nodes greedily (adds virtual pen-up moves).
#   3. Finding an Euler circuit/path with Hierholzer's algorithm.
# ---------------------------------------------------------------------------

def _prune_spurs(skel: np.ndarray, max_spur: int = 8) -> np.ndarray:
    """Remove short dead-end branches ("spurs") from a skeleton image.

    Iteratively deletes endpoint pixels whose chain to the nearest junction
    is shorter than max_spur pixels. Stops when no more spurs are found.
    This eliminates rasterisation/skeletonisation artefacts on curves like 'O'.
    """
    skel = skel.copy()
    H, W = skel.shape

    def _deg(r, c, sk):
        return sum(1 for dr in (-1, 0, 1) for dc in (-1, 0, 1)
                   if (dr, dc) != (0, 0)
                   and 0 <= r+dr < H and 0 <= c+dc < W
                   and sk[r+dr, c+dc])

    changed = True
    while changed:
        changed = False
        rows, cols = np.where(skel)
        for r, c in zip(rows.tolist(), cols.tolist()):
            if not skel[r, c]:
                continue
            if _deg(r, c, skel) != 1:
                continue
            # Trace spur from this endpoint
            spur = [(r, c)]
            prev, cur = None, (r, c)
            while len(spur) <= max_spur:
                nexts = [(r2, c2)
                         for dr in (-1, 0, 1) for dc in (-1, 0, 1)
                         if (dr, dc) != (0, 0)
                         for r2, c2 in [(cur[0]+dr, cur[1]+dc)]
                         if 0 <= r2 < H and 0 <= c2 < W
                         and skel[r2, c2] and (r2, c2) != prev]
                if len(nexts) != 1:
                    break
                prev = cur
                cur = nexts[0]
                spur.append(cur)
            # Only prune if spur ended at a junction (degree ≥ 3), not another endpoint
            end_deg = _deg(*cur, skel)
            if len(spur) <= max_spur and end_deg >= 3:
                for p in spur:
                    skel[p] = False
                changed = True
    return skel


def _close_skeleton_gaps(skel: np.ndarray, max_gap: int = 3) -> np.ndarray:
    """Bridge pairs of skeleton endpoints that are within max_gap pixels.

    Tiny gaps (1-3 px) between skeleton endpoints are common skeletonisation
    artefacts — e.g., the oval of letter 'а' may have a 2-pixel break.
    Bridging them reduces unnecessary pen lifts.
    """
    skel = skel.copy()
    H, W = skel.shape

    while True:
        pts = set(zip(*np.where(skel)))

        def _deg(r, c):
            return sum(1 for dr in (-1, 0, 1) for dc in (-1, 0, 1)
                       if (dr, dc) != (0, 0) and (r+dr, c+dc) in pts)

        endpoints = [(r, c) for (r, c) in pts if _deg(r, c) == 1]

        bridged = False
        for i, (r1, c1) in enumerate(endpoints):
            for r2, c2 in endpoints[i+1:]:
                d = math.hypot(r1-r2, c1-c2)
                if 0 < d <= max_gap:
                    steps = max(abs(r2-r1), abs(c2-c1))
                    for t in range(1, steps):  # endpoints already set
                        r = round(r1 + t / steps * (r2-r1))
                        c = round(c1 + t / steps * (c2-c1))
                        if 0 <= r < H and 0 <= c < W:
                            skel[r, c] = True
                    bridged = True
                    break
            if bridged:
                break

        if not bridged:
            break

    return skel


def skeleton_to_strokes(skel: np.ndarray, min_px: int = 3) -> list[list[tuple[int, int]]]:
    """Return minimum-pen-lift strokes covering all skeleton pixels."""

    rows, cols = np.where(skel)
    pts_set: set = set(zip(rows.tolist(), cols.tolist()))
    if not pts_set:
        return []

    H, W = skel.shape

    def nbrs(r, c):
        return [(r+dr, c+dc)
                for dr in (-1, 0, 1) for dc in (-1, 0, 1)
                if (dr, dc) != (0, 0) and (r+dr, c+dc) in pts_set]

    deg = {p: len(nbrs(*p)) for p in pts_set}

    # Key nodes: endpoints (deg=1), junctions (deg≥3), isolated (deg=0).
    # Degree-2 pixels are "interior" chain pixels — compressed into edges.
    key_nodes: frozenset = frozenset(p for p, d in deg.items() if d != 2)

    # ── Phase 1: build compressed edge list ───────────────────────────────
    # Track visited INTERIOR (degree-2) pixels — not edges.
    # This prevents the "blocked chain" bug where tracing one direction
    # marks edges that block tracing from the other direction.
    visited_interior: set = set()

    def chain_len(path):
        return sum(math.hypot(path[i+1][0]-path[i][0],
                              path[i+1][1]-path[i][1])
                   for i in range(len(path)-1))

    def trace_chain(start, second):
        """Trace from key node 'start' through 'second' until another key node."""
        # Interior pixel already claimed by another chain → skip
        if second not in key_nodes and second in visited_interior:
            return None
        path = [start, second]
        if second not in key_nodes:
            visited_interior.add(second)
        prev, cur = start, second
        while cur not in key_nodes:
            candidates = [n for n in nbrs(*cur)
                          if n != prev
                          and (n in key_nodes or n not in visited_interior)]
            if not candidates:
                break
            nxt = candidates[0]
            path.append(nxt)
            if nxt not in key_nodes:
                visited_interior.add(nxt)
            prev, cur = cur, nxt
        return path

    chains = []

    for node in sorted(key_nodes):
        for nxt in sorted(nbrs(*node)):
            ch = trace_chain(node, nxt)
            if ch is not None and chain_len(ch) >= min_px:
                chains.append(ch)

    # Isolated degree-2 loops (no key nodes in the component)
    for p in sorted(pts_set):
        if p in key_nodes or p in visited_interior:
            continue
        for nxt in sorted(nbrs(*p)):
            if nxt not in visited_interior:
                ch = trace_chain(p, nxt)
                if ch is not None and chain_len(ch) >= min_px:
                    chains.append(ch)

    if not chains:
        return []

    # ── Phase 1.5: contract fat-junction clusters ──────────────────────────
    # Junction pixels that are directly adjacent in the skeleton form "fat"
    # clusters (e.g., 3-5 pixels) instead of the ideal single junction pixel.
    # Each extra pixel adds odd-degree nodes → extra required pen lifts.
    # Fix: merge clusters of adjacent key-nodes connected by micro-chains
    # (≤ MAX_CLUSTER_DIST px) into a single representative node.
    MAX_CLUSTER_DIST = 3.0

    uf_parent: dict = {n: n for n in key_nodes}

    def uf_find(n):
        while uf_parent[n] != n:
            uf_parent[n] = uf_parent[uf_parent[n]]
            n = uf_parent[n]
        return n

    def uf_union(a, b):
        ra, rb = uf_find(a), uf_find(b)
        if ra != rb:
            uf_parent[rb] = ra

    # Merge key nodes within MAX_CLUSTER_DIST of each other — these are
    # "fat junction" pixels that represent one topological junction.
    # NOTE: cannot use `chains` here because micro-chains (< min_px) are
    # already filtered out; use direct pixel-distance instead.
    kn_list = sorted(key_nodes)
    for i, a in enumerate(kn_list):
        for b in kn_list[i+1:]:
            if math.hypot(a[0]-b[0], a[1]-b[1]) <= MAX_CLUSTER_DIST:
                uf_union(a, b)

    rep: dict = {n: uf_find(n) for n in key_nodes}

    # Drop intra-cluster micro-chains; keep external chains (remap endpoints).
    # IMPORTANT: update path pixel endpoints to the representative pixels so
    # that the Euler reconstruction (which checks path[0]==prev_node) works.
    contracted: list = []
    for ch in chains:
        u = rep.get(ch[0], ch[0])
        v = rep.get(ch[-1], ch[-1])
        if u == v and chain_len(ch) <= MAX_CLUSTER_DIST:
            continue   # purely intra-cluster, discard
        # Rebuild path with representative pixels at start/end so that
        # path[0]==u and path[-1]==v (required by Euler reconstruction).
        start_interior = [] if u == ch[0] else [ch[0]]
        end_interior   = [] if v == ch[-1] else [ch[-1]]
        new_path = [u] + start_interior + list(ch[1:-1]) + end_interior + [v]
        # Drop short self-loops that appear AFTER contraction (skeleton
        # "bubbles": two junction pixels bridged by a tiny degree-2 cycle).
        # Legitimate loops (dots '.' / 'ї') are always ≥ 10 px; bubbles ≤ 8.
        SMALL_BUBBLE_PX = 8
        if u == v and chain_len(ch) <= SMALL_BUBBLE_PX:
            continue
        contracted.append((u, v, new_path))
    chains_uv = contracted   # list of (u, v, pixel_path)

    # ── Phase 2: build multigraph ──────────────────────────────────────────
    edges: list[dict] = []          # {u, v, path, used, virtual}
    node_adj: dict = {}             # node → [edge_idx, ...]

    def add_edge(u, v, path, virtual=False):
        ei = len(edges)
        edges.append({'u': u, 'v': v, 'path': path,
                      'used': False, 'virtual': virtual})
        node_adj.setdefault(u, []).append(ei)
        if u != v:
            node_adj.setdefault(v, []).append(ei)
        return ei

    for u, v, ch in chains_uv:
        add_edge(u, v, ch)

    # ── Phase 3: Euler path per connected component ────────────────────────
    all_nodes = set(node_adj.keys())
    visited_graph: set = set()
    result: list = []

    def find_component(start):
        comp: set = set()
        stack = [start]
        while stack:
            n = stack.pop()
            if n in comp:
                continue
            comp.add(n)
            for ei in node_adj.get(n, []):
                e = edges[ei]
                other = e['v'] if e['u'] == n else e['u']
                if other not in comp:
                    stack.append(other)
        return comp

    def euler_for_component(comp):
        # Degree of each node
        local_deg: dict = {n: 0 for n in comp}
        comp_eids: set = set()
        for n in comp:
            for ei in node_adj.get(n, []):
                comp_eids.add(ei)
        for ei in comp_eids:
            e = edges[ei]
            local_deg[e['u']] += 1
            if e['u'] != e['v']:
                local_deg[e['v']] += 1

        odd = [n for n in comp if local_deg[n] % 2 == 1]

        # Pair odd-degree nodes → virtual pen-up edges
        virt_eids: list = []
        remaining = list(odd)
        while len(remaining) >= 2:
            u = remaining.pop(0)
            best = min(remaining,
                       key=lambda v: math.hypot(u[0]-v[0], u[1]-v[1]))
            remaining.remove(best)
            virt_eids.append(add_edge(u, best, None, virtual=True))

        # Reset used flags
        for ei in comp_eids | set(virt_eids):
            edges[ei]['used'] = False

        # Start from an odd node so we get a path (not forced circuit).
        # Prefer a low-degree node (degree-1 = real endpoint) over a junction:
        # starting from a junction tends to produce an extra pen-lift segment.
        start = (min(odd, key=lambda n: local_deg[n]) if odd
                 else next(iter(comp)))

        # Hierholzer's: stack stores (node, incoming_edge_idx)
        stack = [(start, None)]
        circuit: list = []

        while stack:
            v = stack[-1][0]
            found_ei = None
            for ei in node_adj.get(v, []):
                if not edges[ei]['used']:
                    found_ei = ei
                    break
            if found_ei is None:
                circuit.append(stack.pop())
            else:
                edges[found_ei]['used'] = True
                e = edges[found_ei]
                other = e['v'] if e['u'] == v else e['u']
                stack.append((other, found_ei))

        circuit.reverse()

        # Reconstruct pixel strokes from circuit
        strokes: list = []
        cur_stroke: list = []

        for i in range(1, len(circuit)):
            cur_node, ei = circuit[i]
            prev_node   = circuit[i-1][0]
            e = edges[ei]

            if e['virtual'] or e['path'] is None:
                # Pen up
                if len(cur_stroke) >= 2:
                    strokes.append(cur_stroke)
                cur_stroke = []
            else:
                path = e['path']
                # Orient so that path[0] == prev_node
                if e['u'] != e['v'] and path[0] != prev_node:
                    path = list(reversed(path))
                if not cur_stroke:
                    cur_stroke = list(path)
                else:
                    cur_stroke.extend(path[1:])  # skip duplicate junction pixel

        if len(cur_stroke) >= 2:
            strokes.append(cur_stroke)

        # Remove virtual edges from node_adj so they don't pollute other components
        for ei in virt_eids:
            e = edges[ei]
            for n in (e['u'], e['v']):
                adj_list = node_adj.get(n, [])
                if ei in adj_list:
                    adj_list.remove(ei)

        return strokes

    for start in sorted(all_nodes):
        if start in visited_graph:
            continue
        comp = find_component(start)
        visited_graph |= comp
        result.extend(euler_for_component(comp))

    return result


def order_strokes(strokes: list[list]) -> list[list]:
    """Greedy nearest-neighbour ordering to minimise pen-up travel between components."""
    if not strokes:
        return strokes
    remaining = list(strokes)
    ordered = [remaining.pop(0)]
    while remaining:
        cur_end = ordered[-1][-1]
        best_idx, best_dist, best_flip = 0, float("inf"), False
        for i, s in enumerate(remaining):
            d0 = math.hypot(cur_end[0]-s[0][0],  cur_end[1]-s[0][1])
            d1 = math.hypot(cur_end[0]-s[-1][0], cur_end[1]-s[-1][1])
            if d0 <= d1 and d0 < best_dist:
                best_idx, best_dist, best_flip = i, d0, False
            elif d1 < d0 and d1 < best_dist:
                best_idx, best_dist, best_flip = i, d1, True
        nxt = remaining.pop(best_idx)
        ordered.append(list(reversed(nxt)) if best_flip else nxt)
    return ordered


# ---------------------------------------------------------------------------
# 4.  Ramer-Douglas-Peucker simplification
# ---------------------------------------------------------------------------

def _perp_dist(pt, a, b):
    """Perpendicular distance from pt to line segment a→b."""
    ax, ay = a
    bx, by = b
    px, py = pt
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def rdp(points: list, epsilon: float) -> list:
    if len(points) < 3:
        return points
    dmax, idx = 0.0, 0
    for i in range(1, len(points) - 1):
        d = _perp_dist(points[i], points[0], points[-1])
        if d > dmax:
            dmax, idx = d, i
    if dmax > epsilon:
        left  = rdp(points[:idx + 1], epsilon)
        right = rdp(points[idx:],      epsilon)
        return left[:-1] + right
    return [points[0], points[-1]]


# ---------------------------------------------------------------------------
# 5.  Fit cubic Bézier spline (Catmull-Rom → Bézier conversion)
# ---------------------------------------------------------------------------

def catmull_to_bezier(pts: list[tuple]) -> list[tuple]:
    """Convert a list of points to cubic Bézier segments via Catmull-Rom.

    Returns [(P0, CP1, CP2, P3), ...] — one segment per pair of adjacent pts.
    """
    if len(pts) < 2:
        return []
    # Pad endpoints to get tangents at start/end
    ext = [pts[0]] + list(pts) + [pts[-1]]
    segs = []
    tension = 0.5
    for i in range(1, len(ext) - 2):
        p0, p1, p2, p3 = ext[i-1], ext[i], ext[i+1], ext[i+2]
        cp1 = (p1[0] + (p2[0] - p0[0]) * tension / 3,
               p1[1] + (p2[1] - p0[1]) * tension / 3)
        cp2 = (p2[0] - (p3[0] - p1[0]) * tension / 3,
               p2[1] - (p3[1] - p1[1]) * tension / 3)
        segs.append((p1, cp1, cp2, p2))
    return segs


# ---------------------------------------------------------------------------
# 6.  Coordinate transform: image pixels → font units
# ---------------------------------------------------------------------------

def pixel_coords_to_font(
    strokes_px: list[list[tuple[int, int]]],
    metrics: dict,
    size_px: int,
    upm: int,
    rdp_eps: float = 1.5,
) -> list[list[tuple[float, float]]]:
    """
    FreeType places glyph at (bearing_x, size_px - bearing_y) in image coords.
    We flip Y (image Y grows down, font Y grows up).
    Result is in font units (0..upm).
    """
    scale = upm / size_px
    bx = metrics.get("bearing_x", 0)
    by = metrics.get("bearing_y", size_px)

    font_strokes = []
    for stroke in strokes_px:
        if len(stroke) < 2:
            continue
        pts = [(c + bx, by - r) for (r, c) in stroke]   # flip Y; x = col + bitmap_left
        pts_f = [(x * scale, y * scale) for (x, y) in pts]
        simplified = rdp(pts_f, epsilon=rdp_eps * scale)
        if len(simplified) >= 2:
            font_strokes.append(simplified)

    font_strokes = order_strokes(font_strokes)
    return font_strokes


# ---------------------------------------------------------------------------
# 7.  Write SVG
# ---------------------------------------------------------------------------

def strokes_to_svg_paths(strokes_font: list[list[tuple]], stroke_width: float = 10) -> str:
    """Return SVG <path> elements for the given strokes (font-unit coords)."""
    parts = []
    for pts in strokes_font:
        if len(pts) < 2:
            continue
        segs = catmull_to_bezier(pts)
        if not segs:
            continue
        d = f"M {pts[0][0]:.2f},{pts[0][1]:.2f}"
        for (p0, cp1, cp2, p3) in segs:
            d += (f" C {cp1[0]:.2f},{cp1[1]:.2f}"
                  f" {cp2[0]:.2f},{cp2[1]:.2f}"
                  f" {p3[0]:.2f},{p3[1]:.2f}")
        parts.append(
            f'<path d="{d}" fill="none" stroke="black"'
            f' stroke-width="{stroke_width}" stroke-linecap="round"'
            f' stroke-linejoin="round"/>'
        )
    return "\n  ".join(parts)


def write_glyph_svg(
    path: str,
    char: str,
    strokes_font: list[list[tuple]],
    advance: int,
    upm: int,
):
    margin = upm * 0.1
    viewbox_w = advance + 2 * margin
    viewbox_h = upm * 1.2
    # Flip Y for SVG (SVG Y grows down, font Y grows up)
    transform = f"translate({margin:.1f},{upm:.1f}) scale(1,-1)"
    path_data = strokes_to_svg_paths(strokes_font)
    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     viewBox="0 0 {viewbox_w:.1f} {viewbox_h:.1f}"
     width="{viewbox_w/10:.1f}" height="{viewbox_h/10:.1f}">
  <g transform="{transform}">
  {path_data}
  </g>
  <text x="2" y="{viewbox_h - 4:.0f}" font-size="10" fill="#aaa">{repr(char)}</text>
</svg>
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)


def write_specimen_svg(
    path: str,
    glyphs_data: list[dict],
    upm: int,
    cols: int = 16,
):
    """Write a combined specimen SVG with all glyphs laid out on a grid."""
    cell_w = upm * 1.2
    cell_h = upm * 1.5
    rows = math.ceil(len(glyphs_data) / cols)
    total_w = cell_w * cols
    total_h = cell_h * rows

    cells = []
    for idx, gd in enumerate(glyphs_data):
        col = idx % cols
        row = idx // cols
        ox = col * cell_w + cell_w * 0.1
        oy = row * cell_h + upm * 0.2
        # flip Y within cell
        transform = f"translate({ox:.1f},{oy + upm:.1f}) scale(1,-1)"
        path_data = strokes_to_svg_paths(gd["strokes"])
        char_repr = gd["char"].replace("&", "&amp;").replace("<", "&lt;")
        cells.append(
            f'<g transform="{transform}">{path_data}</g>'
            f'<text x="{ox:.1f}" y="{oy + upm * 1.35:.1f}"'
            f' font-size="{upm*0.09:.0f}" fill="#999" text-anchor="middle"'
            f' transform="translate({cell_w*0.5:.1f},0)">{char_repr}</text>'
        )

    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     viewBox="0 0 {total_w:.1f} {total_h:.1f}"
     width="{total_w/5:.0f}" height="{total_h/5:.0f}">
  <rect width="100%" height="100%" fill="white"/>
  {"".join(cells)}
</svg>
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)


# ---------------------------------------------------------------------------
# 8.  Write single-stroke UFO font
# ---------------------------------------------------------------------------

def write_ufo(out_dir: str, glyphs_data: list[dict], upm: int, font_name: str):
    """Write a minimal UFO 3 directory that can be opened in FontForge/RoboFont."""

    ufo_path = os.path.join(out_dir, font_name + "_centerline.ufo")
    os.makedirs(ufo_path, exist_ok=True)
    glyphs_dir = os.path.join(ufo_path, "glyphs")
    os.makedirs(glyphs_dir, exist_ok=True)

    # lib.plist
    with open(os.path.join(ufo_path, "lib.plist"), "w") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>public.glyphOrder</key>
  <array>
    {"".join(f"<string>{gd['name']}</string>" for gd in glyphs_data)}
  </array>
</dict>
</plist>
""")

    # metainfo.plist
    with open(os.path.join(ufo_path, "metainfo.plist"), "w") as f:
        f.write("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>creator</key>
  <string>centerline.py</string>
  <key>formatVersion</key>
  <integer>3</integer>
</dict>
</plist>
""")

    # fontinfo.plist
    with open(os.path.join(ufo_path, "fontinfo.plist"), "w") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>familyName</key>
  <string>{font_name} Centerline</string>
  <key>unitsPerEm</key>
  <integer>{upm}</integer>
  <key>ascender</key>
  <integer>{int(upm * 0.8)}</integer>
  <key>descender</key>
  <integer>{int(upm * -0.2)}</integer>
  <key>xHeight</key>
  <integer>{int(upm * 0.5)}</integer>
  <key>capHeight</key>
  <integer>{int(upm * 0.7)}</integer>
</dict>
</plist>
""")

    contents = {}
    for gd in glyphs_data:
        safe_name = gd["name"].replace("/", "_slash_")
        glif_name = safe_name + ".glif"
        contents[gd["name"]] = glif_name

        # Build GLIF XML with open contours (single-stroke)
        contours_xml = []
        for stroke in gd["strokes"]:
            if len(stroke) < 2:
                continue
            segs = catmull_to_bezier(stroke)
            if not segs:
                continue
            pts_xml = []
            # First on-curve point
            p0 = segs[0][0]
            pts_xml.append(f'      <point x="{p0[0]:.2f}" y="{p0[1]:.2f}" type="move"/>')
            for (_, cp1, cp2, p3) in segs:
                pts_xml.append(
                    f'      <point x="{cp1[0]:.2f}" y="{cp1[1]:.2f}"/>'
                    f'\n      <point x="{cp2[0]:.2f}" y="{cp2[1]:.2f}"/>'
                    f'\n      <point x="{p3[0]:.2f}" y="{p3[1]:.2f}" type="curve"/>'
                )
            contours_xml.append(
                "    <contour>\n"
                + "\n".join(pts_xml)
                + "\n    </contour>"
            )

        # Unicode
        unicode_vals = ""
        if gd.get("codepoint"):
            unicode_vals = f'  <unicode hex="{gd["codepoint"]:04X}"/>\n'

        glif = f"""<?xml version="1.0" encoding="UTF-8"?>
<glyph name="{gd['name']}" format="2">
  <advance width="{gd['advance']:.0f}"/>
{unicode_vals}  <outline>
{"".join(contours_xml)}
  </outline>
</glyph>
"""
        with open(os.path.join(glyphs_dir, glif_name), "w", encoding="utf-8") as f:
            f.write(glif)

    # contents.plist
    contents_entries = "\n".join(
        f"  <key>{k}</key>\n  <string>{v}</string>"
        for k, v in contents.items()
    )
    with open(os.path.join(glyphs_dir, "contents.plist"), "w") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
{contents_entries}
</dict>
</plist>
""")

    print(f"  UFO written → {ufo_path}")
    return ufo_path


# ---------------------------------------------------------------------------
# 9.  Write Hershey/SVG font for Inkscape's Hershey Text extension
# ---------------------------------------------------------------------------

def strokes_to_svg_font_d(strokes_font: list[list[tuple]]) -> str:
    """Convert strokes (font coords, Y-up) to SVG font path d attribute.

    SVG font glyph paths use the same Y-up coordinate system as font units,
    so no Y-flip needed here.  Multiple open strokes are separated by M.
    """
    parts = []
    for pts in strokes_font:
        if len(pts) < 2:
            continue
        segs = catmull_to_bezier(pts)
        if not segs:
            continue
        p0 = segs[0][0]
        d = f"M {p0[0]:.2f} {p0[1]:.2f}"
        for (_, cp1, cp2, p3) in segs:
            d += (f" C {cp1[0]:.2f} {cp1[1]:.2f}"
                  f" {cp2[0]:.2f} {cp2[1]:.2f}"
                  f" {p3[0]:.2f} {p3[1]:.2f}")
        parts.append(d)
    return " ".join(parts)


def write_hershey_svg(out_path: str, glyphs_data: list[dict], upm: int, font_name: str):
    """Write an SVG font file compatible with Inkscape's Hershey Text extension."""

    ascent   = int(upm * 0.8)
    descent  = int(upm * -0.2)
    cap_h    = int(upm * 0.7)
    x_height = int(upm * 0.5)

    # space glyph (no strokes)
    space_adv = int(upm * 0.3)

    glyph_tags = [
        f'<missing-glyph horiz-adv-x="{space_adv}" />',
        f'<glyph unicode=" " glyph-name="space" horiz-adv-x="{space_adv}" />',
    ]

    for gd in glyphs_data:
        d = strokes_to_svg_font_d(gd["strokes"])
        cp = gd["codepoint"]
        # XML-escape the unicode attribute value
        if cp == ord('"'):
            uni_attr = "&quot;"
        elif cp == ord("'"):
            uni_attr = "&apos;"
        elif cp == ord("&"):
            uni_attr = "&amp;"
        elif cp == ord("<"):
            uni_attr = "&lt;"
        elif cp == ord(">"):
            uni_attr = "&gt;"
        else:
            uni_attr = chr(cp)

        adv = int(gd["advance"])
        if d:
            glyph_tags.append(
                f'<glyph unicode="{uni_attr}" glyph-name="{gd["name"]}"'
                f' horiz-adv-x="{adv}" d="{d}" />'
            )
        else:
            glyph_tags.append(
                f'<glyph unicode="{uni_attr}" glyph-name="{gd["name"]}"'
                f' horiz-adv-x="{adv}" />'
            )

    glyphs_xml = "\n".join(glyph_tags)
    font_id = font_name.replace(" ", "_")

    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1">
<metadata>
Font name:    {font_name} Centerline
Created by:   centerline.py (single-stroke plotter font)
Source:       {font_name}.otf
</metadata>
<defs>
<font id="{font_id}Centerline" horiz-adv-x="{int(upm * 0.6)}">
<font-face
  font-family="{font_name} Centerline"
  units-per-em="{upm}"
  ascent="{ascent}"
  descent="{descent}"
  cap-height="{cap_h}"
  x-height="{x_height}"
/>
{glyphs_xml}
</font>
</defs>
</svg>
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"  Hershey SVG font → {out_path}")


# ---------------------------------------------------------------------------
# 10.  Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Build centerline (single-stroke) glyphs from an outline font.")
    parser.add_argument("font", help="Input .otf/.ttf font file")
    parser.add_argument("--glyphs", default="", help="Characters to process (default: printable ASCII + Ukrainian)")
    parser.add_argument("--render-size", type=int, default=300, help="FreeType render height in pixels (default 300)")
    parser.add_argument("--rdp-epsilon", type=float, default=1.5, help="RDP simplification epsilon in pixels (default 1.5)")
    parser.add_argument("--out-dir", default="", help="Output directory (default: next to font file)")
    parser.add_argument("--min-component", type=int, default=10,
                        help="Remove skeleton components smaller than N pixels (default 10). "
                             "Increase for thin/geometric fonts to eliminate dot artefacts.")
    parser.add_argument("--spur-size", type=int, default=12,
                        help="Prune dead-end skeleton branches shorter than N pixels (default 12).")
    parser.add_argument("--no-ufo", action="store_true", help="Skip UFO output")
    parser.add_argument("--no-svg", action="store_true", help="Skip per-glyph SVG output")
    parser.add_argument("--no-hershey", action="store_true", help="Skip Hershey SVG font output")
    args = parser.parse_args()

    font_path = args.font
    font_name = os.path.splitext(os.path.basename(font_path))[0]
    out_dir   = args.out_dir or os.path.join(os.path.dirname(font_path), font_name + "_centerline")
    os.makedirs(out_dir, exist_ok=True)
    svg_dir = os.path.join(out_dir, "svg")
    if not args.no_svg:
        os.makedirs(svg_dir, exist_ok=True)

    # Build character set
    if args.glyphs:
        chars = list(args.glyphs)
    else:
        # Printable ASCII
        chars = [chr(c) for c in range(33, 127)]
        # Ukrainian (Cyrillic) block
        chars += [chr(c) for c in range(0x0400, 0x0500) if chr(c).isprintable()]
        # Basic Latin extended — accented characters common in Ukrainian
        chars += [chr(c) for c in range(0xC0, 0x180) if chr(c).isprintable()]

    # Load font
    tt    = TTFont(font_path)
    cmap  = tt.getBestCmap()
    upm   = tt['head'].unitsPerEm
    face  = freetype.Face(font_path)

    available_codepoints = set(cmap.keys())
    chars = [c for c in chars if ord(c) in available_codepoints]
    # deduplicate preserving order
    seen: set = set()
    chars_unique = []
    for c in chars:
        if c not in seen:
            seen.add(c)
            chars_unique.append(c)
    chars = chars_unique

    print(f"Font: {font_path}")
    print(f"UPM:  {upm}")
    print(f"Glyphs to process: {len(chars)}")
    print(f"Output: {out_dir}")
    print()

    glyphs_data = []
    size_px = args.render_size

    for i, char in enumerate(chars):
        cp   = ord(char)
        name = cmap[cp]
        print(f"  [{i+1:3d}/{len(chars)}] U+{cp:04X} {repr(char)}  ({name})")

        binary, metrics = render_glyph_bitmap(face, char, size_px)
        if binary.size == 0 or not metrics:
            print("       ⚠  empty bitmap, skipping")
            continue

        skel    = thin(binary, spur_size=args.spur_size, min_component=args.min_component)
        strokes_px = skeleton_to_strokes(skel)
        strokes_font = pixel_coords_to_font(
            strokes_px, metrics, size_px, upm, rdp_eps=args.rdp_epsilon
        )

        advance = metrics.get("advance_x", upm) * upm / size_px

        gd = {
            "char":       char,
            "codepoint":  cp,
            "name":       name,
            "strokes":    strokes_font,
            "advance":    advance,
        }
        glyphs_data.append(gd)

        if not args.no_svg:
            svg_path = os.path.join(svg_dir, f"U{cp:04X}_{name}.svg")
            write_glyph_svg(svg_path, char, strokes_font, advance, upm)

    print()
    print("Writing specimen SVG …")
    write_specimen_svg(
        os.path.join(out_dir, font_name + "_centerline_specimen.svg"),
        glyphs_data, upm,
    )

    if not args.no_ufo:
        print("Writing UFO font …")
        write_ufo(out_dir, glyphs_data, upm, font_name)

    if not args.no_hershey:
        print("Writing Hershey SVG font …")
        hershey_path = os.path.join(out_dir, font_name + "_centerline.svg")
        write_hershey_svg(hershey_path, glyphs_data, upm, font_name)

    print()
    print(f"Done! Output in: {out_dir}")
    if not args.no_hershey:
        print()
        print("=== Inkscape Hershey Text ===")
        print(f"  Extensions → Text → Hershey Text")
        print(f"  Font face: Other")
        print(f"  Name/Path: {os.path.realpath(hershey_path)}")


if __name__ == "__main__":
    main()
