"""Regression + edge-case suite for split_fixer.py.

Two kinds of coverage:
  - Regression tests run against the real production files that have
    surfaced bugs so far. They guard against exactly those bugs
    reappearing.
  - Edge-case tests build small synthetic PDFs in-memory (via PyMuPDF) to
    exercise each of the three split shapes and the page-count boundary
    conditions in isolation, without depending on any real file.

Run with: venv/bin/pytest tests/ -v
"""
import os

import fitz
import pytest

import split_fixer

REAL_FILES = [
    "/Users/rahulsharma/Desktop/6.pdf",
    "/Users/rahulsharma/Desktop/6 satluj.pdf",
    "/Users/rahulsharma/Downloads/Ganges-fixed.pdf",
]

STD_LEFT, STD_RIGHT = 27.75, 566.8


# ---------------------------------------------------------------------------
# Synthetic PDF builders (used by the edge-case tests below)
# ---------------------------------------------------------------------------

def _draw_border(page, y, x0=50, x1=500):
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(x0, y, x1, y + 1.0))
    shape.finish(fill=(0, 0, 0), color=(0, 0, 0))
    shape.commit()


def _build_table_page(doc, rows, start_y=50, x0=50, xmid=200, x1=500, row_h=25):
    """rows: list of (label, value). First row is the table's header.
    Draws one bordered row per entry and returns (page, bottom_y)."""
    page = doc.new_page(-1, width=595, height=842)
    y = start_y
    _draw_border(page, y, x0, x1)
    for label, value in rows:
        page.insert_text((x0 + 4, y + 14), label, fontsize=10)
        page.insert_text((xmid + 4, y + 14), value, fontsize=10)
        y += row_h
        _draw_border(page, y, x0, x1)
    return page, y


def _blank_page(doc, text=None):
    page = doc.new_page(-1, width=595, height=842)
    if text:
        page.insert_text((50, 50), text, fontsize=10)
    return page


# ---------------------------------------------------------------------------
# Fixtures: real files, fully processed once per test session
# ---------------------------------------------------------------------------

def _require_file(path):
    if not os.path.exists(path):
        pytest.skip(f"real test file not present on this machine: {path}")
    return path


@pytest.fixture(scope="session", params=REAL_FILES, ids=["6", "6_satluj", "ganges"])
def processed_doc(request):
    """Each real file, fully run through fix_document + enforce_page_count
    exactly once for the whole session, plus the raw split/page-count
    results so tests can assert on them directly."""
    path = _require_file(request.param)
    doc = fitz.open(path)
    split_result = split_fixer.fix_document(doc)
    page_result = split_fixer.enforce_page_count(doc)
    yield doc, split_result, page_result
    doc.close()


# ---------------------------------------------------------------------------
# Regression tests — one per bug fixed this conversation
# ---------------------------------------------------------------------------

def test_no_students_flagged(processed_doc):
    doc, split_result, page_result = processed_doc
    assert page_result.students_flagged == []


def test_all_students_exactly_4_pages(processed_doc):
    doc, split_result, page_result = processed_doc
    starts = split_fixer.find_student_starts(doc, split_fixer.DEFAULT_MARKER_TEXT)
    assert len(starts) == page_result.students_found
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(g == split_fixer.DEFAULT_PAGES_PER_STUDENT for g in gaps), gaps
    assert doc.page_count == len(starts) * split_fixer.DEFAULT_PAGES_PER_STUDENT


def test_no_content_top_anomalies(processed_doc):
    """Guards the phantom-geometry bug: get_drawings()/get_text() picking up
    content clipped away by an earlier show_pdf_page() embed, reported at
    wild off-page coordinates (e.g. y=-674) and corrupting the compaction
    budget."""
    doc, _, _ = processed_doc
    bad = []
    for i in range(doc.page_count):
        ct = split_fixer._content_top(doc[i])
        if not (-3 <= ct <= doc[i].rect.height + 3):
            bad.append((i, ct))
    assert bad == []


def test_compacted_pages_full_width(processed_doc):
    """Guards the keep_proportion bug: show_pdf_page() defaulting to
    aspect-ratio-preserving scaling narrowed and centered every compacted
    page's tables instead of filling the page width."""
    doc, _, _ = processed_doc
    bad = []
    for i in range(doc.page_count):
        for t in split_fixer.find_table_groups(doc[i]):
            if not split_fixer._looks_like_real_table_header(t):
                continue  # a fake fragment table, not a real content table
            if abs(t.left - STD_LEFT) > 3 or abs(t.right - STD_RIGHT) > 3:
                bad.append((i, t.left, t.right, t.header_text))
    assert bad == []


