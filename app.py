"""Web app: upload a PDF, get back a copy with split tables merged.

Runs entirely in memory — the uploaded file and the fixed output are never
written to disk. Nothing is logged or stored after the response is sent.
"""
import io
import os
import traceback
from functools import wraps

import fitz
from flask import Flask, request, send_file, Response

import split_fixer

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB per upload

APP_PASSWORD = os.environ.get("APP_PASSWORD")  # set this in production — see DEPLOY.md


def require_password(view):
    """Gate access behind HTTP Basic Auth if APP_PASSWORD is set. These are
    student report cards; don't leave this open to the whole internet."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not APP_PASSWORD:
            return view(*args, **kwargs)
        auth = request.authorization
        if not auth or auth.password != APP_PASSWORD:
            return Response(
                "Authentication required.", 401,
                {"WWW-Authenticate": 'Basic realm="Fix Split Tables"'},
            )
        return view(*args, **kwargs)
    return wrapped

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fix Split Tables in PDF</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #f5f4f2; color: #1c1b1a; margin: 0;
    min-height: 100vh; display: flex; align-items: center; justify-content: center;
    padding: 24px;
  }
  .card {
    background: #fff; border-radius: 16px; padding: 40px;
    max-width: 480px; width: 100%; box-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 8px 24px rgba(0,0,0,0.06);
  }
  h1 { font-size: 1.35rem; margin: 0 0 8px; }
  p.sub { color: #6b6863; margin: 0 0 28px; font-size: 0.92rem; line-height: 1.5; }
  #drop {
    border: 2px dashed #d8d5d0; border-radius: 12px; padding: 36px 20px;
    text-align: center; cursor: pointer; transition: border-color .15s, background .15s;
  }
  #drop.drag { border-color: #E05A2B; background: #fdf4ef; }
  #drop svg { width: 32px; height: 32px; margin-bottom: 10px; color: #a8a5a0; }
  #drop .main { font-weight: 500; font-size: 0.95rem; }
  #drop .sub2 { color: #9b9893; font-size: 0.82rem; margin-top: 4px; }
  input[type=file] { display: none; }
  #filename { margin-top: 14px; font-size: 0.85rem; color: #4a4844; word-break: break-all; }
  button#go {
    width: 100%; margin-top: 18px; padding: 12px; border: none; border-radius: 10px;
    background: #E05A2B; color: #fff; font-size: 0.95rem; font-weight: 600;
    cursor: pointer; transition: background .15s;
  }
  button#go:disabled { background: #e0ddd8; color: #a8a5a0; cursor: not-allowed; }
  button#go:not(:disabled):hover { background: #c94f24; }
  #status { margin-top: 18px; font-size: 0.88rem; line-height: 1.5; display: none; }
  #status.show { display: block; }
  #status.ok { color: #1a7a3e; }
  #status.warn { color: #a06a00; }
  #status.err { color: #b3261e; }
  .spinner {
    width: 16px; height: 16px; border: 2px solid #e0ddd8; border-top-color: #E05A2B;
    border-radius: 50%; display: inline-block; vertical-align: -3px; margin-right: 8px;
    animation: spin .7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
  <div class="card">
    <h1>Fix split tables in a PDF</h1>
    <p class="sub">Upload a report card (or similar PDF) where a table breaks awkwardly across two pages. If a repeated table header is found, the rows are merged into one continuous table and you get a fixed copy back. Nothing is stored — files are processed in memory and discarded after you download.</p>

    <div id="drop">
      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
        <path stroke-linecap="round" stroke-linejoin="round" d="M12 16.5V9.75m0 0 3 3m-3-3-3 3M6.75 19.5a4.5 4.5 0 0 1-1.41-8.775 5.25 5.25 0 0 1 10.233-2.33 3 3 0 0 1 3.758 3.848A3.752 3.752 0 0 1 18 19.5H6.75Z" />
      </svg>
      <div class="main">Click to choose a PDF, or drag one here</div>
      <div class="sub2">Max 50 MB</div>
      <div id="filename"></div>
    </div>
    <input type="file" id="file" accept="application/pdf">
    <button id="go" disabled>Fix and download</button>
    <div id="status"></div>
  </div>

<script>
const drop = document.getElementById('drop');
const fileInput = document.getElementById('file');
const filenameEl = document.getElementById('filename');
const goBtn = document.getElementById('go');
const statusEl = document.getElementById('status');
let chosenFile = null;

drop.addEventListener('click', () => fileInput.click());
drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('drag'); });
drop.addEventListener('dragleave', () => drop.classList.remove('drag'));
drop.addEventListener('drop', (e) => {
  e.preventDefault(); drop.classList.remove('drag');
  if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', () => { if (fileInput.files.length) setFile(fileInput.files[0]); });

function setFile(f) {
  if (f.type !== 'application/pdf' && !f.name.toLowerCase().endsWith('.pdf')) {
    showStatus('err', 'That doesn\\'t look like a PDF.');
    return;
  }
  chosenFile = f;
  filenameEl.textContent = f.name + '  (' + (f.size / 1024 / 1024).toFixed(1) + ' MB)';
  goBtn.disabled = false;
  statusEl.classList.remove('show');
}

function showStatus(kind, html) {
  statusEl.className = 'show ' + kind;
  statusEl.innerHTML = html;
}

goBtn.addEventListener('click', async () => {
  if (!chosenFile) return;
  goBtn.disabled = true;
  showStatus('ok', '<span class="spinner"></span>Processing…');

  const form = new FormData();
  form.append('file', chosenFile);

  try {
    const resp = await fetch('/fix', { method: 'POST', body: form });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ error: 'Something went wrong.' }));
      showStatus('err', err.error || 'Something went wrong.');
      goBtn.disabled = false;
      return;
    }
    const splitsFixed = parseInt(resp.headers.get('X-Fix-Splits') || '0', 10);
    const overflow = resp.headers.get('X-Fix-Overflow-Risk') === 'true';
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = chosenFile.name.replace(/\\.pdf$/i, '') + '-fixed.pdf';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);

    if (splitsFixed === 0) {
      showStatus('warn', 'No split table found — downloaded a copy unchanged, nothing to fix.');
    } else if (overflow) {
      showStatus('warn', `Fixed ${splitsFixed} split table(s), but the result may run long on one page — please spot-check it.`);
    } else {
      showStatus('ok', `Fixed ${splitsFixed} split table(s). Download started.`);
    }
  } catch (e) {
    showStatus('err', 'Upload failed: ' + e.message);
  }
  goBtn.disabled = false;
});
</script>
</body>
</html>
"""


@app.route("/")
@require_password
def index():
    return PAGE


@app.route("/fix", methods=["POST"])
@require_password
def fix():
    f = request.files.get("file")
    if f is None or f.filename == "":
        return {"error": "No file uploaded."}, 400

    try:
        pdf_bytes = f.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return {"error": "Could not open that file as a PDF."}, 400

    try:
        result = split_fixer.fix_document(doc)
        out_buf = io.BytesIO()
        doc.save(out_buf)
        doc.close()
        out_buf.seek(0)
    except Exception:
        traceback.print_exc()
        return {"error": "Something went wrong while processing this PDF."}, 500

    out_name = (f.filename.rsplit(".", 1)[0] if "." in f.filename else f.filename) + "-fixed.pdf"
    resp = send_file(
        out_buf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=out_name,
    )
    resp.headers["X-Fix-Splits"] = str(result.splits_fixed)
    resp.headers["X-Fix-Overflow-Risk"] = "true" if result.overflow_risk_pages else "false"
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)
