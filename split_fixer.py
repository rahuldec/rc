"""Generic detector + fixer for tables that split across a page break.

Works on PDFs (like these school report cards) where a table's header row
is repeated as the first thing on the next page when the table didn't fit —
the repeated header is treated as the "this table continues" signal. Rather
than trying to reconstruct fonts/borders exactly, both halves that need to
move are captured as high-DPI images and placed at their new positions, so
this doesn't depend on any specific font, column count, or border style.

Usage:
    import fitz, split_fixer
    doc = fitz.open("report.pdf")
    result = split_fixer.fix_document(doc)
    doc.save("report-fixed.pdf")
"""
from dataclasses import dataclass, field
from typing import Optional

import fitz

BORDER_MAX_HEIGHT = 2.5
BORDER_MIN_WIDTH = 15
Y_BUCKET = 1.0
SAME_LINE_TOLERANCE = 2.5  # merge border segments this close together into one line


@dataclass
class TableGroup:
    top: float
    header_bottom: float
    bottom: float
    left: float
    right: float
    header_text: str


@dataclass
class SplitCandidate:
    page_i: int
    page_i1: int
    table_last: TableGroup
    table_first: TableGroup


@dataclass
class FixResult:
    splits_fixed: int = 0
    overflow_risk_pages: list = field(default_factory=list)
    details: list = field(default_factory=list)


def _horizontal_border_ys(page: "fitz.Page"):
    """Return (y, x0, x1) for each near-full-width horizontal black border segment."""
    buckets = {}
    for dr in page.get_drawings():
        if dr.get("fill") != (0.0, 0.0, 0.0):
            continue
        r = dr["rect"]
        if r.height > BORDER_MAX_HEIGHT or r.width < BORDER_MIN_WIDTH:
            continue
        y = round((r.y0 + r.y1) / 2 / Y_BUCKET) * Y_BUCKET
        b = buckets.setdefault(y, [r.x0, r.x1])
        b[0] = min(b[0], r.x0)
        b[1] = max(b[1], r.x1)
    return sorted((y, x0, x1) for y, (x0, x1) in buckets.items())


def find_table_groups(page: "fitz.Page") -> list:
    """Cluster a page's horizontal border lines into distinct tables.

    Row heights inside a table vary a lot here (a wrapped 3-line description
    row can be taller than the whitespace between two separate tables), so a
    fixed y-gap threshold can't tell "next row" from "next table" apart. What
    does distinguish them: the gap between two boundaries that belong to the
    same table is filled with that row's text; the gap between one table's
    closing border and the next table's opening border is blank. So a new
    table starts wherever a boundary-to-boundary gap has no text in it.
    """
    raw_lines = _horizontal_border_ys(page)
    if len(raw_lines) < 2:
        return []

    # Merge near-duplicate y's from rounding (same physical line).
    lines = [raw_lines[0]]
    for y, x0, x1 in raw_lines[1:]:
        py, px0, px1 = lines[-1]
        if y - py <= SAME_LINE_TOLERANCE:
            lines[-1] = (py, min(px0, x0), max(px1, x1))
        else:
            lines.append((y, x0, x1))

    text_dict = page.get_text("dict")
    spans = [
        s
        for b in text_dict["blocks"]
        if "lines" in b
        for l in b["lines"]
        for s in l["spans"]
        if s["text"].strip()
    ]

    def has_text_between(y0: float, y1: float) -> bool:
        return any(y0 - 0.5 <= (s["bbox"][1] + s["bbox"][3]) / 2 <= y1 + 0.5 for s in spans)

    groups = [[lines[0]]]
    for prev, cur in zip(lines, lines[1:]):
        if has_text_between(prev[0], cur[0]):
            groups[-1].append(cur)
        else:
            groups.append([cur])

    tables = []
    for g in groups:
        if len(g) < 2:
            continue  # need at least a top + one row boundary
        top = g[0][0]
        header_bottom = g[1][0]
        bottom = g[-1][0]
        left = min(x0 for _, x0, _ in g)
        right = max(x1 for _, _, x1 in g)
        header_spans = [
            s for s in spans if top - 0.5 <= (s["bbox"][1] + s["bbox"][3]) / 2 < header_bottom
        ]
        header_spans.sort(key=lambda s: s["bbox"][0])
        header_text = " ".join(s["text"].strip() for s in header_spans)
        tables.append(TableGroup(top, header_bottom, bottom, left, right, header_text))
    return tables


def _content_bottom(page: "fitz.Page", below_y: float) -> float:
    """Bottom-most extent (y1) of any text/drawing on the page at or below `below_y`."""
    bottom = below_y
    for b in page.get_text("dict")["blocks"]:
        if b.get("bbox", (0, 0, 0, 0))[1] >= below_y - 1:
            bottom = max(bottom, b["bbox"][3])
    for dr in page.get_drawings():
        if dr["rect"].y0 >= below_y - 1:
            bottom = max(bottom, dr["rect"].y1)
    return bottom


