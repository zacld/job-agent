"""
dashboard/app.py — Flask web dashboard for the job-agent tracker.

v2 — updated for Tier 1/2 pipeline features:
  - Salary range + market alignment parsing
  - Follow-up due detection (7+ days applied, no FOLLOWUP_MARKER)
  - Cover letter version counting (JSON list in col J)
  - LinkedIn source tracking
  - JD preview endpoint
  - Apply method breakdown stats
  - Per-job detail API route
"""

import json
import logging
import re
import subprocess
import sys
import pathlib
import threading
from datetime import date, timedelta
from collections import deque

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from flask import Flask, jsonify, render_template, request, redirect, url_for
import config
from agent.sheets import get_sheet, get_existing_urls, update_status, STATUS

# ---------------------------------------------------------------------------
# Agent subprocess state (module-level singleton)
# ---------------------------------------------------------------------------

_AGENT_ROOT = pathlib.Path(__file__).parent.parent
_MAX_LOG_LINES = 600

_run_lock  = threading.Lock()
_run_state: dict = {
    "process":   None,       # Popen object while running
    "running":   False,
    "exit_code": None,       # int after completion, None while running
    "log":       deque(maxlen=_MAX_LOG_LINES),  # rolling buffer
    "roles":     [],         # roles used in last/current run
    "dry_run":   False,
}


def _stream_output(proc: subprocess.Popen) -> None:
    """Background thread: pipe process stdout→log buffer, then mark done."""
    for raw_line in iter(proc.stdout.readline, ""):
        line = raw_line.rstrip()
        with _run_lock:
            _run_state["log"].append(line)
    proc.wait()
    with _run_lock:
        _run_state["running"]   = False
        _run_state["exit_code"] = proc.returncode
        _run_state["process"]   = None
        _run_state["log"].append(
            f"── Agent exited with code {proc.returncode} ──"
        )

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dashboard")

FOLLOWUP_MARKER    = "[FOLLOWUP_SENT]"
FOLLOWUP_THRESHOLD = 7  # days

# ---------------------------------------------------------------------------
# Sheet helpers
# ---------------------------------------------------------------------------

def _load_jobs() -> list[dict]:
    try:
        sheet = get_sheet()
        return sheet.get_all_records()
    except Exception as exc:
        logger.error("Sheet load failed: %s", exc)
        return []


