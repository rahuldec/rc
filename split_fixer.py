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
MAX_LINE_GAP = 28  # within a real row, consecutive text lines run ~17-18pt apart;
                    # a bigger gap means separate standalone lines (e.g. a
                    # "Congratulations!" heading and a "Grade Incharge" signature
                    # line), not one wrapped paragraph — even a long achievements
                    # paragraph stays dense at ~18pt, however tall it gets overall


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
    # Usually the same shared header repeats on both fragments, so these are
    # equal (see _matching_header_rows). But a lone label with no body at all
    # (e.g. "Achievements:" with nothing after it) isn't repeated on the next
    # page — the whole label has to move, and it lands in front of page i+1's
    # content rather than matching anything already there. That needs two
    # different counts: how much of table_last stays behind (none of it, in
    # that case) vs. how much of table_first is already "the header" that the
    # moved rows should tuck in after (also none, there).
    last_skip: int  # leading rows of table_last that stay put on page i
    first_skip: int  # leading rows of table_first the moved rows are inserted after


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

    def is_dense_flow(y0: float, y1: float) -> bool:
        """True if the text between y0 and y1 reads as one continuous block —
        consecutive text-line centers never more than MAX_LINE_GAP apart.
        False for sparse standalone lines (a heading, blank space, then a
        signature line) even though there IS text somewhere in the gap."""
        line_ys = sorted(
            {
                round((s["bbox"][1] + s["bbox"][3]) / 2)
                for s in spans
                if y0 - 0.5 <= (s["bbox"][1] + s["bbox"][3]) / 2 <= y1 + 0.5
            }
        )
        if not line_ys:
            return False
        return all(b - a <= MAX_LINE_GAP for a, b in zip(line_ys, line_ys[1:]))

    groups = [[lines[0]]]
    for prev, cur in zip(lines, lines[1:]):
        if is_dense_flow(prev[0], cur[0]):
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


def _in_page_bounds(y0: float, y1: float, page_height: float, tol: float = 3.0) -> bool:
    """A page built by embedding a clipped XObject (see fix_split's
    show_pdf_page(..., clip=...)) still carries the source page's full
    content stream, including parts clipped away and never rendered. After
    the clip-to-target transform those land at wild off-page coordinates
    (e.g. y=-674) but still show up in get_text()/get_drawings(). Filter
    anything that isn't actually within the visible page."""
    return y0 >= -tol and y1 <= page_height + tol


def _content_bottom(page: "fitz.Page", below_y: float) -> float:
    """Bottom-most extent (y1) of any text/drawing on the page at or below `below_y`."""
    page_h = page.rect.height
    bottom = below_y
    for b in page.get_text("dict")["blocks"]:
        bbox = b.get("bbox", (0, 0, 0, 0))
        if bbox[1] >= below_y - 1 and _in_page_bounds(bbox[1], bbox[3], page_h):
            bottom = max(bottom, bbox[3])
    for dr in page.get_drawings():
        r = dr["rect"]
        if r.y0 >= below_y - 1 and _in_page_bounds(r.y0, r.y1, page_h):
            bottom = max(bottom, r.y1)
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


def _is_orphaned_label(t: TableGroup) -> bool:
    """A table that's just a single label row ending in ':' with no body at
    all — e.g. an "Achievements:" box that ran out of room before any of its
    content could be drawn. The content shows up on the next page with no
    repeated label of its own, so there's nothing for _matching_header_rows
    to match — this is a separate signal."""
    return len(t.row_texts) == 1 and t.row_texts[0].strip().endswith(":")


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

        if abs(t_last.left - t_first.left) > 5 or abs(t_last.right - t_first.right) > 5:
            continue
        # last table on page i should be the last thing on that page (touching the break)
        page_bottom_content = _content_bottom(doc[i], t_last.bottom)
        if page_bottom_content - t_last.bottom > 20:
            continue

        header_rows = _matching_header_rows(t_last, t_first)
        if header_rows > 0:
            candidates.append(SplitCandidate(i, i + 1, t_last, t_first, header_rows, header_rows))
        elif _is_orphaned_label(t_last):
            # Nothing of t_last stays behind (last_skip=0, the whole label
            # moves) and nothing of t_first is a pre-existing header to tuck
            # in after (first_skip=0, insert right at its top).
            candidates.append(SplitCandidate(i, i + 1, t_last, t_first, 0, 0))
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

    # Row boundary that everything moves relative to — computed separately
    # per page since last_skip/first_skip can differ (orphaned-label case)
    # and even when they're equal, the two copies' row heights can differ by
    # a hair despite matching text.
    header_bottom_i = t_last.boundaries[split.last_skip]
    header_bottom_i1 = t_first.boundaries[split.first_skip]

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


