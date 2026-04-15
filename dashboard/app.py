"""
dashboard/app.py — Flask web dashboard for the job-agent tracker.

Reads live from the Google Sheet and renders a visual dashboard.
Optionally allows status updates directly from the UI.

Run:
    python dashboard/app.py
    open http://localhost:5050
"""

import json
import logging
import sys
import pathlib

# Allow imports from repo root
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from flask import Flask, jsonify, render_template, request, redirect, url_for
import config
from agent.sheets import get_sheet, get_existing_urls, update_status, STATUS

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dashboard")

# ---------------------------------------------------------------------------
# Sheet helpers
# ---------------------------------------------------------------------------

def _load_jobs() -> list[dict]:
    """Pull all rows from the sheet as a list of dicts."""
    try:
        sheet = get_sheet()
        rows = sheet.get_all_records()
        return rows
    except Exception as exc:
        logger.error("Failed to load sheet: %s", exc)
        return []


def _compute_stats(jobs: list[dict]) -> dict:
    total = len(jobs)
    status_counts: dict[str, int] = {}
    scores = []
    by_source: dict[str, int] = {}
    by_method: dict[str, int] = {}

    for j in jobs:
        s = j.get("Status", "⚪ Found")
        status_counts[s] = status_counts.get(s, 0) + 1

        score = j.get("Score", "")
        try:
            scores.append(int(score))
        except (ValueError, TypeError):
            pass

        source = _guess_source(j.get("Source URL", ""))
        by_source[source] = by_source.get(source, 0) + 1

        method = j.get("Apply Method", "unknown") or "unknown"
        by_method[method] = by_method.get(method, 0) + 1

    avg_score = round(sum(scores) / len(scores), 1) if scores else 0

    # Map to friendly labels
    applied   = status_counts.get(STATUS["applied"], 0)
    interview = status_counts.get(STATUS["interview"], 0)
    rejected  = status_counts.get(STATUS["rejected"], 0)
    queued    = status_counts.get(STATUS["queued"], 0)
    cover     = status_counts.get(STATUS["cover"], 0)
    scored    = status_counts.get(STATUS["scored"], 0)
    skipped   = status_counts.get(STATUS["skipped"], 0)
    response  = status_counts.get(STATUS["response"], 0)
    found     = status_counts.get(STATUS["found"], 0)

    return {
        "total": total,
        "applied": applied,
        "interview": interview,
        "rejected": rejected,
        "queued": queued,
        "cover": cover,
        "scored": scored,
        "skipped": skipped,
        "response": response,
        "found": found,
        "avg_score": avg_score,
        "status_counts": status_counts,
        "by_source": by_source,
        "by_method": by_method,
        "score_dist": _score_distribution(scores),
    }


def _score_distribution(scores: list[int]) -> dict[str, int]:
    dist = {str(i): 0 for i in range(1, 11)}
    for s in scores:
        k = str(max(1, min(10, s)))
        dist[k] += 1
    return dist


def _guess_source(url: str) -> str:
    known = {
        "reed.co.uk": "Reed",
        "adzuna.co.uk": "Adzuna",
        "workable.com": "Workable",
        "linkedin.com": "LinkedIn",
        "totaljobs.com": "TotalJobs",
        "indeed.com": "Indeed",
    }
    for domain, name in known.items():
        if domain in url:
            return name
    return "Other"


def _status_meta(status: str) -> dict:
    """Return colour + short label for a status string."""
    mapping = {
        "⚪": {"color": "#9aa0a6", "bg": "#f1f3f4", "label": "Found"},
        "🔵": {"color": "#1a73e8", "bg": "#e8f0fe", "label": "Scored"},
        "🟡": {"color": "#b06000", "bg": "#fef9e7", "label": "Cover Letter"},
        "🟠": {"color": "#e37400", "bg": "#fef3e2", "label": "Queued"},
        "🟢": {"color": "#188038", "bg": "#e6f4ea", "label": "Applied"},
        "📬": {"color": "#1967d2", "bg": "#d2e3fc", "label": "Response"},
        "❌": {"color": "#c5221f", "bg": "#fce8e6", "label": "Rejected"},
        "⭐": {"color": "#b06000", "bg": "#fef9e7", "label": "Interview"},
        "⏭":  {"color": "#80868b", "bg": "#f1f3f4", "label": "Skipped"},
    }
    for emoji, meta in mapping.items():
        if emoji in status:
            return meta
    return {"color": "#333", "bg": "#f1f3f4", "label": status}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    jobs = _load_jobs()
    stats = _compute_stats(jobs)

    # Attach display metadata to each job
    for j in jobs:
        j["_meta"] = _status_meta(j.get("Status", ""))

    # Sort: interviews first, then applied, then queued, then cover, etc.
    priority = {
        "⭐": 0, "📬": 1, "🟢": 2, "🟠": 3, "🟡": 4,
        "🔵": 5, "⚪": 6, "❌": 7, "⏭": 8,
    }
    def sort_key(j):
        s = j.get("Status", "")
        for emoji, p in priority.items():
            if emoji in s:
                return (p, -(j.get("Score") or 0))
        return (99, 0)

    jobs_sorted = sorted(jobs, key=sort_key)

    all_statuses = list(STATUS.values())
    return render_template(
        "index.html",
        jobs=jobs_sorted,
        stats=stats,
        all_statuses=all_statuses,
        status_meta=_status_meta,
        sheet_name=config.SHEET_NAME,
    )


@app.route("/api/jobs")
def api_jobs():
    """JSON endpoint for dynamic filtering."""
    jobs = _load_jobs()
    for j in jobs:
        j["_meta"] = _status_meta(j.get("Status", ""))
    return jsonify(jobs)


@app.route("/api/stats")
def api_stats():
    jobs = _load_jobs()
    return jsonify(_compute_stats(jobs))


@app.route("/update_status", methods=["POST"])
def update_status_route():
    """Update the status of a job from the dashboard."""
    url = request.form.get("url")
    new_status = request.form.get("status")
    if url and new_status:
        try:
            sheet = get_sheet()
            update_status(sheet, url, new_status)
        except Exception as exc:
            logger.error("Status update failed: %s", exc)
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n  Job Agent Dashboard")
    print("  Running at: http://localhost:5050\n")
    app.run(debug=True, host="0.0.0.0", port=5050)