def detect_splits(doc: "fitz.Document") -> list:
    candidates = []
    page_tables = [find_table_groups(doc[i]) for i in range(doc.page_count)]
    for i in range(doc.page_count - 1):
        tables_i = page_tables[i]
        tables_i1 = page_tables[i + 1]
        if not tables_i or not tables_i1:
            continue
        t_last = tables_i[-1]
        t_first = tables_i1[0]
        if not t_last.header_text or t_last.header_text != t_first.header_text:
            continue
        if abs(t_last.left - t_first.left) > 5 or abs(t_last.right - t_first.right) > 5:
            continue
        # last table on page i should be the last thing on that page (touching the break)
        page_bottom_content = _content_bottom(doc[i], t_last.bottom)
        if page_bottom_content - t_last.bottom > 20:
            continue
        candidates.append(SplitCandidate(i, i + 1, t_last, t_first))
    return candidates


def _snapshot_page(doc: "fitz.Document", page_index: int) -> "fitz.Document":
    """An independent single-page copy of doc[page_index], unaffected by later
    edits to `doc` — lets us clear/rewrite the live page while still being able
    to pull its original content from the snapshot."""
    snap = fitz.open()
    snap.insert_pdf(doc, from_page=page_index, to_page=page_index)
    return snap


def fix_split(doc: "fitz.Document", split: SplitCandidate) -> str:
    """Merge one detected split in place. Returns 'fixed' or 'overflow_risk'.

    Both moved fragments are re-embedded as vector content (via show_pdf_page
    from an independent page snapshot), not rasterized — this keeps text
    selectable/searchable and avoids bloating the file with high-DPI images.
    """
    page_i = doc[split.page_i]
    page_i1 = doc[split.page_i1]
    t_last = split.table_last
    t_first = split.table_first

    left = min(t_last.left, t_first.left) - 1
    right = max(t_last.right, t_first.right) + 1

    # Snapshot both pages BEFORE any edits — page i+1 needs its own snapshot too
    # since fragment B's source and target rects are both on page i+1 itself.
    snap_i = _snapshot_page(doc, split.page_i)
    snap_i1 = _snapshot_page(doc, split.page_i1)

    # Fragment A: the rows on page i that need to move up onto page i+1
    frag_a_rect = fitz.Rect(t_last.left, t_last.header_bottom, t_last.right, t_last.bottom)
    frag_a_height = frag_a_rect.height

    # Fragment B: everything on page i+1 currently below its own header (old body rows + rest of page)
    content_bottom = _content_bottom(page_i1, t_first.header_bottom)
    frag_b_rect = fitz.Rect(t_first.left, t_first.header_bottom, t_first.right, content_bottom + 2)
    frag_b_height = frag_b_rect.height

    # Clear the moved region on page i (leaves the rest of that page untouched)
    page_i.add_redact_annot(fitz.Rect(left, t_last.top - 1, right, t_last.bottom + 1), fill=(1, 1, 1))
    page_i.apply_redactions()

    # Clear everything below page i+1's header (we're about to reinsert it, shifted)
    page_i1.add_redact_annot(fitz.Rect(left, t_first.header_bottom - 1, right, frag_b_rect.y1 + 1), fill=(1, 1, 1))
    page_i1.apply_redactions()

    new_a_top = t_first.header_bottom
    new_a_bottom = new_a_top + frag_a_height
    page_i1.show_pdf_page(
        fitz.Rect(t_last.left, new_a_top, t_last.right, new_a_bottom), snap_i, 0, clip=frag_a_rect
    )

    new_b_top = new_a_bottom
    new_b_bottom = new_b_top + frag_b_height
    page_i1.show_pdf_page(
        fitz.Rect(t_first.left, new_b_top, t_first.right, new_b_bottom), snap_i1, 0, clip=frag_b_rect
    )

    snap_i.close()
    snap_i1.close()

    safe_bottom = page_i1.rect.height - t_first.top  # mirror the top margin
    if new_b_bottom > safe_bottom:
        return "overflow_risk"
    return "fixed"


def fix_document(doc: "fitz.Document", max_passes: int = 200) -> FixResult:
    result = FixResult()
    for _ in range(max_passes):
        splits = detect_splits(doc)
        if not splits:
            break
        # Fix the first one, then re-detect from scratch since page content shifted.
        split = splits[0]
        status = fix_split(doc, split)
        result.splits_fixed += 1
        result.details.append(
            {
                "pages": [split.page_i + 1, split.page_i1 + 1],
                "header": split.table_last.header_text,
                "status": status,
            }
        )
        if status == "overflow_risk":
            result.overflow_risk_pages.append(split.page_i1 + 1)
    return result