def _mock_jobs() -> list[dict]:
    """Mock data for local testing when sheet is unavailable."""
    today = date.today().isoformat()
    eight_days_ago = (date.today() - timedelta(days=8)).isoformat()
    return [
        {
            "Date Found": today, "Company": "CyberShield Brokers", "Role Title": "Cyber Insurance Sales Executive",
            "Source URL": "https://reed.co.uk/jobs/cyber-insurance/1001",
            "Score": 9, "Score Reason": "Perfect fit — B2B closing background + cyber degree directly maps to cyber insurance sales. Client base (CISOs, risk managers) aligns with Zac's consultative sales style.",
            "Apply Method": "email", "Status": "🟡 Cover Letter Written", "Date Applied": "",
            "Cover Letter Path": json.dumps(["/output/cover_letters/CyberShield_Cyber_Insurance_2026-04-16_v1.txt", "/output/cover_letters/CyberShield_Cyber_Insurance_2026-04-16_v2.txt"]),
            "Contact Email": "careers@cybershield.co.uk", "Response": "", "Notes": "Salary: 55000–80000 (within_target)",
        },
        {
            "Date Found": today, "Company": "InsurTech Ventures", "Role Title": "BDM InsurTech",
            "Source URL": "https://linkedin.com/jobs/view/2002",
            "Score": 8, "Score Reason": "Strong match — digital transformation sales experience at Gro.Team maps directly. InsurTech SaaS selling to insurers.",
            "Apply Method": "portal", "Status": "🟠 Queued", "Date Applied": "",
            "Cover Letter Path": json.dumps(["/output/cover_letters/InsurTech_BDM_2026-04-16_v1.txt"]),
            "Contact Email": "", "Response": "", "Notes": "Salary: 60000–90000 (above_target) Screenshot: /output/screenshots/InsurTech_BDM_2026-04-16.png",
        },
        {
            "Date Found": eight_days_ago, "Company": "Dojo Payments", "Role Title": "Senior Fintech Sales Consultant",
            "Source URL": "https://adzuna.co.uk/jobs/3003",
            "Score": 7, "Score Reason": "Direct experience — currently at Dojo. Internal progression opportunity. High-volume outbound expertise matches.",
            "Apply Method": "email", "Status": "🟢 Applied", "Date Applied": eight_days_ago,
            "Cover Letter Path": json.dumps(["/output/cover_letters/Dojo_Senior_Fintech_2026-04-08_v1.txt"]),
            "Contact Email": "internal.jobs@dojo.tech", "Response": "", "Notes": "Salary: 50000–70000 (within_target)",
        },
        {
            "Date Found": today, "Company": "Paladin Cyber", "Role Title": "Cybersecurity Sales Executive",
            "Source URL": "https://workable.com/jobs/4004",
            "Score": 9, "Score Reason": "Exceptional fit — cybersecurity dissertation + B2B SaaS sales = rare combination. Paladin sells to SMEs exactly like Zac's Gro.Team profile.",
            "Apply Method": "portal", "Status": "⭐ Interview", "Date Applied": today,
            "Cover Letter Path": json.dumps(["/output/cover_letters/Paladin_Cybersec_2026-04-16_v1.txt"]),
            "Contact Email": "talent@paladincyber.com", "Response": "Interview booked Fri 18 Apr", "Notes": "Salary: 65000–95000 (above_target)",
        },
        {
            "Date Found": today, "Company": "Acuity Insurance", "Role Title": "Portfolio Manager",
            "Source URL": "https://reed.co.uk/jobs/portfolio-manager/5005",
            "Score": 5, "Score Reason": "Low fit — role requires 5+ years actuarial experience. Zac's sales background doesn't map.",
            "Apply Method": "unknown", "Status": "⏭ Skipped", "Date Applied": "",
            "Cover Letter Path": "", "Contact Email": "", "Response": "", "Notes": "Salary: 40000–50000 (below_target)",
        },
        {
            "Date Found": today, "Company": "DataSec Partners", "Role Title": "Enterprise Account Executive",
            "Source URL": "https://linkedin.com/jobs/view/6006",
            "Score": 8, "Score Reason": "Strong enterprise SaaS fit. Multi-stakeholder sales cycle experience from Gro.Team is directly relevant.",
            "Apply Method": "email", "Status": "🔵 Scored", "Date Applied": "",
            "Cover Letter Path": "",
            "Contact Email": "hire@datasec.io", "Response": "", "Notes": "Salary: 60000–85000 (within_target)",
        },
        {
            "Date Found": eight_days_ago, "Company": "ThreatGuard Ltd", "Role Title": "IT Sales Consultant",
            "Source URL": "https://totaljobs.com/jobs/7007",
            "Score": 7, "Score Reason": "Good fit — IT security consulting sales, technical literacy matches Zac's automation and recon tools background.",
            "Apply Method": "email", "Status": "🟢 Applied", "Date Applied": eight_days_ago,
            "Cover Letter Path": json.dumps(["/output/cover_letters/ThreatGuard_IT_Sales_2026-04-08_v1.txt"]),
            "Contact Email": "jobs@threatguard.co.uk", "Response": "", "Notes": "Salary: 48000–65000 (within_target) [FOLLOWUP_SENT] Draft: Following up on IT Sales Consultant application — ThreatGuard",
        },
        {
            "Date Found": today, "Company": "FinSecure UK", "Role Title": "Cyber Sales Executive",
            "Source URL": "https://adzuna.co.uk/jobs/8008",
            "Score": 6, "Score Reason": "Moderate fit — smaller company, lower OTE than target, but cyber insurance vertical is relevant.",
            "Apply Method": "portal", "Status": "❌ Rejected", "Date Applied": today,
            "Cover Letter Path": json.dumps(["/output/cover_letters/FinSecure_Cyber_2026-04-16_v1.txt"]),
            "Contact Email": "", "Response": "Position filled internally", "Notes": "Salary: 42000–55000 (below_target)",
        },
    ]


