# job-agent

Autonomous Python agent that searches for jobs, scores them against your CV using Claude, writes tailored cover letters, fills out application forms with Playwright + Claude Vision, logs everything to Google Sheets, and emails you a daily digest — all on a daily schedule via GitHub Actions.

---

## What it does

| Step | Module | Description |
|------|--------|-------------|
| 1 | `search.py` | Queries Google Custom Search across Reed, Adzuna, Workable, and open web for your target roles |
| 2 | `score.py` | Sends each listing + your CV to Claude — returns a 1-10 fit score with reasoning |
| 3 | `cover_letter.py` | Writes a 3-paragraph tailored cover letter via Claude for every job above your threshold |
| 4 | `apply.py` | Opens the job URL in Playwright, screenshots the form, asks Claude Vision to identify fields, fills them out |
| 5 | `sheets.py` | Logs everything to a Google Sheet with colour-coded status emojis |
| 6 | `notify.py` | Sends you an HTML daily digest email with attached cover letters and screenshots |
| 7 | GitHub Actions | Runs the whole pipeline at 8 AM UTC Monday–Friday automatically |

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/job-agent.git
cd job-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Copy `.env.example` → `.env` and fill in all values

```bash
cp .env.example .env
```

### 3. Google Cloud setup

**Enable APIs** (in [Google Cloud Console](https://console.cloud.google.com)):
- Google Sheets API
- Google Drive API
- Custom Search API

**Service Account** (for Sheets access):
1. Create a service account with Editor role
2. Download the JSON key
3. Base64-encode it: `base64 -i service-account.json | tr -d '\n'`
4. Paste the result into `GOOGLE_CREDENTIALS_JSON` in your `.env`
5. Share your Google Sheet with the service account email

**Custom Search Engine** (for Google job search):
1. Go to [Programmable Search Engine](https://programmablesearchengine.google.com)
2. Create a new search engine — set "Search the entire web"
3. Copy your **Search engine ID** → `GOOGLE_CSE_ID`
4. Enable the **Custom Search API** → copy your **API key** → `GOOGLE_API_KEY`

### 4. Gmail App Password (for digest emails)

1. Enable 2FA on your Gmail account
2. Go to Google Account → Security → App Passwords
3. Generate a password for "Mail"
4. Set `GMAIL_USER` = your Gmail address, `GMAIL_APP_PASSWORD` = the generated password

### 5. Anthropic API key

Get your key from [console.anthropic.com](https://console.anthropic.com) and set `ANTHROPIC_API_KEY`.

---

## Fill in your CV

Edit `data/cv.json` with your real details. The more detail you add, the better Claude can tailor cover letters and score job fit.

Key fields to populate:
- `summary` — your 2-3 sentence professional summary
- `experience` — each role with `bullets` (achievement-focused, quantified where possible)
- `skills` — technical and commercial skills
- `target_roles` and `target_sectors`
- `salary_expectation`

---

## Running locally

**Dry run** (no form fills, no emails — safe for testing):
```bash
python main.py --dry-run
```

**Full run** (fills forms, sends digest email):
```bash
python main.py
```

**Test individual modules:**
```bash
python -m agent.search
python -m agent.score
python -m agent.cover_letter
python -m agent.notify
```

---

## GitHub Actions automation

The workflow runs automatically at **8 AM UTC Monday–Friday**.

To set up:
1. Push this repo to GitHub
2. Go to **Settings → Secrets and variables → Actions**
3. Add each secret from `.env.example`:
   - `ANTHROPIC_API_KEY`
   - `GOOGLE_API_KEY`
   - `GOOGLE_CSE_ID`
   - `GOOGLE_CREDENTIALS_JSON` ← the base64-encoded service account JSON
   - `GMAIL_USER`
   - `GMAIL_APP_PASSWORD`
   - `MY_EMAIL`

To trigger a manual run: **Actions → Run Job Agent → Run workflow**

Screenshots and cover letters are uploaded as workflow artifacts (retained 30 days).

---

## Google Sheet structure

Create a sheet called **"Job Agent Tracker"** and share it with your service account email. The agent will auto-create the headers on first run.

| Col | Field | Notes |
|-----|-------|-------|
| A | Date Found | ISO date |
| B | Company | |
| C | Role Title | |
| D | Source URL | Used for deduplication |
| E | Score | 1–10 from Claude |
| F | Score Reason | Claude's reasoning |
| G | Apply Method | email / portal / unknown |
| H | Status | See colour codes below |
| I | Date Applied | Set manually |
| J | Cover Letter Path | Local file path |
| K | Contact Email | Extracted by Claude |
| L | Response | Set manually |
| M | Notes | Screenshots etc. |

### Status colour codes (set manually in Sheets for visual clarity)

| Status | Meaning |
|--------|---------|
| ⚪ Found | Just discovered |
| 🔵 Scored | Scored by Claude, above threshold |
| 🟡 Cover Letter Written | Cover letter generated |
| 🟠 Queued | Form filled, awaiting your review & submit |
| 🟢 Applied | You have submitted the application |
| 📬 Response Received | Heard back |
| ❌ Rejected | No thanks |
| ⭐ Interview | Interview scheduled |
| ⏭ Skipped | Score below threshold or NO_APPLY match |

---

## Configuration

All settings are in `config.py`:

- `TARGET_ROLES` — job titles to search for
- `TARGET_LOCATIONS` — locations to include in searches
- `SCORE_THRESHOLD` — minimum Claude score to proceed (default: 7)
- `NO_APPLY` — keywords that auto-skip a listing
- `TONE` — writing style instructions for cover letters
- `MAX_RESULTS_PER_QUERY` — how many results per search query (max 10)

---

## Architecture notes

- Claude model: `claude-opus-4-5` throughout
- Playwright runs **headless=False** locally, **headless=True** in CI (auto-detected via `CI` env var)
- The form filler takes a final screenshot but **never auto-submits** — you review in the sheet and submit manually or via the 🟠 Queued queue
- All external API calls are wrapped in `try/except` — one failing job never crashes the run
- Deduplication is URL-based against column D of the sheet
