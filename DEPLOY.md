# Deploying so others can use it

This is a small Flask app (`app.py`) that runs the same fix logic already
validated locally. Files are processed in memory and never written to disk —
nothing is stored, logged, or retained after you download the result.

Processing runs in a background thread per upload so the page can poll for
progress. That job state lives in the process's memory, which is why the
`Procfile` runs a **single worker** (`--workers 1`) — a second worker process
wouldn't be able to see jobs started on the first one, and polling would 404
at random. `--threads 4` keeps that one process able to handle the upload
request and the progress-polling requests at the same time.

I can't create hosting accounts or enter payment details on your behalf, so
you'll need to do the actual sign-up/deploy click yourself. Everything else
(the code, config files, and these steps) is ready to go.

## Recommended: Render.com (free tier available, no CLI needed)

1. **Push this folder to a GitHub repo.**

   ```bash
   cd pdf-edit-agent
   git init
   git add .
   git commit -m "Fix split tables in PDF — web app"
   # create a new repo on github.com, then:
   git remote add origin <your-repo-url>
   git push -u origin main
   ```

2. Go to [render.com](https://render.com) → sign up / log in → **New +** →
   **Web Service** → connect your GitHub account → pick this repo.

3. Render should auto-detect the `Procfile`. If it asks:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app --workers 1 --threads 4 --timeout 300`
   - **Environment:** Python 3

4. Click **Create Web Service**. Render builds and deploys; you'll get a URL
   like `https://your-app-name.onrender.com`.

This version has no login gate — anyone with the link can use it. That's
fine for solo/private use; if you ever want to share the link more broadly,
say so and I'll add a password back in (it's a small, optional change).

### Notes on the free tier

- The free tier **sleeps after 15 minutes of inactivity** and takes ~30–50s
  to wake up on the next request — fine for occasional use, annoying if
  someone's waiting on it. Upgrade to a paid instance ($7/mo Starter tier as
  of writing) for always-on.
- 50 MB max upload is set in `app.py` (`MAX_CONTENT_LENGTH`) — raise it there
  if you have larger PDFs.
- A restart (redeploy, or the free tier sleeping) drops any in-progress or
  finished-but-not-yet-downloaded jobs, since they only live in memory. Not a
  concern in normal use — just don't expect a job to survive a redeploy.

## Alternative hosts

Railway, Fly.io, and PythonAnywhere all work the same way (push code, they
build from `requirements.txt` + `Procfile`). Make sure whatever start command
you configure keeps `--workers 1` for the reason above. Render is the
simplest to set up with no CLI, which is why it's the default recommendation.

## Testing changes locally before you redeploy

```bash
cd pdf-edit-agent
source venv/bin/activate
python app.py
# open http://127.0.0.1:5050
```
