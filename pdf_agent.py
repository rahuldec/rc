#!/usr/bin/env python3
"""AI agent that edits a PDF's text content from a natural-language instruction.

Reads a PDF, sends its text (page by page) to Claude along with your
instruction, gets back a precise list of find-and-replace edits, and applies
them to the PDF in place using PyMuPDF (redact the old text, draw the new
text in its spot).

Usage:
    python pdf_agent.py input.pdf "Change the invoice date on page 1 to 2026-01-15"
    python pdf_agent.py input.pdf "Redact all SSNs" -o output.pdf
    python pdf_agent.py input.pdf "Fix the typo 'recieve' -> 'receive'" --dry-run
"""
import argparse
import sys
from pathlib import Path

import fitz  # PyMuPDF
import anthropic

MODEL = "claude-opus-5"

EDIT_TOOL = {
    "name": "propose_edits",
    "description": "Propose exact text find-and-replace edits to apply to the PDF.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "page": {
                            "type": "integer",
                            "description": "1-indexed page number containing the text to change",
                        },
                        "find": {
                            "type": "string",
                            "description": (
                                "A short, exact substring of text on that page to locate "
                                "(a few words is ideal). Must match the page text verbatim, "
                                "including case and punctuation."
                            ),
                        },
                        "replace": {
                            "type": "string",
                            "description": "The text to put in its place. Use an empty string to delete/redact.",
                        },
                    },
                    "required": ["page", "find", "replace"],
                    "additionalProperties": False,
                },
            },
            "summary": {
                "type": "string",
                "description": "One or two sentence summary of what changes were made and why",
            },
        },
        "required": ["edits", "summary"],
        "additionalProperties": False,
    },
}


def extract_pages_text(doc: fitz.Document) -> str:
    parts = []
    for i, page in enumerate(doc, start=1):
        parts.append(f"--- PAGE {i} ---\n{page.get_text()}")
    return "\n\n".join(parts)


def propose_edits(client: anthropic.Anthropic, doc_text: str, instruction: str) -> dict:
    response = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        tools=[EDIT_TOOL],
        tool_choice={"type": "tool", "name": "propose_edits"},
        messages=[
            {
                "role": "user",
                "content": (
                    "Here is the full text of a PDF, page by page:\n\n"
                    f"{doc_text}\n\n"
                    f"Instruction: {instruction}\n\n"
                    "Propose the minimal set of find-and-replace edits needed to satisfy the "
                    "instruction. Each `find` value must be a short, exact substring that "
                    "appears verbatim on the given page (so it can be located precisely) — "
                    "prefer a few words over a full sentence or paragraph."
                ),
            }
        ],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "propose_edits":
            return block.input
    raise RuntimeError("Claude did not return a propose_edits tool call")


def apply_edit(page: fitz.Page, find: str, replace: str) -> int:
    """Redact `find` wherever it appears on the page and draw `replace` in its place."""
    rects = page.search_for(find)
    if not rects:
        return 0
    for rect in rects:
        page.add_redact_annot(rect, fill=(1, 1, 1))
    page.apply_redactions()
    if replace:
        for rect in rects:
            fontsize = max(6.0, rect.height * 0.75)
            point = fitz.Point(rect.x0, rect.y1 - rect.height * 0.15)
            page.insert_text(point, replace, fontsize=fontsize, color=(0, 0, 0))
    return len(rects)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Edit a PDF's text content using a natural-language instruction, via Claude."
    )
    parser.add_argument("input_pdf", type=Path)
    parser.add_argument(
        "instruction",
        help="What to change, e.g. 'Change the date on page 1 from 2025 to 2026'",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output PDF path (default: <input>-edited.pdf)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show proposed edits without writing the output PDF",
    )
    args = parser.parse_args()

    if not args.input_pdf.exists():
        print(f"Error: {args.input_pdf} not found", file=sys.stderr)
        sys.exit(1)

    output_path = args.output or args.input_pdf.with_name(args.input_pdf.stem + "-edited.pdf")

    doc = fitz.open(args.input_pdf)
    doc_text = extract_pages_text(doc)

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    result = propose_edits(client, doc_text, args.instruction)

    print(f"Summary: {result['summary']}")
    print(f"Proposed {len(result['edits'])} edit(s):")
    for e in result["edits"]:
        print(f"  page {e['page']}: {e['find']!r} -> {e['replace']!r}")

    if args.dry_run:
        doc.close()
        return

    applied = 0
    skipped = []
    for e in result["edits"]:
        page_index = e["page"] - 1
        if page_index < 0 or page_index >= doc.page_count:
            skipped.append(e)
            continue
        n = apply_edit(doc[page_index], e["find"], e["replace"])
        if n == 0:
            skipped.append(e)
        else:
            applied += 1

    doc.save(output_path)
    doc.close()

    print(f"\nApplied {applied} edit(s). Saved to {output_path}")
    if skipped:
        print(f"Could not locate {len(skipped)} edit(s) verbatim on their page:")
        for e in skipped:
            print(f"  page {e['page']}: {e['find']!r}")


if __name__ == "__main__":
    main()
