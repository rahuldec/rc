"""Web app: upload a PDF, get back a copy with split tables merged.

Runs entirely in memory — the uploaded file and the fixed output are never
written to disk. Nothing is logged or stored after you download the result
(and the job is dropped from memory the moment you do).

Processing runs in a background thread so the page can poll for progress
instead of just staring at a spinner during a single long request. This
means job state lives in this process's memory — the app must run as a
single worker process (see Procfile) so a poll doesn't land on a different
process than the one doing the work.
"""
import io
import threading
import time
import traceback
import uuid

import fitz
from flask import Flask, request, send_file

import split_fixer

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB per upload

JOB_TTL_SECONDS = 3600  # prune jobs nobody came back to download

jobs = {}
jobs_lock = threading.Lock()


def _prune_old_jobs():
    cutoff = time.time() - JOB_TTL_SECONDS
    with jobs_lock:
        for job_id in [j for j, v in jobs.items() if v["created_at"] < cutoff]:
            del jobs[job_id]


def _process_job(job_id: str, pdf_bytes: bytes, out_name: str) -> None:
    def progress(phase, current, total):
        with jobs_lock:
            job = jobs.get(job_id)
            if job is not None:
                job.update(phase=phase, current=current, total=total)

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        with jobs_lock:
            jobs[job_id].update(status="error", error="Could not open that file as a PDF.")
        return

    with jobs_lock:
        jobs[job_id].update(status="processing", phase="scan", total=doc.page_count)

    try:
        result = split_fixer.fix_document(doc, progress_callback=progress)
        out_buf = io.BytesIO()
        doc.save(out_buf)
        doc.close()
    except Exception:
        traceback.print_exc()
        with jobs_lock:
            jobs[job_id].update(status="error", error="Something went wrong while processing this PDF.")
        return

    with jobs_lock:
        jobs[job_id].update(
            status="done",
            bytes=out_buf.getvalue(),
            out_name=out_name,
            splits_fixed=result.splits_fixed,
            overflow_risk=bool(result.overflow_risk_pages),
        )


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

  #progress-wrap { margin-top: 18px; display: none; }
  #progress-wrap.show { display: block; }
  #progress-label { font-size: 0.85rem; color: #4a4844; margin-bottom: 6px; display: flex; justify-content: space-between; }
  #progress-track { height: 8px; border-radius: 4px; background: #eeece8; overflow: hidden; }
  #progress-fill { height: 100%; width: 0%; background: #E05A2B; border-radius: 4px; transition: width .2s ease; }
  #progress-fill.indeterminate { width: 30% !important; animation: indeterminate 1.1s ease-in-out infinite; }
  @keyframes indeterminate {
    0% { margin-left: -30%; }
    100% { margin-left: 100%; }
  }

  #status { margin-top: 14px; font-size: 0.88rem; line-height: 1.5; display: none; }
  #status.show { display: block; }
  #status.ok { color: #1a7a3e; }
  #status.warn { color: #a06a00; }
  #status.err { color: #b3261e; }
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

    <div id="progress-wrap">
      <div id="progress-label"><span id="progress-text">Starting…</span><span id="progress-pct"></span></div>
      <div id="progress-track"><div id="progress-fill" class="indeterminate"></div></div>
    </div>
    <div id="status"></div>
  </div>

<script>
const drop = document.getElementById('drop');
const fileInput = document.getElementById('file');
const filenameEl = document.getElementById('filename');
const goBtn = document.getElementById('go');
const statusEl = document.getElementById('status');
const progressWrap = document.getElementById('progress-wrap');
const progressFill = document.getElementById('progress-fill');
const progressText = document.getElementById('progress-text');
const progressPct = document.getElementById('progress-pct');
let chosenFile = null;
let pollTimer = null;

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

function setProgress(phase, current, total) {
  progressWrap.classList.add('show');
  if (!total) {
    progressFill.classList.add('indeterminate');
    progressFill.style.width = '';
    progressText.textContent = phase === 'fix' ? 'Merging tables…' : 'Reading pages…';
    progressPct.textContent = '';
    return;
  }
  progressFill.classList.remove('indeterminate');
  const pct = Math.min(100, Math.round((current / total) * 100));
  progressFill.style.width = pct + '%';
  progressText.textContent = phase === 'fix'
    ? `Merging table ${current} of ${total}`
    : `Reading page ${current} of ${total}`;
  progressPct.textContent = pct + '%';
}