def _parse_salary_from_notes(notes: str) -> dict:
    """Extract salary_min, salary_max, market_alignment from Notes string."""
    result = {"salary_min": None, "salary_max": None, "market": "unknown"}
    if not notes:
        return result

    # Format written by score.py: "Salary: 55000–65000 (within_target)"
    m = re.search(r"Salary:\s*([\d]+)[–\-]([\d]+)\s*\((\w+)\)", notes)
    if m:
        result["salary_min"]  = int(m.group(1))
        result["salary_max"]  = int(m.group(2))
        result["market"]      = m.group(3)
    return result


def _parse_cl_versions(cover_letter_val: str) -> list[str]:
    """Parse cover letter path(s) — may be a JSON list or plain string."""
    if not cover_letter_val:
        return []
    try:
        versions = json.loads(cover_letter_val)
        if isinstance(versions, list):
            return versions
    except (json.JSONDecodeError, TypeError):
        pass
    return [cover_letter_val]


def _is_followup_due(job: dict) -> bool:
    """Return True if this Applied job is 7+ days old with no follow-up sent."""
    if STATUS["applied"] not in job.get("Status", ""):
        return False
    notes = job.get("Notes", "")
    if FOLLOWUP_MARKER in notes:
        return False
    date_str = job.get("Date Applied", "")
    if not date_str:
        return False
    try:
        applied_date = date.fromisoformat(str(date_str)[:10])
        return (date.today() - applied_date).days >= FOLLOWUP_THRESHOLD
    except ValueError:
        return False


def _guess_source(url: str) -> str:
    known = {
        "linkedin.com":  "LinkedIn",
        "reed.co.uk":    "Reed",
        "adzuna.co.uk":  "Adzuna",
        "workable.com":  "Workable",
        "totaljobs.com": "TotalJobs",
        "indeed.com":    "Indeed",
    }
    for domain, name in known.items():
        if domain in url:
            return name
    return "Other"


def _enrich_job(job: dict) -> dict:
    """Add computed display fields to a job dict."""
    notes = job.get("Notes", "")
    salary_data = _parse_salary_from_notes(notes)
    job["_salary_min"]  = salary_data["salary_min"]
    job["_salary_max"]  = salary_data["salary_max"]
    job["_market"]      = salary_data["market"]
    job["_cl_versions"] = _parse_cl_versions(job.get("Cover Letter Path", ""))
    job["_cl_count"]    = len(job["_cl_versions"])
    job["_followup_due"] = _is_followup_due(job)
    job["_source"]      = _guess_source(job.get("Source URL", ""))
    job["_meta"]        = _status_meta(job.get("Status", ""))

    # Parse follow-up draft from Notes if present
    fu_match = re.search(r"\[FOLLOWUP_SENT\] Draft: (.+?)(?:\n|$)", notes)
    job["_followup_draft_subject"] = fu_match.group(1).strip() if fu_match else ""

    return job