# ---------------------------------------------------------------------------
# Page-count enforcement: some students' content is long enough that even
# after fix_document() merges every split table correctly, their block still
# spans more than the expected page count (fix_document only repositions
# content within existing pages — it never removes a page). This second,
# independent pass finds every student whose block isn't exactly the
# expected length and compacts it back down by scaling the overflow content
# to fit, then physically deleting the now-unneeded page(s).
#
# Run this AFTER fix_document(), not instead of it — it assumes content is
# already correctly ordered and non-duplicated within each page.
# ---------------------------------------------------------------------------

DEFAULT_MARKER_TEXT = "Attainment Record"
DEFAULT_PAGES_PER_STUDENT = 4
DEFAULT_FIXED_PREFIX_PAGES = 2  # leading pages per student that are never compacted


@dataclass
class PageCountResult:
    students_found: int = 0
    students_compacted: int = 0
    students_flagged: list = field(default_factory=list)  # [{start_page, page_count, reason}]
    details: list = field(default_factory=list)  # [{start_page, original_pages, scale}]


def find_student_starts(doc: "fitz.Document", marker_text: str = DEFAULT_MARKER_TEXT) -> list:
    """Page indices where each student's report begins, identified by a
    marker string that appears once at the top of every student's first
    page (e.g. a fixed header like "Attainment Record")."""
    return [i for i in range(doc.page_count) if marker_text in doc[i].get_text()]


def _content_top(page: "fitz.Page") -> float:
    """Topmost extent (y0) of any text/drawing on the page."""
    page_h = page.rect.height
    top = page_h
    for b in page.get_text("dict")["blocks"]:
        bbox = b.get("bbox")
        if bbox and bbox[3] > bbox[1] and _in_page_bounds(bbox[1], bbox[3], page_h):
            top = min(top, bbox[1])
    for dr in page.get_drawings():
        r = dr["rect"]
        if r.height > 0 and _in_page_bounds(r.y0, r.y1, page_h):
            top = min(top, r.y0)
    return top


def _stack_pages(doc: "fitz.Document", page_indices: list):
    """Copy several pages' content (vector, not rasterized) into one tall
    independent canvas page, stacked tightly end to end — each page's own
    top/bottom margins and any blank leftover space (e.g. from an earlier
    content-merge fix) are trimmed away rather than carried over as gaps.
    Returns (canvas_doc, content_height, left, right)."""
    lefts, rights = [], []
    for i in page_indices:
        tables = find_table_groups(doc[i])
        if tables:
            lefts.append(min(t.left for t in tables))
            rights.append(max(t.right for t in tables))
    left = min(lefts) if lefts else 27.75
    right = max(rights) if rights else doc[page_indices[0]].rect.width - 27.75

    budget_h = sum(doc[i].rect.height for i in page_indices) + 50
    canvas = fitz.open()
    cpage = canvas.new_page(-1, width=doc[page_indices[0]].rect.width, height=budget_h)

    y = 0.0
    for i in page_indices:
        src = doc[i]
        top = _content_top(src)
        bottom = _content_bottom(src, 0)
        if bottom <= top:
            continue  # nothing on this page
        h = bottom - top
        cpage.show_pdf_page(fitz.Rect(left, y, right, y + h), doc, i, clip=fitz.Rect(left, top, right, bottom))
        y += h
    return canvas, y, left, right


def _safe_cut_points(page: "fitz.Page", top: float, bottom: float) -> list:
    """Y-coordinates between `top` and `bottom` where it's safe to cut this
    page's content without slicing through a table row or a line of text —
    the whitespace gaps between tables and between standalone lines. Always
    includes `top` and `bottom` themselves as valid (empty) cut points."""
    unsafe = [(t.top, t.bottom) for t in find_table_groups(page)]

    for b in page.get_text("dict")["blocks"]:
        if "lines" not in b:
            continue
        for l in b["lines"]:
            ys = [s["bbox"][1] for s in l["spans"] if s["text"].strip()]
            ys += [s["bbox"][3] for s in l["spans"] if s["text"].strip()]
            if not ys:
                continue
            y0, y1 = min(ys), max(ys)
            if any(t0 - 1 <= y0 and y1 <= t1 + 1 for t0, t1 in unsafe):
                continue  # already covered by a table's own zone
            unsafe.append((y0, y1))

    unsafe = sorted(z for z in unsafe if z[1] > top and z[0] < bottom)
    merged = []
    for y0, y1 in unsafe:
        y0, y1 = max(y0, top), min(y1, bottom)
        if merged and y0 <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], y1))
        else:
            merged.append((y0, y1))

    cuts = [top]
    for (_, a1), (b0, _) in zip(merged, merged[1:]):
        cuts.append((a1 + b0) / 2)
    cuts.append(bottom)
    return sorted(set(cuts))


