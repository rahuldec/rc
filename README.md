# pdf-edit-agent

Two tools for editing PDFs, in this folder:

1. **`pdf_agent.py`** — an AI agent that edits a PDF's text content from a plain-English instruction, using Claude.
2. **`split_fixer.py` + `app.py`** — a deterministic (no AI, no API key needed) tool that detects a table split awkwardly across two pages and merges it back into one continuous table. Ships as both a CLI-importable module and a web upload page.

## Tool 1: `pdf_agent.py` — instruction-driven text edits

1. Extracts the text of every page with PyMuPDF.
2. Sends the full text + your instruction to Claude (`claude-opus-5`), forcing a `propose_edits` tool call that returns a precise list of `{page, find, replace}` edits.
3. Applies each edit to the PDF: redacts (whites out) the located text and draws the replacement text in its place, then saves a new PDF.

Good for: fixing typos, updating dates/names/numbers, redacting specific phrases, rewording a sentence or two. Not a layout/design tool — it edits text in place at its original position and font size, so large text-length changes may look cramped or overflow.

## Setup

```bash
cd pdf-edit-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."   # or `ant auth login`
```

## Usage

```bash
# Preview edits without touching the PDF
python pdf_agent.py input.pdf "Change the invoice date on page 1 to 2026-01-15" --dry-run

# Apply edits, write input-edited.pdf
python pdf_agent.py input.pdf "Fix the typo 'recieve' -> 'receive'"

# Apply edits, custom output path
python pdf_agent.py input.pdf "Redact the SSN on page 2" -o redacted.pdf
```

### Notes

- `find` text must appear verbatim (exact substring, case-sensitive) on the page — Claude is instructed to pick short, distinctive phrases for this reason. If an edit can't be located, it's reported as skipped and left untouched.
- Scanned/image-only PDFs have no extractable text — OCR them first if you need to edit those.
- This performs redaction by drawing a white box over the old text, which is destructive (the original text is actually removed, not just visually hidden) — safe for sharing redacted output.

## Tool 2: split-table fixer — CLI, batch, or web page

Detects tables that break across a page (recognized by a repeated table
header at the top of the next page — the generator's own "this continues"
marker) and merges them into one unbroken table, shifting whatever follows
down to make room. No API key, no AI call — pure PDF geometry, so it's free
and fast even across thousands of files. Falls back safely: if no split is
found, or the fix can't be done without overflowing the page, the file is
left alone (or flagged) rather than guessed at.

### As a library / one-off script

```python
import fitz, split_fixer

doc = fitz.open("report.pdf")
result = split_fixer.fix_document(doc)
print(result)  # FixResult(splits_fixed=1, overflow_risk_pages=[], details=[...])
doc.save("report-fixed.pdf")
```

### As a web page (upload PDF → download fixed PDF)

```bash
source venv/bin/activate
python app.py
# open http://127.0.0.1:5050
```

Runs entirely in memory — nothing is written to disk, logged, or retained.
See [DEPLOY.md](DEPLOY.md) for putting this on a real URL others can use
(includes a password gate, since these are student report cards).

### Batch processing many files at once

```python
import pathlib, fitz, split_fixer

src_dir = pathlib.Path("~/Desktop/Report Cards").expanduser()
out_dir = pathlib.Path("~/Desktop/Report Cards - fixed").expanduser()
out_dir.mkdir(exist_ok=True)

log = []
for pdf_path in src_dir.glob("*.pdf"):
    doc = fitz.open(pdf_path)
    result = split_fixer.fix_document(doc)
    doc.save(out_dir / pdf_path.name)
    doc.close()
    log.append({"file": pdf_path.name, **result.__dict__})

import json
print(json.dumps(log, indent=2))
```
