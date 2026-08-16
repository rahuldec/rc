# Deploying so others can use it

This is a small Flask app (`app.py`) that runs the same fix logic already
validated locally. Files are processed in memory and never written to disk —
nothing is stored, logged, or retained after the response is sent.

I can't create hosting accounts or enter payment details on your behalf, so
you'll need to do the actual sign-up/deploy click yourself. Everything else
(the code, config files, and these steps) is ready to go.

## Recommended: Render.com (free tier available, no CLI needed)

1. **Push this folder to a GitHub repo** (private is fine — the code has no
   secrets in it; `APP_PASSWORD` is set as an environment variable, not
   committed).

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
   - **Start command:** `gunicorn app:app --workers 2 --timeout 120`
   - **Environment:** Python 3

4. **Set an environment variable before deploying** — this is the important
   part, since these are student report cards:
   - Key: `APP_PASSWORD`
   - Value: a password you choose and share only with whoever needs the tool

   Without this set, the page is open to anyone with the link.

5. Click **Create Web Service**. Render builds and deploys; you'll get a URL
   like `https://your-app-name.onrender.com`.

6. Share the URL **and the password** with whoever needs it. They'll get an
   "Authentication required" browser prompt asking for a username (anything)
   and the password.

### Notes on the free tier

- The free tier **sleeps after 15 minutes of inactivity** and takes ~30–50s
  to wake up on the next request — fine for occasional use, annoying if
  someone's waiting on it. Upgrade to a paid instance ($7/mo Starter tier as
  of writing) for always-on.
- 50 MB max upload is set in `app.py` (`MAX_CONTENT_LENGTH`) — raise it there
  if you have larger PDFs.

## Alternative hosts

Railway, Fly.io, and PythonAnywhere all work the same way (push code, they
build from `requirements.txt` + `Procfile`, run `gunicorn app:app`). Render
is the simplest to set up with no CLI, which is why it's the default
recommendation above.

## Testing changes locally before you redeploy

```bash
cd pdf-edit-agent
source venv/bin/activate
python app.py
# open http://127.0.0.1:5050
```

Set `APP_PASSWORD=something` before running locally to test the login gate
the same way it'll behave in production.