function resetProgress() {
  progressWrap.classList.remove('show');
  progressFill.classList.add('indeterminate');
  progressFill.style.width = '';
  progressText.textContent = 'Starting…';
  progressPct.textContent = '';
}

async function poll(jobId) {
  try {
    const r = await fetch('/progress/' + jobId);
    if (!r.ok) throw new Error('lost track of the job');
    const j = await r.json();

    if (j.status === 'error') {
      clearInterval(pollTimer);
      resetProgress();
      showStatus('err', j.error || 'Something went wrong.');
      goBtn.disabled = false;
      return;
    }

    setProgress(j.phase, j.current, j.total);

    if (j.status === 'done') {
      clearInterval(pollTimer);
      progressText.textContent = 'Done';
      progressPct.textContent = '100%';
      progressFill.classList.remove('indeterminate');
      progressFill.style.width = '100%';

      window.location.href = '/download/' + jobId;

      if (j.splits_fixed === 0) {
        showStatus('warn', 'No split table found — downloaded a copy unchanged, nothing to fix.');
      } else if (j.overflow_risk) {
        showStatus('warn', `Fixed ${j.splits_fixed} split table(s), but the result may run long on one page — please spot-check it.`);
      } else {
        showStatus('ok', `Fixed ${j.splits_fixed} split table(s). Download started.`);
      }
      goBtn.disabled = false;
    }
  } catch (e) {
    clearInterval(pollTimer);
    resetProgress();
    showStatus('err', 'Lost connection while processing: ' + e.message);
    goBtn.disabled = false;
  }
}

goBtn.addEventListener('click', async () => {
  if (!chosenFile) return;
  goBtn.disabled = true;
  statusEl.classList.remove('show');
  resetProgress();
  progressWrap.classList.add('show');

  const form = new FormData();
  form.append('file', chosenFile);

  try {
    const resp = await fetch('/start', { method: 'POST', body: form });
    const j = await resp.json();
    if (!resp.ok) {
      showStatus('err', j.error || 'Something went wrong.');
      resetProgress();
      goBtn.disabled = false;
      return;
    }
    pollTimer = setInterval(() => poll(j.job_id), 400);
  } catch (e) {
    showStatus('err', 'Upload failed: ' + e.message);
    resetProgress();
    goBtn.disabled = false;
  }
});
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return PAGE


@app.route("/start", methods=["POST"])
def start():
    _prune_old_jobs()
    f = request.files.get("file")
    if f is None or f.filename == "":
        return {"error": "No file uploaded."}, 400

    pdf_bytes = f.read()
    out_name = (f.filename.rsplit(".", 1)[0] if "." in f.filename else f.filename) + "-fixed.pdf"

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "status": "starting",
            "phase": None,
            "current": 0,
            "total": 0,
            "created_at": time.time(),
            "error": None,
            "bytes": None,
            "out_name": out_name,
            "splits_fixed": 0,
            "overflow_risk": False,
        }

    threading.Thread(target=_process_job, args=(job_id, pdf_bytes, out_name), daemon=True).start()
    return {"job_id": job_id}


@app.route("/progress/<job_id>")
def progress_endpoint(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return {"error": "Unknown job — it may have expired."}, 404
        return {
            "status": job["status"],
            "phase": job["phase"],
            "current": job["current"],
            "total": job["total"],
            "error": job["error"],
            "splits_fixed": job["splits_fixed"],
            "overflow_risk": job["overflow_risk"],
        }


@app.route("/download/<job_id>")
def download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None or job["status"] != "done":
            return {"error": "Not ready."}, 404
        data = job["bytes"]
        out_name = job["out_name"]
        del jobs[job_id]  # one-shot: free the memory the moment it's downloaded

    return send_file(
        io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=out_name
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True, threaded=True)
