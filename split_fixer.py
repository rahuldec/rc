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
MAX_ROW_GAP = 70  # boundaries farther apart than this are never the same table's row,
                   # even with unrelated unbordered text (headings, signatures) between
                   # them — the tallest legitimate wrapped row seen is ~60pt


@dataclass
class TableGroup:
    boundaries: list  # row-boundary y-coordinates, ascending; len == n_rows + 1
    row_texts: list  # text per row band, top to bottom; len == n_rows
    left: float
    right: float

    @property
    def top(self) -> float:
        return self.boundaries[0]

    @property
    def bottom(self) -> float:
        return self.boundaries[-1]

    @property
    def header_text(self) -> str:
        """First row's text — a quick single-row signature, mainly for logging.
        Actual split detection compares row-by-row (see _matching_header_rows)
        since a table's header isn't always exactly one row."""
        return self.row_texts[0] if self.row_texts else ""


@dataclass
class SplitCandidate:
    page_i: int
    page_i1: int
    table_last: TableGroup
    table_first: TableGroup
    header_rows: int  # how many leading rows are the shared, unmoving header


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
    fixed y-gap threshold alone can't tell "next row" from "next table" apart.
    What mostly distinguishes them: the gap between two boundaries that belong
    to the same table is filled with that row's text; the gap between one
    table's closing border and the next table's opening border is blank. So a
    new table starts wherever a boundary-to-boundary gap has no text in it —
    *unless* the gap is unusually large (past MAX_ROW_GAP), in which case it's
    treated as a new table regardless of text in between. That catches
    unrelated, unbordered text sitting between two separate tables (e.g. a
    "Congratulations!" line and signature blanks between an achievements box
    and a reference table below it) that would otherwise glue them together.
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
        gap = cur[0] - prev[0]
        if gap <= MAX_ROW_GAP and has_text_between(prev[0], cur[0]):
            groups[-1].append(cur)
        else:
            groups.append([cur])

    tables = []
    for g in groups:
        if len(g) < 2:
            continue  # need at least a top + one row boundary
        boundaries = [y for y, _, _ in g]
        left = min(x0 for _, x0, _ in g)
        right = max(x1 for _, _, x1 in g)
        row_texts = []
        for row_top, row_bottom in zip(boundaries, boundaries[1:]):
            row_spans = [
                s for s in spans if row_top - 0.5 <= (s["bbox"][1] + s["bbox"][3]) / 2 < row_bottom
            ]
            row_spans.sort(key=lambda s: (round(s["bbox"][1]), s["bbox"][0]))
            row_texts.append(" ".join(s["text"].strip() for s in row_spans))
        tables.append(TableGroup(boundaries, row_texts, left, right))
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


def _matching_header_rows(t_last: TableGroup, t_first: TableGroup) -> int:
    """How many leading rows have identical text on both fragments — that's
    the shared, unmoving header. A table's header isn't always one row (e.g.
    a title row plus a subtitle row before the real data starts), so this
    walks row by row from the top instead of assuming a fixed count."""
    count = 0
    for a, b in zip(t_last.row_texts, t_first.row_texts):
        if not a or a != b:
            break
        count += 1
    return count


def detect_splits(doc: "fitz.Document", progress_callback=None) -> list:
    """progress_callback(pages_scanned, total_pages), called once per page."""
    candidates = []
    n = doc.page_count
    page_tables = []
    for i in range(n):
        page_tables.append(find_table_groups(doc[i]))
        if progress_callback:
            progress_callback(i + 1, n)
    for i in range(doc.page_count - 1):
        tables_i = page_tables[i]
        tables_i1 = page_tables[i + 1]
        if not tables_i or not tables_i1:
            continue
        t_last = tables_i[-1]
        t_first = tables_i1[0]
        header_rows = _matching_header_rows(t_last, t_first)
        if header_rows == 0:
            continue
        if abs(t_last.left - t_first.left) > 5 or abs(t_last.right - t_first.right) > 5:
            continue
        # last table on page i should be the last thing on that page (touching the break)
        page_bottom_content = _content_bottom(doc[i], t_last.bottom)
        if page_bottom_content - t_last.bottom > 20:
            continue
        candidates.append(SplitCandidate(i, i + 1, t_last, t_first, header_rows))
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

    # bottom of the last shared header row (there can be more than one, e.g. a
    # title row plus a subtitle row) — computed separately per page since the
    # two copies' row heights can differ by a hair even when the text matches.
    header_bottom_i = t_last.boundaries[split.header_rows]
    header_bottom_i1 = t_first.boundaries[split.header_rows]

    left = min(t_last.left, t_first.left) - 1
    right = max(t_last.right, t_first.right) + 1

    # Snapshot both pages BEFORE any edits — page i+1 needs its own snapshot too
    # since fragment B's source and target rects are both on page i+1 itself.
    snap_i = _snapshot_page(doc, split.page_i)
    snap_i1 = _snapshot_page(doc, split.page_i1)

    # Fragment A: the rows on page i that need to move up onto page i+1
    frag_a_rect = fitz.Rect(t_last.left, header_bottom_i, t_last.right, t_last.bottom)
    frag_a_height = frag_a_rect.height

    # Fragment B: everything on page i+1 currently below its own header (old body rows + rest of page)
    content_bottom = _content_bottom(page_i1, header_bottom_i1)
    frag_b_rect = fitz.Rect(t_first.left, header_bottom_i1, t_first.right, content_bottom + 2)
    frag_b_height = frag_b_rect.height

    # Clear the moved region on page i (leaves the rest of that page untouched)
    page_i.add_redact_annot(fitz.Rect(left, t_last.top - 1, right, t_last.bottom + 1), fill=(1, 1, 1))
    page_i.apply_redactions()

    # Clear everything below page i+1's header (we're about to reinsert it, shifted)
    page_i1.add_redact_annot(fitz.Rect(left, header_bottom_i1 - 1, right, frag_b_rect.y1 + 1), fill=(1, 1, 1))
    page_i1.apply_redactions()

    new_a_top = header_bottom_i1
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


def fix_document(doc: "fitz.Document", max_passes: int = 10, progress_callback=None) -> FixResult:
    """Detect and fix every split table in the document.

    progress_callback(phase, current, total), if given, is called repeatedly:
    phase='scan' while reading pages to find splits, phase='fix' as each one
    is merged. A full re-scan only happens once per pass (not once per fix) —
    within a pass, splits are applied in one sweep and any split whose page
    was already touched earlier in the same pass is deferred to the next
    pass, where a fresh scan reflects its current state. In practice nearly
    every document resolves in a single pass; extra passes only matter for
    the rare case of a table split across three or more consecutive pages.
    """
    result = FixResult()
    total_hint = 0
    for pass_num in range(max_passes):
        def scan_progress(cur, tot):
            if progress_callback:
                progress_callback("scan", cur, tot)

        splits = detect_splits(doc, progress_callback=scan_progress)
        if not splits:
            break
        if pass_num == 0:
            total_hint = len(splits)

        touched_pages = set()
        for split in splits:
            if split.page_i in touched_pages or split.page_i1 in touched_pages:
                continue  # this page changed earlier in this pass; re-detect fresh next pass
            status = fix_split(doc, split)
            touched_pages.add(split.page_i)
            touched_pages.add(split.page_i1)
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
            if progress_callback:
                progress_callback("fix", result.splits_fixed, max(total_hint, result.splits_fixed))
    return result