def test_western_fusion_table_intact(processed_doc):
    """Guards the safe-cut-points bug: duplicated border-line geometry on
    the compaction canvas fooled find_table_groups into seeing 3 fake
    tables instead of 1, letting a compaction cut land mid-table and strand
    "Stage Presence" on the page after "Facial Expressions and Body
    Language" with no repeated header."""
    doc, _, _ = processed_doc
    pages_with_facial = [i for i in range(doc.page_count) if "Facial Expressions" in doc[i].get_text()]
    for i in pages_with_facial:
        assert "Stage Presence" in doc[i].get_text(), (
            f"page {i} has 'Facial Expressions' but not 'Stage Presence' — "
            "the Western Fusion and Folk Dance table got split across pages again"
        )


def test_social_skills_table_intact(processed_doc):
    """Guards the text_only content_bottom bug: some source templates draw a
    row's border but defer its text to the next page, leaving a text-less
    box behind. _content_bottom() counted that empty box as "more content
    after this table," pushing detect_splits' page-bottom gap past its
    threshold and causing the whole split candidate to be silently skipped
    — even though everything else about it (alignment, matching header)
    was a valid split."""
    doc, _, _ = processed_doc
    pages_with_first_row = [i for i in range(doc.page_count) if "Observes courtesy" in doc[i].get_text()]
    for i in pages_with_first_row:
        assert "Follows table etiquettes" in doc[i].get_text(), (
            f"page {i} has 'Observes courtesy' but not 'Follows table etiquettes' — "
            "the Social Skills table got split across pages again"
        )


# ---------------------------------------------------------------------------
# Edge cases — synthetic PDFs, independent of the two real files
# ---------------------------------------------------------------------------

def test_single_row_header_split_is_merged():
    doc = fitz.open()
    _build_table_page(doc, [("Header", "Grade"), ("Row A", "value a"), ("Row B", "value b")])
    _build_table_page(doc, [("Header", "Grade"), ("Row C", "value c"), ("Row D", "value d")])

    result = split_fixer.fix_document(doc)
    assert result.splits_fixed == 1

    all_rows = [t.row_texts for i in range(doc.page_count) for t in split_fixer.find_table_groups(doc[i])]
    assert all_rows == [["Header Grade", "Row A value a", "Row B value b", "Row C value c", "Row D value d"]]
    doc.close()


def test_multi_row_header_split_is_merged():
    doc = fitz.open()
    _build_table_page(doc, [("Grading Pointers", ""), ("Scholastic", "8 Point Scale"), ("Row A", "value a")])
    _build_table_page(doc, [("Grading Pointers", ""), ("Scholastic", "8 Point Scale"), ("Row B", "value b")])

    result = split_fixer.fix_document(doc)
    assert result.splits_fixed == 1

    merged = [t.row_texts for i in range(doc.page_count) for t in split_fixer.find_table_groups(doc[i])]
    assert merged == [["Grading Pointers", "Scholastic 8 Point Scale", "Row A value a", "Row B value b"]]
    doc.close()


def test_orphaned_label_split_is_merged():
    doc = fitz.open()
    _build_table_page(doc, [("Achievements:", "")])
    _build_table_page(doc, [("Won I Position", "in something")])

    result = split_fixer.fix_document(doc)
    assert result.splits_fixed == 1

    merged = [t.row_texts for i in range(doc.page_count) for t in split_fixer.find_table_groups(doc[i])]
    assert merged == [["Achievements:", "Won I Position in something"]]
    doc.close()


def test_student_already_at_target_page_count_is_untouched():
    doc = fitz.open()
    _blank_page(doc, "Attainment Record")
    _blank_page(doc, "page2")
    _blank_page(doc, "page3")
    _blank_page(doc, "page4")

    result = split_fixer.enforce_page_count(doc)
    assert result.students_found == 1
    assert result.students_compacted == 0
    assert result.students_flagged == []
    assert doc.page_count == 4
    doc.close()


def test_student_with_fewer_pages_is_flagged_not_compacted():
    doc = fitz.open()
    _blank_page(doc, "Attainment Record")
    _blank_page(doc, "page2")
    _blank_page(doc, "page3")

    result = split_fixer.enforce_page_count(doc)
    assert result.students_compacted == 0
    assert len(result.students_flagged) == 1
    assert result.students_flagged[0]["reason"] == "fewer pages than expected"
    assert doc.page_count == 3  # left untouched, not silently padded or cut
    doc.close()


def test_student_with_more_pages_is_compacted_to_target():
    doc = fitz.open()
    _blank_page(doc, "Attainment Record")
    _blank_page(doc, "page2")
    _build_table_page(doc, [("Header", "Grade"), ("Row A", "value a")])
    _build_table_page(doc, [("Header2", "Grade")])
    _build_table_page(doc, [("Header3", "Grade")])

    result = split_fixer.enforce_page_count(doc)
    assert result.students_compacted == 1
    assert result.students_flagged == []
    assert doc.page_count == split_fixer.DEFAULT_PAGES_PER_STUDENT
    doc.close()
