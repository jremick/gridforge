"""Automated physical-scale, vector-geometry, collar-text, and visual PDF verifier for GridForge.

Can be imported by pytest integration tests or invoked directly from the command line:

    python tests/integration/verify_pdf.py out.pdf --scale 25000 --paper a4 --epsg 32756 --gzd 56H
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pypdf
from pypdf import PdfReader
from pypdf.generic import ContentStream


def _dense_vector_pypdf_context() -> Any:
    """Expand pypdf 7.x content-stream token budget for dense multi-megabyte vector topo PDFs."""
    apply_cfg = getattr(pypdf, "apply_configuration", None)
    if callable(apply_cfg):
        return apply_cfg(stream_parsing_work_maximum_count=50_000_000)
    return nullcontext()

PT_TO_MM: float = 25.4 / 72.0

PAPER_DIMENSIONS_MM: dict[str, tuple[float, float]] = {
    "letter": (215.9, 279.4),
    "a4": (210.0, 297.0),
    "legal": (215.9, 355.6),
    "a3": (297.0, 420.0),
}

# Default FrameMarginsMM from gridforge.config (top, right, bottom, left)
DEFAULT_MARGINS_MM: tuple[float, float, float, float] = (14.0, 12.0, 48.0, 12.0)

Matrix = tuple[float, float, float, float, float, float]
IDENTITY_MATRIX: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
BBoxTuple = tuple[float, float, float, float]  # (xmin, ymin, xmax, ymax) in top-down mm
Segment = tuple[float, float, float, float]  # (x0, y0, x1, y1) in top-down mm


def expected_page_dimensions_mm(paper: str, orientation: str = "portrait") -> tuple[float, float]:
    """Return expected physical (width_mm, height_mm) for a paper size and orientation."""
    key = paper.strip().lower()
    if key not in PAPER_DIMENSIONS_MM:
        raise ValueError(f"Unknown paper size {paper!r}. Expected one of {sorted(PAPER_DIMENSIONS_MM)}.")
    w, h = PAPER_DIMENSIONS_MM[key]
    orient = orientation.strip().lower()
    if orient == "landscape":
        return (h, w)
    if orient == "portrait":
        return (w, h)
    raise ValueError(f"Unknown orientation {orientation!r}. Expected 'portrait' or 'landscape'.")


def expected_grid_spacing_mm(scale: int, grid_spacing_m: float = 1000.0) -> float:
    """Return exact physical distance in millimeters between consecutive MGRS grid lines."""
    if scale <= 0:
        raise ValueError(f"Scale must be positive, got {scale}.")
    return (grid_spacing_m / float(scale)) * 1000.0


def _mul_matrix(m1: Matrix, m2: Matrix) -> Matrix:
    """Concatenate two PDF affine matrices: M_new = m1 * m2 (row-vector convention)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _apply_matrix(m: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def _intersect_bbox(b1: BBoxTuple | None, b2: BBoxTuple | None) -> BBoxTuple | None:
    if b1 is None:
        return b2
    if b2 is None:
        return b1
    xmin = max(b1[0], b2[0])
    ymin = max(b1[1], b2[1])
    xmax = min(b1[2], b2[2])
    ymax = min(b1[3], b2[3])
    if xmax < xmin or ymax < ymin:
        return (xmin, ymin, xmin, ymin)
    return (xmin, ymin, xmax, ymax)


def _clip_segment(seg: Segment, clip: BBoxTuple | None) -> Segment | None:
    """Liang-Barsky line segment clipping against an axis-aligned rectangle."""
    if clip is None:
        return seg
    x0, y0, x1, y1 = seg
    xmin, ymin, xmax, ymax = clip
    dx = x1 - x0
    dy = y1 - y0
    t0 = 0.0
    t1 = 1.0
    for p, q in (
        (-dx, x0 - xmin),
        (dx, xmax - x0),
        (-dy, y0 - ymin),
        (dy, ymax - y0),
    ):
        if p == 0.0:
            if q < 0.0:
                return None
        else:
            r = q / p
            if p < 0.0:
                if r > t1:
                    return None
                if r > t0:
                    t0 = r
            else:
                if r < t0:
                    return None
                if r < t1:
                    t1 = r
    return (x0 + t0 * dx, y0 + t0 * dy, x0 + t1 * dx, y0 + t1 * dy)


def _resolve_obj(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "get_object"):
        return obj.get_object()
    return obj


@dataclass
class _ExtractedVectors:
    """Extracted vector primitives and text anchors from page 1 in top-down mm."""

    vertical_segments: list[Segment] = field(default_factory=list)
    horizontal_segments: list[Segment] = field(default_factory=list)
    total_vector_segments: int = 0
    total_curves: int = 0
    image_draw_count: int = 0
    text_anchors: list[tuple[float, float, str]] = field(default_factory=list)


def _extract_pdf_vectors(reader: PdfReader, page_index: int = 0) -> _ExtractedVectors:
    """Parse PDF page content stream (including Form XObjects) tracking full CTM & clip stack."""
    page = reader.pages[page_index]
    media_x0 = float(page.mediabox.left)
    media_y1 = float(page.mediabox.top)
    media_w_mm = float(page.mediabox.width) * PT_TO_MM
    media_h_mm = float(page.mediabox.height) * PT_TO_MM
    page_clip: BBoxTuple = (0.0, 0.0, media_w_mm, media_h_mm)

    result = _ExtractedVectors()

    def pt_to_page_mm(x_pt: float, y_pt: float) -> tuple[float, float]:
        return ((x_pt - media_x0) * PT_TO_MM, (media_y1 - y_pt) * PT_TO_MM)

    def record_segment(
        x0_mm: float,
        y0_mm: float,
        x1_mm: float,
        y1_mm: float,
        clip_box: BBoxTuple | None,
    ) -> None:
        clipped = _clip_segment((x0_mm, y0_mm, x1_mm, y1_mm), clip_box)
        if clipped is None:
            return
        cx0, cy0, cx1, cy1 = clipped
        dx = abs(cx1 - cx0)
        dy = abs(cy1 - cy0)
        # Keep short perpendicular neatline ticks (0.7..6 mm, tight axis alignment)
        # and long nearly-axis-aligned frame/grid lines (>= 15 mm, slope <= 0.03)
        # while excluding short oblique contour/road chords.
        if (dy >= 15.0 and dx <= 2.0 and dx / dy <= 0.03) or (0.7 <= dy <= 6.0 and dx <= 0.15):
            result.vertical_segments.append(clipped)
        elif (dx >= 15.0 and dy <= 2.0 and dy / dx <= 0.03) or (0.7 <= dx <= 6.0 and dy <= 0.15):
            result.horizontal_segments.append(clipped)

    def walk_stream(
        stream_obj: Any,
        resources: Any,
        initial_ctm: Matrix,
        initial_clip: BBoxTuple | None,
        depth: int = 0,
    ) -> None:
        if stream_obj is None or depth > 16:
            return
        cs = ContentStream(stream_obj, reader)
        res_dict = _resolve_obj(resources) or {}

        # Graphics state stack entries: (ctm, clip_bbox)
        stack: list[tuple[Matrix, BBoxTuple | None]] = [(initial_ctm, initial_clip)]
        ctm: Matrix = initial_ctm
        clip_box: BBoxTuple | None = initial_clip

        # Current path state in user coordinates (untransformed until flush)
        # Each entry is ("seg", x0, y0, x1, y1) or ("rect", x, y, w, h)
        path_segments_pt: list[Segment] = []
        path_rects_pt: list[tuple[float, float, float, float]] = []
        cur_x = 0.0
        cur_y = 0.0
        sub_x = 0.0
        sub_y = 0.0
        has_subpath = False
        pending_clip = False

        # Text state
        in_text = False
        tm: Matrix = IDENTITY_MATRIX
        tlm: Matrix = IDENTITY_MATRIX
        text_leading = 0.0

        def close_subpath() -> None:
            nonlocal cur_x, cur_y
            if has_subpath and (cur_x != sub_x or cur_y != sub_y):
                path_segments_pt.append((cur_x, cur_y, sub_x, sub_y))
                cur_x, cur_y = sub_x, sub_y

        def flush_path(stroke: bool, fill: bool, close_first: bool) -> None:
            nonlocal has_subpath, pending_clip, clip_box
            if close_first:
                close_subpath()

            # Compute transformed segments and bbox for clip/paint
            min_x = math.inf
            min_y = math.inf
            max_x = -math.inf
            max_y = -math.inf

            transformed_segs: list[Segment] = []
            for sx0, sy0, sx1, sy1 in path_segments_pt:
                px0, py0 = _apply_matrix(ctm, sx0, sy0)
                px1, py1 = _apply_matrix(ctm, sx1, sy1)
                mx0, my0 = pt_to_page_mm(px0, py0)
                mx1, my1 = pt_to_page_mm(px1, py1)
                transformed_segs.append((mx0, my0, mx1, my1))
                if mx0 < min_x:
                    min_x = mx0
                if mx0 > max_x:
                    max_x = mx0
                if mx1 < min_x:
                    min_x = mx1
                if mx1 > max_x:
                    max_x = mx1
                if my0 < min_y:
                    min_y = my0
                if my0 > max_y:
                    max_y = my0
                if my1 < min_y:
                    min_y = my1
                if my1 > max_y:
                    max_y = my1

            for rx, ry, rw, rh in path_rects_pt:
                c0 = pt_to_page_mm(*_apply_matrix(ctm, rx, ry))
                c1 = pt_to_page_mm(*_apply_matrix(ctm, rx + rw, ry))
                c2 = pt_to_page_mm(*_apply_matrix(ctm, rx + rw, ry + rh))
                c3 = pt_to_page_mm(*_apply_matrix(ctm, rx, ry + rh))
                xs = (c0[0], c1[0], c2[0], c3[0])
                ys = (c0[1], c1[1], c2[1], c3[1])
                rmin_x, rmax_x = min(xs), max(xs)
                rmin_y, rmax_y = min(ys), max(ys)
                if rmin_x < min_x:
                    min_x = rmin_x
                if rmax_x > max_x:
                    max_x = rmax_x
                if rmin_y < min_y:
                    min_y = rmin_y
                if rmax_y > max_y:
                    max_y = rmax_y

                if stroke:
                    transformed_segs.append((c0[0], c0[1], c1[0], c1[1]))
                    transformed_segs.append((c1[0], c1[1], c2[0], c2[1]))
                    transformed_segs.append((c2[0], c2[1], c3[0], c3[1]))
                    transformed_segs.append((c3[0], c3[1], c0[0], c0[1]))
                elif fill:
                    # Treat very thin filled rectangles as hairline strokes (some exporters do this)
                    rect_w = rmax_x - rmin_x
                    rect_h = rmax_y - rmin_y
                    if rect_w <= 0.8 and rect_h >= 1.0:
                        mid_x = 0.5 * (rmin_x + rmax_x)
                        transformed_segs.append((mid_x, rmin_y, mid_x, rmax_y))
                    elif rect_h <= 0.8 and rect_w >= 1.0:
                        mid_y = 0.5 * (rmin_y + rmax_y)
                        transformed_segs.append((rmin_x, mid_y, rmax_x, mid_y))
                    result.total_vector_segments += 4

            if stroke or fill:
                result.total_vector_segments += len(path_segments_pt)
                if stroke:
                    result.total_vector_segments += 4 * len(path_rects_pt)
                for mx0, my0, mx1, my1 in transformed_segs:
                    record_segment(mx0, my0, mx1, my1, clip_box)

            if pending_clip and min_x <= max_x and min_y <= max_y:
                clip_box = _intersect_bbox(clip_box, (min_x, min_y, max_x, max_y))
            pending_clip = False
            path_segments_pt.clear()
            path_rects_pt.clear()
            has_subpath = False

        for operands, operator in cs.operations:
            if operator == b"q":
                stack.append((ctm, clip_box))
            elif operator == b"Q":
                if len(stack) > 1:
                    ctm, clip_box = stack.pop()
            elif operator == b"cm":
                if len(operands) == 6:
                    m_op: Matrix = (
                        float(operands[0]),
                        float(operands[1]),
                        float(operands[2]),
                        float(operands[3]),
                        float(operands[4]),
                        float(operands[5]),
                    )
                    ctm = _mul_matrix(m_op, ctm)
            elif operator == b"m":
                cur_x = sub_x = float(operands[0])
                cur_y = sub_y = float(operands[1])
                has_subpath = True
            elif operator == b"l":
                nx = float(operands[0])
                ny = float(operands[1])
                if has_subpath:
                    path_segments_pt.append((cur_x, cur_y, nx, ny))
                cur_x, cur_y = nx, ny
            elif operator == b"re":
                rx = float(operands[0])
                ry = float(operands[1])
                rw = float(operands[2])
                rh = float(operands[3])
                path_rects_pt.append((rx, ry, rw, rh))
                cur_x = sub_x = rx
                cur_y = sub_y = ry
                has_subpath = True
            elif operator == b"h":
                close_subpath()
            elif operator in (b"c", b"v", b"y"):
                result.total_curves += 1
                cur_x = float(operands[-2])
                cur_y = float(operands[-1])
            elif operator in (b"W", b"W*"):
                pending_clip = True
            elif operator == b"n":
                flush_path(stroke=False, fill=False, close_first=False)
            elif operator == b"S":
                flush_path(stroke=True, fill=False, close_first=False)
            elif operator == b"s":
                flush_path(stroke=True, fill=False, close_first=True)
            elif operator in (b"f", b"F", b"f*"):
                flush_path(stroke=False, fill=True, close_first=False)
            elif operator in (b"B", b"B*"):
                flush_path(stroke=True, fill=True, close_first=False)
            elif operator in (b"b", b"b*"):
                flush_path(stroke=True, fill=True, close_first=True)
            elif operator == b"Do":
                if operands:
                    name = str(operands[0])
                    xobjs = _resolve_obj(res_dict.get("/XObject"))
                    if xobjs and name in xobjs:
                        xobj = _resolve_obj(xobjs[name])
                        subtype = str(xobj.get("/Subtype", "")) if hasattr(xobj, "get") else ""
                        if subtype == "/Image":
                            result.image_draw_count += 1
                        elif subtype == "/Form":
                            form_ctm = ctm
                            form_clip = clip_box
                            if "/Matrix" in xobj:
                                raw_m = _resolve_obj(xobj["/Matrix"])
                                if raw_m and len(raw_m) == 6:
                                    fm: Matrix = tuple(float(v) for v in raw_m)  # type: ignore[assignment]
                                    form_ctm = _mul_matrix(fm, ctm)
                            if "/BBox" in xobj:
                                raw_b = _resolve_obj(xobj["/BBox"])
                                if raw_b and len(raw_b) == 4:
                                    bx0, by0, bx1, by1 = (float(v) for v in raw_b)
                                    corners = [
                                        pt_to_page_mm(*_apply_matrix(form_ctm, cx, cy))
                                        for cx, cy in (
                                            (bx0, by0),
                                            (bx1, by0),
                                            (bx1, by1),
                                            (bx0, by1),
                                        )
                                    ]
                                    fb = (
                                        min(p[0] for p in corners),
                                        min(p[1] for p in corners),
                                        max(p[0] for p in corners),
                                        max(p[1] for p in corners),
                                    )
                                    form_clip = _intersect_bbox(form_clip, fb)
                            sub_res = _resolve_obj(xobj.get("/Resources")) or res_dict
                            walk_stream(xobj, sub_res, form_ctm, form_clip, depth + 1)
            elif operator == b"BT":
                in_text = True
                tm = IDENTITY_MATRIX
                tlm = IDENTITY_MATRIX
            elif operator == b"ET":
                in_text = False
            elif in_text and operator == b"Tm" and len(operands) == 6:
                tm = tlm = (
                    float(operands[0]),
                    float(operands[1]),
                    float(operands[2]),
                    float(operands[3]),
                    float(operands[4]),
                    float(operands[5]),
                )
            elif in_text and operator == b"Td" and len(operands) == 2:
                tlm = _mul_matrix((1.0, 0.0, 0.0, 1.0, float(operands[0]), float(operands[1])), tlm)
                tm = tlm
            elif in_text and operator == b"TD" and len(operands) == 2:
                ty = float(operands[1])
                text_leading = -ty
                tlm = _mul_matrix((1.0, 0.0, 0.0, 1.0, float(operands[0]), ty), tlm)
                tm = tlm
            elif in_text and operator == b"TL" and len(operands) == 1:
                text_leading = float(operands[0])
            elif in_text and operator == b"T*":
                tlm = _mul_matrix((1.0, 0.0, 0.0, 1.0, 0.0, -text_leading), tlm)
                tm = tlm
            elif in_text and operator in (b"Tj", b"TJ", b"'", b'"'):
                if operator in (b"'", b'"'):
                    tlm = _mul_matrix((1.0, 0.0, 0.0, 1.0, 0.0, -text_leading), tlm)
                    tm = tlm
                combined = _mul_matrix(tm, ctm)
                tx_mm, ty_mm = pt_to_page_mm(combined[4], combined[5])
                if operator == b"TJ" and operands:
                    parts = [str(item) for item in operands[0] if isinstance(item, (str, bytes))]
                    txt = "".join(parts)
                elif operands:
                    txt = str(operands[-1])
                else:
                    txt = ""
                if txt.strip():
                    result.text_anchors.append((tx_mm, ty_mm, txt.strip()))

    walk_stream(page.get_contents(), page.get("/Resources"), IDENTITY_MATRIX, page_clip, 0)
    return result


def _merge_intervals(intervals: list[tuple[float, float]]) -> float:
    """Return total covered length of a list of 1D intervals."""
    if not intervals:
        return 0.0
    sorted_iv = sorted(intervals)
    cur_s, cur_e = sorted_iv[0]
    total = 0.0
    for s, e in sorted_iv[1:]:
        if s <= cur_e + 0.5:  # bridge tiny sub-millimeter rounding gaps
            if e > cur_e:
                cur_e = e
        else:
            total += max(0.0, cur_e - cur_s)
            cur_s, cur_e = s, e
    total += max(0.0, cur_e - cur_s)
    return total


def _cluster_lines(
    segments: list[Segment],
    axis: str,
    span_min: float,
    span_max: float,
    min_coverage_mm: float,
    cluster_tol_mm: float = 0.20,
) -> list[tuple[float, float, float]]:
    """Cluster collinear axis-aligned segments and return (pos_mm, coverage_mm, skew_mm).

    axis == 'v': vertical lines (clustered by x, spanning y in [span_min, span_max]).
    axis == 'h': horizontal lines (clustered by y, spanning x in [span_min, span_max]).
    """
    if not segments:
        return []

    if axis == "v":
        keyed = sorted(segments, key=lambda s: 0.5 * (s[0] + s[2]))
    else:
        keyed = sorted(segments, key=lambda s: 0.5 * (s[1] + s[3]))

    clusters: list[list[Segment]] = []
    cluster_centers: list[float] = []

    for seg in keyed:
        pos = 0.5 * (seg[0] + seg[2]) if axis == "v" else 0.5 * (seg[1] + seg[3])
        if clusters and abs(pos - cluster_centers[-1]) <= cluster_tol_mm:
            clusters[-1].append(seg)
            # Keep running average of cluster center
            n = len(clusters[-1])
            cluster_centers[-1] = cluster_centers[-1] + (pos - cluster_centers[-1]) / n
        else:
            clusters.append([seg])
            cluster_centers.append(pos)

    out: list[tuple[float, float, float]] = []
    for cl in clusters:
        intervals: list[tuple[float, float]] = []
        coords: list[float] = []
        max_seg_skew = 0.0
        for x0, y0, x1, y1 in cl:
            if axis == "v":
                a, b = min(y0, y1), max(y0, y1)
                ca, cb = max(a, span_min), min(b, span_max)
                if cb > ca:
                    intervals.append((ca, cb))
                    coords.append(x0)
                    coords.append(x1)
                    max_seg_skew = max(max_seg_skew, abs(x1 - x0))
            else:
                a, b = min(x0, x1), max(x0, x1)
                ca, cb = max(a, span_min), min(b, span_max)
                if cb > ca:
                    intervals.append((ca, cb))
                    coords.append(y0)
                    coords.append(y1)
                    max_seg_skew = max(max_seg_skew, abs(y1 - y0))
        coverage = _merge_intervals(intervals)
        if coverage >= min_coverage_mm and coords:
            mean_pos = sum(coords) / len(coords)
            # Use max single-segment skew AND full-cluster spread across the frame
            skew = max(max_seg_skew, max(coords) - min(coords))
            out.append((mean_pos, coverage, skew))

    out.sort(key=lambda item: item[0])
    return out


def _detect_map_frame_mm(
    vectors: _ExtractedVectors,
    page_w_mm: float,
    page_h_mm: float,
    expected_margins_mm: tuple[float, float, float, float] = DEFAULT_MARGINS_MM,
) -> tuple[float, float, float, float]:
    """Detect the inner map neatline rectangle (fx, fy, fw, fh) in mm from PDF vectors.

    Falls back to expected_margins_mm if frame borders cannot be cleanly isolated.
    """
    m_top, m_right, m_bottom, m_left = expected_margins_mm
    default_frame = (m_left, m_top, page_w_mm - m_left - m_right, page_h_mm - m_top - m_bottom)

    long_v = _cluster_lines(
        vectors.vertical_segments,
        axis="v",
        span_min=0.0,
        span_max=page_h_mm,
        min_coverage_mm=0.45 * page_h_mm,
        cluster_tol_mm=0.15,
    )
    long_h = _cluster_lines(
        vectors.horizontal_segments,
        axis="h",
        span_min=0.0,
        span_max=page_w_mm,
        min_coverage_mm=0.45 * page_w_mm,
        cluster_tol_mm=0.15,
    )

    if not long_v or not long_h:
        return default_frame

    # Look tightly around expected neatline borders; prefer line closest to inner neatline
    # (avoids confusing an outer double-neatline or a nearby interior 1 km grid line with the frame)
    def pick_border(candidates: list[tuple[float, float, float]], target: float, lo: float, hi: float) -> float:
        near = [pos for pos, _, _ in candidates if lo <= pos <= hi]
        if not near:
            return target
        return min(near, key=lambda p: abs(p - target))

    fx = pick_border(long_v, m_left, m_left - 0.5, m_left + 1.5)
    fx_right = pick_border(long_v, page_w_mm - m_right, page_w_mm - m_right - 1.5, page_w_mm - m_right + 0.5)
    fy = pick_border(long_h, m_top, m_top - 0.5, m_top + 1.5)
    fy_bottom = pick_border(long_h, page_h_mm - m_bottom, page_h_mm - m_bottom - 1.5, page_h_mm - m_bottom + 0.5)

    fw = fx_right - fx
    fh = fy_bottom - fy
    if fw <= 0.4 * page_w_mm or fh <= 0.4 * page_h_mm:
        return default_frame
    return (fx, fy, fw, fh)


def _extract_collar_ticks_mm(
    vectors: _ExtractedVectors,
    frame_mm: tuple[float, float, float, float],
    border_exclude_mm: float = 0.75,
) -> dict[str, list[float]]:
    """Extract neatline collar tick positions along top, bottom, left, and right edges."""
    fx, fy, fw, fh = frame_mm
    fx_right = fx + fw
    fy_bottom = fy + fh

    top_xs: list[float] = []
    bottom_xs: list[float] = []
    left_ys: list[float] = []
    right_ys: list[float] = []

    for x0, y0, x1, y1 in vectors.vertical_segments:
        length = abs(y1 - y0)
        if not (0.7 <= length <= 5.5):
            continue
        if abs(x1 - x0) > 0.15:
            continue
        mx = 0.5 * (x0 + x1)
        if not (fx + border_exclude_mm < mx < fx_right - border_exclude_mm):
            continue
        ymin, ymax = min(y0, y1), max(y0, y1)
        # Top ticks protrude strictly upward OUTSIDE the top neatline (ymax ≈ fy, ymin < fy - 0.5)
        if fy - 5.5 <= ymin < fy - 0.5 and abs(ymax - fy) <= 0.4:
            top_xs.append(mx)
        # Bottom ticks protrude strictly downward OUTSIDE the bottom neatline (ymin ≈ fy_bottom, ymax > fy_bottom + 0.5)
        elif fy_bottom + 0.5 < ymax <= fy_bottom + 5.5 and abs(ymin - fy_bottom) <= 0.4:
            bottom_xs.append(mx)

    for x0, y0, x1, y1 in vectors.horizontal_segments:
        length = abs(x1 - x0)
        if not (0.7 <= length <= 5.5):
            continue
        if abs(y1 - y0) > 0.15:
            continue
        my = 0.5 * (y0 + y1)
        if not (fy + border_exclude_mm < my < fy_bottom - border_exclude_mm):
            continue
        xmin, xmax = min(x0, x1), max(x0, x1)
        # Left ticks protrude strictly leftward OUTSIDE the left neatline (xmax ≈ fx, xmin < fx - 0.5)
        if fx - 5.5 <= xmin < fx - 0.5 and abs(xmax - fx) <= 0.4:
            left_ys.append(my)
        # Right ticks protrude strictly rightward OUTSIDE the right neatline (xmin ≈ fx_right, xmax > fx_right + 0.5)
        elif fx_right + 0.5 < xmax <= fx_right + 5.5 and abs(xmin - fx_right) <= 0.4:
            right_ys.append(my)

    def dedupe(vals: list[float], tol: float = 0.25) -> list[float]:
        if not vals:
            return []
        vals_sorted = sorted(vals)
        groups: list[list[float]] = [[vals_sorted[0]]]
        for v in vals_sorted[1:]:
            if abs(v - groups[-1][-1]) <= tol:
                groups[-1].append(v)
            else:
                groups.append([v])
        return [sum(g) / len(g) for g in groups]

    return {
        "top": dedupe(top_xs),
        "bottom": dedupe(bottom_xs),
        "left": dedupe(left_ys),
        "right": dedupe(right_ys),
    }


def _query_pdfinfo_dimensions_mm(pdf_path: Path) -> tuple[float, float] | None:
    """Query page width & height in mm via `pdfinfo` if available on PATH."""
    pdfinfo = shutil.which("pdfinfo")
    if not pdfinfo:
        return None
    proc = subprocess.run(
        [pdfinfo, str(pdf_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    match = re.search(r"Page size:\s+([0-9.]+)\s+x\s+([0-9.]+)\s+pts", proc.stdout)
    if not match:
        return None
    w_pt = float(match.group(1))
    h_pt = float(match.group(2))
    return (w_pt * PT_TO_MM, h_pt * PT_TO_MM)


def _query_pdftotext(pdf_path: Path) -> str | None:
    """Extract text via `pdftotext` if installed (useful diagnostic cross-check)."""
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        return None
    proc = subprocess.run(
        [pdftotext, str(pdf_path), "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


@dataclass
class PDFVerificationReport:
    """Structured result of all physical-scale, vector, and collar-text PDF checks."""

    pdf_path: Path
    file_size_bytes: int
    page_count: int
    pypdf_width_mm: float
    pypdf_height_mm: float
    pdfinfo_width_mm: float | None
    pdfinfo_height_mm: float | None
    expected_width_mm: float
    expected_height_mm: float
    scale: int
    expected_grid_spacing_mm: float
    detected_frame_mm: tuple[float, float, float, float]
    vertical_grid_x_mm: list[float]
    horizontal_grid_y_mm: list[float]
    vertical_spacings_mm: list[float]
    horizontal_spacings_mm: list[float]
    max_vertical_spacing_err_mm: float | None
    max_horizontal_spacing_err_mm: float | None
    max_vertical_skew_mm: float | None
    max_horizontal_skew_mm: float | None
    collar_ticks_mm: dict[str, list[float]]
    total_vector_segments: int
    image_draw_count: int
    extracted_text: str
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdf_path": str(self.pdf_path),
            "passed": self.passed,
            "file_size_bytes": self.file_size_bytes,
            "page_count": self.page_count,
            "pypdf_dimensions_mm": [round(self.pypdf_width_mm, 3), round(self.pypdf_height_mm, 3)],
            "pdfinfo_dimensions_mm": (
                [round(self.pdfinfo_width_mm, 3), round(self.pdfinfo_height_mm, 3)]
                if self.pdfinfo_width_mm is not None and self.pdfinfo_height_mm is not None
                else None
            ),
            "expected_dimensions_mm": [self.expected_width_mm, self.expected_height_mm],
            "scale": self.scale,
            "expected_grid_spacing_mm": round(self.expected_grid_spacing_mm, 4),
            "detected_frame_mm": [round(v, 3) for v in self.detected_frame_mm],
            "vertical_grid_x_mm": [round(v, 3) for v in self.vertical_grid_x_mm],
            "horizontal_grid_y_mm": [round(v, 3) for v in self.horizontal_grid_y_mm],
            "vertical_spacings_mm": [round(v, 3) for v in self.vertical_spacings_mm],
            "horizontal_spacings_mm": [round(v, 3) for v in self.horizontal_spacings_mm],
            "max_vertical_spacing_err_mm": (
                round(self.max_vertical_spacing_err_mm, 4)
                if self.max_vertical_spacing_err_mm is not None
                else None
            ),
            "max_horizontal_spacing_err_mm": (
                round(self.max_horizontal_spacing_err_mm, 4)
                if self.max_horizontal_spacing_err_mm is not None
                else None
            ),
            "max_vertical_skew_mm": (
                round(self.max_vertical_skew_mm, 5)
                if self.max_vertical_skew_mm is not None
                else None
            ),
            "max_horizontal_skew_mm": (
                round(self.max_horizontal_skew_mm, 5)
                if self.max_horizontal_skew_mm is not None
                else None
            ),
            "collar_ticks_count": {k: len(v) for k, v in self.collar_ticks_mm.items()},
            "total_vector_segments": self.total_vector_segments,
            "image_draw_count": self.image_draw_count,
            "checks": self.checks,
            "errors": self.errors,
            "warnings": self.warnings,
        }

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [
            f"[{status}] PDF Verification: {self.pdf_path}",
            f"  File size       : {self.file_size_bytes:,} bytes ({self.page_count} page)",
            f"  MediaBox (pypdf): {self.pypdf_width_mm:.3f} x {self.pypdf_height_mm:.3f} mm "
            f"(expected {self.expected_width_mm:.2f} x {self.expected_height_mm:.2f} mm)",
        ]
        if self.pdfinfo_width_mm is not None and self.pdfinfo_height_mm is not None:
            lines.append(
                f"  MediaBox(pdfinf): {self.pdfinfo_width_mm:.3f} x {self.pdfinfo_height_mm:.3f} mm"
            )
        lines.append(
            f"  Map frame (mm)  : x={self.detected_frame_mm[0]:.2f}, y={self.detected_frame_mm[1]:.2f}, "
            f"w={self.detected_frame_mm[2]:.2f}, h={self.detected_frame_mm[3]:.2f}"
        )
        lines.append(
            f"  Scale 1:{self.scale:,} -> expected 1 km grid spacing = {self.expected_grid_spacing_mm:.4f} mm"
        )
        lines.append(
            f"  Vertical lines  : {len(self.vertical_grid_x_mm)} "
            f"(spacings={[round(s, 3) for s in self.vertical_spacings_mm]}, "
            f"max_err={self.max_vertical_spacing_err_mm}, max_skew={self.max_vertical_skew_mm})"
        )
        lines.append(
            f"  Horizontal lines: {len(self.horizontal_grid_y_mm)} "
            f"(spacings={[round(s, 3) for s in self.horizontal_spacings_mm]}, "
            f"max_err={self.max_horizontal_spacing_err_mm}, max_skew={self.max_horizontal_skew_mm})"
        )
        lines.append(
            f"  Collar ticks    : "
            + ", ".join(f"{k}={len(v)}" for k, v in self.collar_ticks_mm.items())
        )
        lines.append(
            f"  Vector segments : {self.total_vector_segments:,} (images={self.image_draw_count})"
        )
        if self.warnings:
            lines.append("  Warnings:")
            for w in self.warnings:
                lines.append(f"    - {w}")
        if self.errors:
            lines.append("  ERRORS:")
            for e in self.errors:
                lines.append(f"    - {e}")
        return "\n".join(lines)


def verify_pdf(
    pdf_path: Path | str,
    *,
    scale: int = 25000,
    paper: str = "a4",
    orientation: str = "portrait",
    expected_epsg: int | None = None,
    expected_gzd: str | None = None,
    expected_title: str | None = None,
    extra_required_text: list[str] | None = None,
    require_osm_attribution: bool = True,
    require_copernicus_attribution: bool = True,
    dimension_tol_mm: float = 0.15,
    grid_spacing_tol_mm: float = 0.15,
    grid_skew_tol_mm: float = 0.05,
    min_size_bytes: int = 50_000,
    min_vector_segments: int = 50,
    frame_mm: tuple[float, float, float, float] | None = None,
    expected_margins_mm: tuple[float, float, float, float] = DEFAULT_MARGINS_MM,
) -> PDFVerificationReport:
    """Run full physical MediaBox, MGRS 1 km vector grid spacing, orthogonality, and collar checks."""
    path = Path(pdf_path)
    exp_w, exp_h = expected_page_dimensions_mm(paper, orientation)
    exp_grid_mm = expected_grid_spacing_mm(scale, 1000.0)

    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, bool] = {}

    if not path.is_file():
        raise FileNotFoundError(f"PDF does not exist: {path}")

    file_size = path.stat().st_size
    size_ok = file_size >= min_size_bytes
    checks["file_size"] = size_ok
    if not size_ok:
        errors.append(
            f"PDF file size {file_size:,} bytes is below minimum {min_size_bytes:,} bytes (blank/empty output?)."
        )

    reader = PdfReader(str(path))
    page_count = len(reader.pages)
    checks["single_page"] = page_count == 1
    if page_count != 1:
        errors.append(f"Expected single-page PDF, found {page_count} pages.")

    page = reader.pages[0]
    pypdf_w = float(page.mediabox.width) * PT_TO_MM
    pypdf_h = float(page.mediabox.height) * PT_TO_MM

    # Account for optional PDF /Rotate metadata if ever used
    rotate = int(page.get("/Rotate", 0) or 0) % 360
    if rotate in (90, 270):
        pypdf_w, pypdf_h = pypdf_h, pypdf_w

    # 1. Exact physical MediaBox check (pypdf)
    w_err = abs(pypdf_w - exp_w)
    h_err = abs(pypdf_h - exp_h)
    dim_ok = w_err <= dimension_tol_mm and h_err <= dimension_tol_mm
    checks["mediabox_pypdf"] = dim_ok
    if not dim_ok:
        errors.append(
            f"MediaBox dimensions mismatch (pypdf): got {pypdf_w:.3f} x {pypdf_h:.3f} mm, "
            f"expected {exp_w:.2f} x {exp_h:.2f} mm ± {dimension_tol_mm:.2f} mm "
            f"(err: dw={w_err:.3f} mm, dh={h_err:.3f} mm)."
        )

    # Cross-check with pdfinfo if present
    pdfinfo_dims = _query_pdfinfo_dimensions_mm(path)
    pdfinfo_w: float | None = None
    pdfinfo_h: float | None = None
    if pdfinfo_dims is not None:
        pdfinfo_w, pdfinfo_h = pdfinfo_dims
        pi_w_err = abs(pdfinfo_w - exp_w)
        pi_h_err = abs(pdfinfo_h - exp_h)
        pi_agree = abs(pdfinfo_w - pypdf_w) <= 0.05 and abs(pdfinfo_h - pypdf_h) <= 0.05
        pi_ok = pi_w_err <= dimension_tol_mm and pi_h_err <= dimension_tol_mm and pi_agree
        checks["mediabox_pdfinfo"] = pi_ok
        if not pi_ok:
            errors.append(
                f"pdfinfo dimensions check failed: pdfinfo={pdfinfo_w:.3f} x {pdfinfo_h:.3f} mm, "
                f"pypdf={pypdf_w:.3f} x {pypdf_h:.3f} mm, expected={exp_w:.2f} x {exp_h:.2f} mm."
            )
    else:
        warnings.append("pdfinfo binary not found on PATH; skipped pdfinfo cross-check.")

    # 2. Vector geometry extraction & MGRS 1 km grid spacing / orthogonality check
    with _dense_vector_pypdf_context():
        vectors = _extract_pdf_vectors(reader, 0)
        raw_text = page.extract_text() or ""
    has_vectors = vectors.total_vector_segments >= min_vector_segments
    checks["embedded_vectors"] = has_vectors
    if not has_vectors:
        errors.append(
            f"Too few embedded vector segments ({vectors.total_vector_segments} < {min_vector_segments}); "
            f"PDF may be blank or raster-only (image draws={vectors.image_draw_count})."
        )

    detected_frame = frame_mm or _detect_map_frame_mm(vectors, pypdf_w, pypdf_h, expected_margins_mm)
    fx, fy, fw, fh = detected_frame
    fx_right = fx + fw
    fy_bottom = fy + fh
    border_exclude_mm = 0.75

    # Filter long vertical & horizontal segments strictly inside the map frame
    # (length >= max(15 mm, 25% of frame span) prevents short buildings/roads/contours
    # crossing a grid line from polluting grid skew or position measurements)
    min_v_seg_len = max(15.0, 0.25 * fh)
    min_h_seg_len = max(15.0, 0.25 * fw)
    interior_v = [
        seg
        for seg in vectors.vertical_segments
        if abs(seg[3] - seg[1]) >= min_v_seg_len
        and fx + border_exclude_mm < 0.5 * (seg[0] + seg[2]) < fx_right - border_exclude_mm
        and max(seg[1], seg[3]) >= fy - 1.0
        and min(seg[1], seg[3]) <= fy_bottom + 1.0
    ]
    interior_h = [
        seg
        for seg in vectors.horizontal_segments
        if abs(seg[2] - seg[0]) >= min_h_seg_len
        and fy + border_exclude_mm < 0.5 * (seg[1] + seg[3]) < fy_bottom - border_exclude_mm
        and max(seg[0], seg[2]) >= fx - 1.0
        and min(seg[0], seg[2]) <= fx_right + 1.0
    ]

    v_clusters = _cluster_lines(
        interior_v,
        axis="v",
        span_min=fy,
        span_max=fy_bottom,
        min_coverage_mm=0.85 * fh,
        cluster_tol_mm=0.25,
    )
    h_clusters = _cluster_lines(
        interior_h,
        axis="h",
        span_min=fx,
        span_max=fx_right,
        min_coverage_mm=0.85 * fw,
        cluster_tol_mm=0.25,
    )

    v_xs = [c[0] for c in v_clusters]
    h_ys = [c[0] for c in h_clusters]
    v_skews = [c[2] for c in v_clusters]
    h_skews = [c[2] for c in h_clusters]

    v_spacings = [v_xs[i + 1] - v_xs[i] for i in range(len(v_xs) - 1)]
    h_spacings = [h_ys[i + 1] - h_ys[i] for i in range(len(h_ys) - 1)]

    max_v_err = max((abs(s - exp_grid_mm) for s in v_spacings), default=None)
    max_h_err = max((abs(s - exp_grid_mm) for s in h_spacings), default=None)
    max_v_skew = max(v_skews, default=None)
    max_h_skew = max(h_skews, default=None)

    collar_ticks = _extract_collar_ticks_mm(vectors, detected_frame, border_exclude_mm)

    # Expected minimum count of interior 1 km grid lines
    min_v_lines = max(2, math.floor((fw - 2.0) / exp_grid_mm)) if fw > 2.0 * exp_grid_mm + 2.0 else 1
    min_h_lines = max(2, math.floor((fh - 2.0) / exp_grid_mm)) if fh > 2.0 * exp_grid_mm + 2.0 else 1

    grid_lines_present = len(v_xs) >= min_v_lines and len(h_ys) >= min_h_lines
    checks["mgrs_vector_grid_present"] = grid_lines_present
    if not grid_lines_present:
        tick_diag = ", ".join(f"{edge}:{len(pts)}" for edge, pts in collar_ticks.items())
        errors.append(
            f"Insufficient vector MGRS 1 km grid lines inside map frame: "
            f"found {len(v_xs)} vertical (need >= {min_v_lines}) and {len(h_ys)} horizontal "
            f"(need >= {min_h_lines}). Map interior may be rasterized or missing grid_1km layer! "
            f"(Detected collar ticks: {tick_diag})"
        )

    v_ortho_ok = max_v_skew is not None and max_v_skew < grid_skew_tol_mm
    h_ortho_ok = max_h_skew is not None and max_h_skew < grid_skew_tol_mm
    checks["mgrs_grid_orthogonal"] = v_ortho_ok and h_ortho_ok
    if len(v_xs) > 0 and not v_ortho_ok:
        errors.append(
            f"Vertical MGRS grid lines exceed orthogonality skew limit: "
            f"max skew = {max_v_skew:.4f} mm (limit < {grid_skew_tol_mm:.2f} mm)."
        )
    if len(h_ys) > 0 and not h_ortho_ok:
        errors.append(
            f"Horizontal MGRS grid lines exceed orthogonality skew limit: "
            f"max skew = {max_h_skew:.4f} mm (limit < {grid_skew_tol_mm:.2f} mm)."
        )

    v_spacing_ok = len(v_spacings) >= 1 and max_v_err is not None and max_v_err <= grid_spacing_tol_mm
    h_spacing_ok = len(h_spacings) >= 1 and max_h_err is not None and max_h_err <= grid_spacing_tol_mm
    checks["mgrs_grid_spacing"] = v_spacing_ok and h_spacing_ok
    if not v_spacing_ok:
        errors.append(
            f"Vertical Easting 1 km grid spacing failed physical-scale check at 1:{scale:,}: "
            f"expected {exp_grid_mm:.4f} ± {grid_spacing_tol_mm:.2f} mm, "
            f"measured spacings={[round(s, 3) for s in v_spacings]} mm (max_err={max_v_err})."
        )
    if not h_spacing_ok:
        errors.append(
            f"Horizontal Northing 1 km grid spacing failed physical-scale check at 1:{scale:,}: "
            f"expected {exp_grid_mm:.4f} ± {grid_spacing_tol_mm:.2f} mm, "
            f"measured spacings={[round(s, 3) for s in h_spacings]} mm (max_err={max_h_err})."
        )

    # Cross-check collar tick spacing & alignment with interior grid lines when ticks are present
    tick_alignment_errors: list[str] = []
    for edge, tick_positions in collar_ticks.items():
        if len(tick_positions) >= 2:
            t_spacings = [tick_positions[i + 1] - tick_positions[i] for i in range(len(tick_positions) - 1)]
            t_max_err = max(abs(s - exp_grid_mm) for s in t_spacings)
            if t_max_err > grid_spacing_tol_mm:
                tick_alignment_errors.append(
                    f"{edge} collar tick spacing error {t_max_err:.3f} mm > {grid_spacing_tol_mm:.2f} mm "
                    f"(spacings={[round(s, 3) for s in t_spacings]})"
                )
        ref_lines = v_xs if edge in ("top", "bottom") else h_ys
        if tick_positions and ref_lines:
            for tp in tick_positions:
                nearest = min(abs(tp - gl) for gl in ref_lines)
                if nearest > 0.30:
                    tick_alignment_errors.append(
                        f"{edge} collar tick at {tp:.2f} mm is {nearest:.2f} mm away from nearest interior grid line"
                    )
                    break

    checks["collar_ticks_aligned"] = len(tick_alignment_errors) == 0
    if tick_alignment_errors:
        errors.extend(tick_alignment_errors)
    elif all(len(v) == 0 for v in collar_ticks.values()):
        warnings.append(
            "No short vector tick segments detected outside neatline; check tick geometry visually."
        )

    # 3. Vector text & required collar content
    norm_text = re.sub(r"\s+", " ", raw_text).strip()

    # Fallback diagnostic via pdftotext if pypdf yielded empty text
    if not norm_text:
        alt_text = _query_pdftotext(path)
        if alt_text and alt_text.strip():
            warnings.append(
                "pypdf extract_text() returned empty string while pdftotext succeeded; "
                "check Qt TextFormatAlwaysText font CMap embedding."
            )

    def _check_text(check_name: str, pattern: str, desc: str, regex: bool = False) -> None:
        if regex:
            ok = re.search(pattern, norm_text, flags=re.IGNORECASE) is not None
        else:
            ok = pattern.lower() in norm_text.lower()
        checks[check_name] = ok
        if not ok:
            snippet = norm_text[:300] + ("..." if len(norm_text) > 300 else "")
            errors.append(
                f"Missing required collar text ({desc}): expected {pattern!r} in extracted PDF text. "
                f"Extracted text preview: {snippet!r}"
            )

    scale_str = f"1:{scale:,}"
    _check_text("text_scale", scale_str, f"scale string {scale_str}")

    if expected_epsg is not None:
        epsg_str = f"EPSG:{expected_epsg}"
        _check_text("text_epsg", epsg_str, f"UTM EPSG code {epsg_str}")

    if expected_gzd is not None:
        _check_text(
            "text_gzd",
            rf"\b{re.escape(expected_gzd)}\b",
            f"MGRS Grid Zone Designator {expected_gzd}",
            regex=True,
        )

    if require_osm_attribution:
        _check_text("text_osm_attribution", "OpenStreetMap", "OSM ODbL attribution")

    if require_copernicus_attribution:
        has_cop = "copernicus" in norm_text.lower()
        has_esa = re.search(r"\bESA\b", norm_text) is not None
        checks["text_copernicus_attribution"] = has_cop and has_esa
        if not (has_cop and has_esa):
            errors.append(
                "Missing required Copernicus DEM attribution ('Copernicus' and 'ESA') in extracted PDF text."
            )

    if expected_title:
        _check_text("text_title", expected_title, f"sheet title {expected_title!r}")

    for idx, required in enumerate(extra_required_text or []):
        _check_text(f"text_extra_{idx}", required, f"required text {required!r}")

    return PDFVerificationReport(
        pdf_path=path,
        file_size_bytes=file_size,
        page_count=page_count,
        pypdf_width_mm=pypdf_w,
        pypdf_height_mm=pypdf_h,
        pdfinfo_width_mm=pdfinfo_w,
        pdfinfo_height_mm=pdfinfo_h,
        expected_width_mm=exp_w,
        expected_height_mm=exp_h,
        scale=scale,
        expected_grid_spacing_mm=exp_grid_mm,
        detected_frame_mm=detected_frame,
        vertical_grid_x_mm=v_xs,
        horizontal_grid_y_mm=h_ys,
        vertical_spacings_mm=v_spacings,
        horizontal_spacings_mm=h_spacings,
        max_vertical_spacing_err_mm=max_v_err,
        max_horizontal_spacing_err_mm=max_h_err,
        max_vertical_skew_mm=max_v_skew,
        max_horizontal_skew_mm=max_h_skew,
        collar_ticks_mm=collar_ticks,
        total_vector_segments=vectors.total_vector_segments,
        image_draw_count=vectors.image_draw_count,
        extracted_text=norm_text,
        checks=checks,
        errors=errors,
        warnings=warnings,
    )


def assert_pdf_valid(pdf_path: Path | str, **kwargs: Any) -> PDFVerificationReport:
    """Run verify_pdf and raise AssertionError with a full diagnostic report on failure."""
    report = verify_pdf(pdf_path, **kwargs)
    if not report.passed:
        raise AssertionError(report.summary())
    return report


def rasterize_pdf_for_inspection(
    pdf_path: Path | str,
    out_dir: Path | str,
    *,
    prefix: str | None = None,
    full_dpi: int = 150,
    crop_dpi: int = 300,
    margins_mm: tuple[float, float, float, float] = DEFAULT_MARGINS_MM,
) -> dict[str, Path]:
    """Rasterize full sheet (150 DPI) and 300 DPI zoomed detail crops via `pdftoppm`.

    Produces:
      - ``full``: full sheet at ``full_dpi``
      - ``center``: 300 DPI zoomed crop of the map center (contours, labels, hillshade, roads, grid)
      - ``ticks_tl``: 300 DPI zoomed crop of top-left header, neatline, and MGRS collar ticks
      - ``collar_left``: 300 DPI zoomed crop of bottom-left collar (declination SVG + scale bars)
      - ``collar_right``: 300 DPI zoomed crop of bottom-right collar (grid ref box + legend + footer)
    """
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        raise RuntimeError("`pdftoppm` (poppler-utils) is required for visual rasterization.")

    src = Path(pdf_path)
    dst_dir = Path(out_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    stem = prefix or src.stem

    reader = PdfReader(str(src))
    page = reader.pages[0]
    w_mm = float(page.mediabox.width) * PT_TO_MM
    h_mm = float(page.mediabox.height) * PT_TO_MM
    rotate = int(page.get("/Rotate", 0) or 0) % 360
    if rotate in (90, 270):
        w_mm, h_mm = h_mm, w_mm

    mm_to_px = crop_dpi / 25.4
    px_w = max(1, int(round(w_mm * mm_to_px)))
    px_h = max(1, int(round(h_mm * mm_to_px)))

    outputs: dict[str, Path] = {}

    # 1. Full sheet rasterization
    full_prefix = dst_dir / f"{stem}_full"
    subprocess.run(
        [pdftoppm, "-png", "-r", str(full_dpi), "-singlefile", str(src), str(full_prefix)],
        check=True,
        capture_output=True,
        text=True,
    )
    outputs["full"] = full_prefix.with_suffix(".png")

    m_top, m_right, m_bottom, m_left = margins_mm
    fx_mm = m_left
    fy_mm = m_top
    fw_mm = w_mm - m_left - m_right
    fh_mm = h_mm - m_top - m_bottom

    def run_crop(name: str, x: int, y: int, w: int, h: int) -> None:
        cx = max(0, min(x, px_w - 1))
        cy = max(0, min(y, px_h - 1))
        cw = max(1, min(w, px_w - cx))
        ch = max(1, min(h, px_h - cy))
        crop_prefix = dst_dir / f"{stem}_{name}"
        subprocess.run(
            [
                pdftoppm,
                "-png",
                "-r",
                str(crop_dpi),
                "-x",
                str(cx),
                "-y",
                str(cy),
                "-W",
                str(cw),
                "-H",
                str(ch),
                "-singlefile",
                str(src),
                str(crop_prefix),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        outputs[name] = crop_prefix.with_suffix(".png")

    # 2. Map center crop (~90 mm x 90 mm at 300 DPI)
    center_w = min(px_w, int(round(90.0 * mm_to_px)))
    center_h = min(px_h, int(round(90.0 * mm_to_px)))
    center_x = int(round((fx_mm + fw_mm / 2.0) * mm_to_px)) - center_w // 2
    center_y = int(round((fy_mm + fh_mm / 2.0) * mm_to_px)) - center_h // 2
    run_crop("center", center_x, center_y, center_w, center_h)

    # 3. Top-left header, neatline & MGRS ticks (~110 mm x 85 mm)
    run_crop("ticks_tl", 0, 0, int(round(110.0 * mm_to_px)), int(round(85.0 * mm_to_px)))

    # 4. Bottom collar left & right halves (covering neatline bottom ticks through footer)
    collar_y = max(0, int(round((h_mm - m_bottom - 6.0) * mm_to_px)))
    collar_h = px_h - collar_y
    half_w = px_w // 2 + int(round(8.0 * mm_to_px))
    run_crop("collar_left", 0, collar_y, half_w, collar_h)
    run_crop("collar_right", max(0, px_w - half_w), collar_y, half_w, collar_h)

    return outputs


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify GridForge topographic PDF physical dimensions, MGRS 1 km grid scale, and collar text."
    )
    parser.add_argument("pdf", type=Path, help="Path to rendered PDF file.")
    parser.add_argument("--scale", type=int, default=25000, help="Map scale denominator (default: 25000).")
    parser.add_argument(
        "--paper",
        choices=sorted(PAPER_DIMENSIONS_MM),
        default="a4",
        help="Paper size (default: a4).",
    )
    parser.add_argument(
        "--orientation",
        choices=("portrait", "landscape"),
        default="portrait",
        help="Page orientation (default: portrait).",
    )
    parser.add_argument("--epsg", type=int, default=None, help="Expected UTM EPSG code (e.g. 32756).")
    parser.add_argument("--gzd", type=str, default=None, help="Expected MGRS GZD (e.g. 56H).")
    parser.add_argument("--title", type=str, default=None, help="Expected sheet title substring.")
    parser.add_argument(
        "--no-osm",
        action="store_true",
        help="Do not require OpenStreetMap attribution.",
    )
    parser.add_argument(
        "--no-copernicus",
        action="store_true",
        help="Do not require Copernicus/ESA DEM attribution (e.g. for local DEM tests).",
    )
    parser.add_argument(
        "--min-size-bytes",
        type=int,
        default=50_000,
        help="Minimum PDF file size in bytes (default: 50000).",
    )
    parser.add_argument(
        "--rasterize-dir",
        type=Path,
        default=None,
        help="Optional directory to rasterize full PNG + 300 DPI detail crops via pdftoppm.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    report = verify_pdf(
        args.pdf,
        scale=args.scale,
        paper=args.paper,
        orientation=args.orientation,
        expected_epsg=args.epsg,
        expected_gzd=args.gzd,
        expected_title=args.title,
        require_osm_attribution=not args.no_osm,
        require_copernicus_attribution=not args.no_copernicus,
        min_size_bytes=args.min_size_bytes,
    )

    if args.rasterize_dir is not None:
        pngs = rasterize_pdf_for_inspection(args.pdf, args.rasterize_dir)
        if not args.as_json:
            print("Rasterized inspection PNGs:")
            for label, png_path in pngs.items():
                print(f"  {label}: {png_path}")

    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.summary())

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