def _slice_at_safe_points(cuts: list, n_pages: int, max_slice_h: float):
    """Greedily place n_pages slices using only points from `cuts`, each as
    tall as possible without exceeding max_slice_h. Returns None if the
    final page would still overflow (caller should shrink further and
    retry) — this can happen when a safe point forces an earlier page to
    end well short of its budget, leaving too much for the last page."""
    slices = []
    y0 = cuts[0]
    for _ in range(n_pages - 1):
        limit = y0 + max_slice_h
        candidates = [c for c in cuts if y0 < c <= limit + 1e-6]
        if not candidates:
            return None
        y1 = max(candidates)
        slices.append((y0, y1))
        y0 = y1
    y_end = cuts[-1]
    if y_end - y0 > max_slice_h + 1e-6:
        return None
    slices.append((y0, y_end))
    return slices


def _compact_student_pages(
    doc: "fitz.Document", first_page: int, page_count: int, target_page_count: int, fixed_prefix_pages: int
) -> float:
    """Compact one student's overflow pages down to exactly
    (target_page_count - fixed_prefix_pages) pages, scaling content down
    only if it doesn't already fit at full size. Mutates doc in place.
    Returns the scale factor used (1.0 means no shrinking was needed)."""
    overflow_start = first_page + fixed_prefix_pages
    overflow_end = first_page + page_count  # exclusive
    overflow_indices = list(range(overflow_start, overflow_end))
    n_target_pages = target_page_count - fixed_prefix_pages

    page_w = doc[overflow_start].rect.width
    page_h = doc[overflow_start].rect.height
    top_margin = min(_content_top(doc[i]) for i in overflow_indices)
    usable_h = (page_h - top_margin) - top_margin  # mirror top margin at the bottom

    canvas, content_h, left, right = _stack_pages(doc, overflow_indices)
    cuts = _safe_cut_points(canvas[0], 0.0, content_h)

    budget = usable_h * n_target_pages
    scale = min(1.0, budget / content_h * 0.995) if content_h > 0 else 1.0

    slices = None
    for _ in range(40):
        slices = _slice_at_safe_points(cuts, n_target_pages, usable_h / scale)
        if slices is not None:
            break
        scale *= 0.97  # a safe cut point forced a page under-full; shrink a bit more and retry
    if slices is None:
        # Pathological case (a single uncuttable block taller than one page even
        # heavily shrunk) — fall back to fixed-height slicing so this doesn't
        # crash; it may cut mid-table, but only ever for content that couldn't
        # be made to fit any other way.
        src_slice_h = usable_h / scale
        slices, y0 = [], 0.0
        while y0 < content_h - 0.01 and len(slices) < n_target_pages:
            y1 = min(content_h, y0 + src_slice_h)
            slices.append((y0, y1))
            y0 = y1
        while len(slices) < n_target_pages:
            slices.append((content_h, content_h))

    insert_at = overflow_end
    for k in range(n_target_pages):
        doc.new_page(insert_at + k, width=page_w, height=page_h)
    for k, (y0, y1) in enumerate(slices):
        target_page = doc[insert_at + k]
        target_h = (y1 - y0) * scale
        target_page.show_pdf_page(
            fitz.Rect(left, top_margin, right, top_margin + target_h),
            canvas, 0, clip=fitz.Rect(left, y0, right, y1),
            keep_proportion=False,
        )

    canvas.close()
    doc.delete_pages(overflow_start, overflow_end - 1)
    return scale


def enforce_page_count(
    doc: "fitz.Document",
    pages_per_student: int = DEFAULT_PAGES_PER_STUDENT,
    fixed_prefix_pages: int = DEFAULT_FIXED_PREFIX_PAGES,
    marker_text: str = DEFAULT_MARKER_TEXT,
    progress_callback=None,
) -> PageCountResult:
    """Make every student's report exactly `pages_per_student` pages long.
    Call this after fix_document() has already corrected content ordering.

    progress_callback(current, total) is called once per student.
    """
    result = PageCountResult()
    starts = find_student_starts(doc, marker_text)
    result.students_found = len(starts)

    # boundaries include a sentinel for the end of the document
    bounds = list(zip(starts, starts[1:] + [doc.page_count]))

    # Process from the last student to the first so page indices for
    # students not yet processed stay valid as earlier ones are resized.
    for idx, (start, end) in reversed(list(enumerate(bounds))):
        page_count = end - start
        if progress_callback:
            progress_callback(len(bounds) - idx, len(bounds))
        if page_count == pages_per_student:
            continue
        if page_count < pages_per_student:
            result.students_flagged.append(
                {"start_page": start + 1, "page_count": page_count, "reason": "fewer pages than expected"}
            )
            continue
        if end - (start + fixed_prefix_pages) <= 0:
            result.students_flagged.append(
                {"start_page": start + 1, "page_count": page_count, "reason": "unexpected structure"}
            )
            continue
        scale = _compact_student_pages(doc, start, page_count, pages_per_student, fixed_prefix_pages)
        result.students_compacted += 1
        result.details.append({"start_page": start + 1, "original_pages": page_count, "scale": round(scale, 3)})
    return result