def _compute_stats(jobs: list[dict]) -> dict:
    total          = len(jobs)
    status_counts  : dict[str, int] = {}
    scores         : list[int]      = []
    by_source      : dict[str, int] = {}
    by_method      : dict[str, int] = {}
    by_market      : dict[str, int] = {"above_target": 0, "within_target": 0,
                                        "below_target": 0, "unknown": 0}
    followups_due  = 0
    emailed        = 0
    salary_ranges  : list[int]      = []

    for j in jobs:
        s = j.get("Status", "⚪ Found")
        status_counts[s] = status_counts.get(s, 0) + 1

        try:
            scores.append(int(j.get("Score", "")))
        except (ValueError, TypeError):
            pass

        src = j.get("_source") or _guess_source(j.get("Source URL", ""))
        by_source[src] = by_source.get(src, 0) + 1

        method = (j.get("Apply Method") or "unknown").strip()
        by_method[method] = by_method.get(method, 0) + 1

        market = j.get("_market") or _parse_salary_from_notes(j.get("Notes",""))["market"]
        if market in by_market:
            by_market[market] += 1
        else:
            by_market["unknown"] += 1

        if j.get("_followup_due") if "_followup_due" in j else _is_followup_due(j):
            followups_due += 1

        notes = j.get("Notes", "")
        if "Direct email to" in notes:
            emailed += 1

        sal_max = j.get("_salary_max") or _parse_salary_from_notes(j.get("Notes",""))["salary_max"]
        if sal_max:
            salary_ranges.append(sal_max)

    avg_score   = round(sum(scores) / len(scores), 1) if scores else 0
    avg_salary  = int(sum(salary_ranges) / len(salary_ranges)) if salary_ranges else 0

    return {
        "total":          total,
        "applied":        status_counts.get(STATUS["applied"],   0),
        "interview":      status_counts.get(STATUS["interview"], 0),
        "rejected":       status_counts.get(STATUS["rejected"],  0),
        "queued":         status_counts.get(STATUS["queued"],    0),
        "cover":          status_counts.get(STATUS["cover"],     0),
        "scored":         status_counts.get(STATUS["scored"],    0),
        "skipped":        status_counts.get(STATUS["skipped"],   0),
        "response":       status_counts.get(STATUS["response"],  0),
        "found":          status_counts.get(STATUS["found"],     0),
        "avg_score":      avg_score,
        "avg_salary":     avg_salary,
        "followups_due":  followups_due,
        "emailed":        emailed,
        "status_counts":  status_counts,
        "by_source":      by_source,
        "by_method":      by_method,
        "by_market":      by_market,
        "score_dist":     _score_distribution(scores),
    }


def _score_distribution(scores: list[int]) -> dict[str, int]:
    dist = {str(i): 0 for i in range(1, 11)}
    for s in scores:
        dist[str(max(1, min(10, s)))] += 1
    return dist


def _status_meta(status: str) -> dict:
    mapping = {
        "⚪": {"color": "#9aa0a6", "bg": "#1e2030", "label": "Found"},
        "🔵": {"color": "#60a5fa", "bg": "#1e3a5f", "label": "Scored"},
        "🟡": {"color": "#fbbf24", "bg": "#3b2a00", "label": "Cover Letter"},
        "🟠": {"color": "#fb923c", "bg": "#3b1f00", "label": "Queued"},
        "🟢": {"color": "#4ade80", "bg": "#0d2e1a", "label": "Applied"},
        "📬": {"color": "#818cf8", "bg": "#1e1b4b", "label": "Response"},
        "❌": {"color": "#f87171", "bg": "#2d0f0f", "label": "Rejected"},
        "⭐": {"color": "#facc15", "bg": "#2d2200", "label": "Interview"},
        "⏭":  {"color": "#6b7280", "bg": "#1a1d27", "label": "Skipped"},
    }
    for emoji, meta in mapping.items():
        if emoji in status:
            return meta
    return {"color": "#e8eaf6", "bg": "#22263a", "label": status}


def _market_meta(market: str) -> dict:
    return {
        "above_target":  {"label": "Above target",  "color": "#4ade80", "bg": "#0d2e1a"},
        "within_target": {"label": "Within target",  "color": "#60a5fa", "bg": "#1e3a5f"},
        "below_target":  {"label": "Below target",   "color": "#f87171", "bg": "#2d0f0f"},
        "unknown":       {"label": "Salary unknown", "color": "#6b7280", "bg": "#1a1d27"},
    }.get(market, {"label": market, "color": "#6b7280", "bg": "#1a1d27"})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    use_mock = request.args.get("mock") == "1"
    jobs = _mock_jobs() if use_mock else _load_jobs()
    jobs = [_enrich_job(j) for j in jobs]
    stats = _compute_stats(jobs)

    followup_jobs = [j for j in jobs if j.get("_followup_due")]

    priority = {"⭐": 0, "📬": 1, "🟢": 2, "🟠": 3, "🟡": 4, "🔵": 5, "⚪": 6, "❌": 7, "⏭": 8}
    def sort_key(j):
        s = j.get("Status", "")
        for emoji, p in priority.items():
            if emoji in s:
                return (p, -(j.get("Score") or 0))
        return (99, 0)

    jobs_sorted = sorted(jobs, key=sort_key)

    return render_template(
        "index.html",
        jobs=jobs_sorted,
        stats=stats,
        followup_jobs=followup_jobs,
        all_statuses=list(STATUS.values()),
        status_meta=_status_meta,
        market_meta=_market_meta,
        sheet_name=config.SHEET_NAME,
        salary_min=config.SALARY_MIN,
        score_threshold=config.SCORE_THRESHOLD,
        target_roles=config.TARGET_ROLES,
    )


