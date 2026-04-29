# Deploy plan — submission URL for the MS Form

The submission needs **a single URL**. Two layers, in priority order:

1. **GitHub repo URL** (required, ~10 min) — works as the submission
   even if nothing else does. The repo's `README.md` tells the
   judge-evaluation story.
2. **Streamlit Community Cloud live demo** (optional, ~15 min) — links
   from the README. Best-effort; if it breaks during judging, the repo
   still stands.

A pre-recorded screen capture (`docs/internal/Hackathon_pre_demo.mkv`)
is the third backstop — embed in the README via GitHub's video upload.

---

## Pre-flight (do this before pushing anything)

```bash
cd /home/charles/agency_hack_2026

# 1. Confirm secrets are NOT staged
git status | grep -E "\.env( |$)"             # should print nothing
grep -l "sk-ant\|JvqVh0" -r . --exclude-dir=.venv --exclude-dir=.git \
    --exclude-dir=agency-26-hackathon         # should print only .env

# 2. Confirm .gitignore catches the right things
git check-ignore -v .env data/agent_state.db agency-26-hackathon/ \
    docs/internal/Hackathon_pre_demo.mkv .planning/

# 3. Confirm the app imports cleanly
PYTHONPATH=. .venv/bin/python -c "import app.main; print('ok')"

# 4. Run the smoke test one more time
PYTHONPATH=. .venv/bin/pytest tests/ -x --timeout=60
```

If any of these fail, **stop** and fix before pushing.

---

## Step 1 — Push to GitHub (required)

### 1a. Create the repo (GitHub CLI, fastest)

```bash
# Authenticate once if not already
gh auth status || gh auth login

# Create a public repo named after the challenge
gh repo create agency-2026-challenge6 \
    --public \
    --description "Agency 2026 Challenge #6: Related-party governance networks" \
    --source=. \
    --remote=origin \
    --push=false
```

### 1b. Stage and commit

```bash
git add -A
git status                                    # eyeball — no .env, no MKV at root
git commit -m "Hackathon submission: related-party governance networks

- Three detection patterns: round-trip rings, shared-director networks,
  contractor / charity-director crossover
- Streamlit UI over CRA T3010 + federal G&C + Alberta open data
- Decision-support framing (DADM-aligned)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
git push -u origin master
```

### 1c. Verify the public repo

```bash
gh repo view --web                            # opens browser
```

Check:
- README renders.
- `.env` is **not** in the file list.
- `data/agent_state.db` is **not** in the file list.
- The screenshot in the README loads.

**This URL is your submission fallback.** Copy it now:
`https://github.com/<your-handle>/agency-2026-challenge6`

---

## Step 2 — Embed the demo video in the README (3 min)

GitHub accepts `.mp4`/`.mov` drag-and-drop in issues/PRs/releases, then
gives back a permanent CDN URL. `.mkv` is **not** supported — convert
first if you want this.

```bash
# Convert MKV to MP4 (fast, no re-encode if codecs match)
ffmpeg -i docs/internal/Hackathon_pre_demo.mkv \
    -c:v copy -c:a aac docs/internal/Hackathon_pre_demo.mp4
```

Then in the browser:
1. Open the new repo, click **Issues → New issue**.
2. Drag `Hackathon_pre_demo.mp4` into the comment box. Wait for upload.
3. Copy the `https://github.com/.../user-assets/.../...mp4` URL it
   generates.
4. Paste into `README.md` under a `## Demo video` section as
   `<video src="...">` or just the bare URL.
5. **Cancel** the issue (don't submit it).
6. Commit and push the README update.

---

## Step 3 — Streamlit Community Cloud (optional, ~15 min)

Skip if T-30 min or less.

### 3a. Prep

```bash
# Streamlit Cloud reads requirements.txt OR pyproject.toml. We have
# pyproject.toml — confirm it lists every runtime dep.
grep -A1 "^dependencies" pyproject.toml | head -25

# Add a streamlit launch entrypoint if missing
cat > .streamlit/config.toml <<'EOF'
[server]
headless = true
runOnSave = false
fileWatcherType = "none"
[browser]
gatherUsageStats = false
EOF
```

Confirm `.streamlit/credentials.toml` is **gitignored** (Codex Task 1
handles this) — if not, do not commit it.

### 3b. Deploy

1. Go to https://share.streamlit.io and sign in with the same GitHub
   account that owns the repo.
2. Click **New app → From existing repo**.
3. Repo: `<your-handle>/agency-2026-challenge6`. Branch: `master`.
   Main file path: `app/main.py`.
4. Click **Advanced settings → Secrets** and paste:
   ```toml
   DATABASE_URL = "postgresql://...the-event-day-string..."
   ANTHROPIC_API_KEY = "sk-ant-..."
   ```
   (These come from your `.env`. Do not type them anywhere else.)
5. Python version: 3.11 (Streamlit Cloud doesn't yet ship 3.13 — your
   `pyproject.toml` declares `>=3.11`, so this is fine).
6. Click **Deploy**. First build takes 5–8 min.

### 3c. Verify

- App URL will be like `https://agency-2026-challenge6-<hash>.streamlit.app`.
- Open it. Confirm the title renders, three tabs appear, at least one
  ring loads. If the live DB blocks Streamlit Cloud's egress IP, the
  page will show the demo-data fallback warning — that's still a valid
  deliverable, but ideally we get live.
- Add the live URL to the README's top section:
  ```markdown
  **Live demo:** https://...streamlit.app
  ```
  Commit and push.

### 3d. Failure modes

| Symptom | Likely cause | Fallback |
|---|---|---|
| Build fails on `psycopg[binary]` | Streamlit Cloud Python version | Pin `psycopg[binary]>=3.2,<4` and bump to 3.11 |
| App loads but Postgres connection times out | Render DB egress allowlist | Submit GitHub URL only; mention in README |
| `ModuleNotFoundError: src` | Missing `PYTHONPATH=.` | Add a `streamlit_app.py` at repo root that `sys.path.append(".")`s and imports `app.main` |
| Anthropic 401 | Secret not pasted | Re-check secrets pane |

---

## Step 4 — Submit

The MS Form expects **one URL**. Choose in this order:

1. **Streamlit Cloud URL** (if it loads live data and looks like the
   local demo).
2. **GitHub repo URL** (always works; README has video + screenshots).

Paste into the form. Done.

---

## Post-submission cleanup (do not skip)

The `.env` contains a real `ANTHROPIC_API_KEY` and the organizers'
`DATABASE_URL`. After submission:

```bash
# 1. Confirm .env was never pushed
gh api repos/<your-handle>/agency-2026-challenge6/contents/.env \
    2>&1 | grep -q "Not Found" && echo "OK: .env not on GitHub"

# 2. If the Anthropic key was ever exposed in a commit, rotate it
#    immediately at https://console.anthropic.com/settings/keys

# 3. If the DATABASE_URL was exposed, notify the organizers — they own
#    the credential rotation
```

---

## Rollback / "oh no" moves

- **Pushed `.env` by accident:**
  ```bash
  # Rotate keys first. Then:
  git rm --cached .env
  git commit -m "Remove .env"
  git push
  # The secret is still in git history. Force-push only if the org
  # confirms it's OK; otherwise rotate and move on.
  ```
- **Streamlit Cloud build won't go green and judging starts in 10 min:**
  Delete the Streamlit Cloud app, remove the live-demo line from the
  README, push, submit GitHub URL. The video in the README is enough.