@app.route("/api/jobs")
def api_jobs():
    jobs = [_enrich_job(j) for j in _load_jobs()]
    return jsonify(jobs)


@app.route("/api/stats")
def api_stats():
    jobs = [_enrich_job(j) for j in _load_jobs()]
    return jsonify(_compute_stats(jobs))


@app.route("/api/job")
def api_job_detail():
    """Return full detail for one job by URL."""
    url = request.args.get("url", "")
    jobs = _load_jobs()
    for j in jobs:
        if j.get("Source URL") == url:
            return jsonify(_enrich_job(j))
    return jsonify({"error": "not found"}), 404


@app.route("/api/run", methods=["POST"])
def api_run():
    """Start the job-agent pipeline in a background subprocess."""
    with _run_lock:
        if _run_state["running"]:
            return jsonify({"error": "Agent already running"}), 409

    data     = request.get_json(silent=True) or {}
    roles    = [r.strip() for r in data.get("roles", []) if r.strip()]
    dry_run  = bool(data.get("dry_run", False))
    linkedin = bool(data.get("linkedin", False))

    cmd = [sys.executable, str(_AGENT_ROOT / "main.py")]
    if dry_run:
        cmd.append("--dry-run")
    if linkedin:
        cmd.append("--linkedin")
    if roles:
        cmd += ["--roles", ",".join(roles)]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(_AGENT_ROOT),
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    with _run_lock:
        _run_state["log"].clear()
        _run_state["running"]   = True
        _run_state["exit_code"] = None
        _run_state["process"]   = proc
        _run_state["roles"]     = roles or list(config.TARGET_ROLES)
        _run_state["dry_run"]   = dry_run

    thread = threading.Thread(target=_stream_output, args=(proc,), daemon=True)
    thread.start()

    logger.info("Agent subprocess started (PID %d) roles=%s dry=%s", proc.pid, roles, dry_run)
    return jsonify({"started": True, "pid": proc.pid})


@app.route("/api/run/status")
def api_run_status():
    """Return current run state + last N log lines."""
    with _run_lock:
        return jsonify({
            "running":   _run_state["running"],
            "exit_code": _run_state["exit_code"],
            "roles":     _run_state["roles"],
            "dry_run":   _run_state["dry_run"],
            "log":       "\n".join(_run_state["log"]),
        })


@app.route("/api/run/stop", methods=["POST"])
def api_run_stop():
    """Terminate the running agent subprocess."""
    with _run_lock:
        proc = _run_state.get("process")
        if proc and _run_state["running"]:
            proc.terminate()
            _run_state["running"] = False
            _run_state["log"].append("── Run stopped by user ──")
            return jsonify({"stopped": True})
    return jsonify({"stopped": False, "reason": "not running"})


@app.route("/update_status", methods=["POST"])
def update_status_route():
    url        = request.form.get("url")
    new_status = request.form.get("status")
    notes      = request.form.get("notes", "")
    if url and new_status:
        try:
            sheet = get_sheet()
            extras = {"notes": notes} if notes else {}
            update_status(sheet, url, new_status, extras=extras)
        except Exception as exc:
            logger.error("Status update failed: %s", exc)
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n  Job Agent Dashboard v2")
    print("  http://localhost:5050\n")
    app.run(debug=True, host="0.0.0.0", port=5050)
