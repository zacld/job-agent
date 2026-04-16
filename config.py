import os
from dotenv import load_dotenv

load_dotenv()

# --- Job Search Targets ---
TARGET_ROLES = [
    "Cyber Insurance Sales Executive",
    "BDM InsurTech",
    "Cyber Sales Executive",
    "B2B SaaS Account Executive",
    "Enterprise Account Executive",
    "IT Sales Consultant",
    "Digital Transformation Sales",
    "Cybersecurity Sales",
    "Tech Sales Executive",
    "Portfolio Manager",
]

TARGET_LOCATIONS = ["London", "Remote", "Hybrid UK"]

SALARY_MIN = 45000

# --- Scoring ---
SCORE_THRESHOLD = 7

# --- Cover Letter Tone ---
TONE = (
    "Direct, confident, commercially focused. Not corporate filler. "
    "First person. Reference specific details from the job description."
)

# --- Auto-skip keywords (case-insensitive match in snippet/title) ---
NO_APPLY = ["pure inbound", "no commission", "graduate scheme"]

# --- Contact / Notifications ---
MY_EMAIL = os.getenv("MY_EMAIL", "")

# --- Google Sheets ---
SHEET_NAME = "Job Agent Tracker"

# --- Search limits ---
MAX_RESULTS_PER_QUERY = 10

# --- Daily digest hour (UTC) ---
DAILY_DIGEST_HOUR = 8

# --- Follow-up settings ---
FOLLOWUP_AFTER_DAYS = 7        # Days after applying before follow-up is drafted
AUTO_SEND_FOLLOWUPS = False    # Set True to auto-send follow-ups (or override via env)

# --- LinkedIn scraping ---
LINKEDIN_EMAIL    = os.getenv("LINKEDIN_EMAIL", "")
LINKEDIN_PASSWORD = os.getenv("LINKEDIN_PASSWORD", "")
USE_LINKEDIN      = os.getenv("USE_LINKEDIN", "false").lower() == "true"

# --- API / Auth (loaded from env) ---
ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY", "")
GOOGLE_API_KEY          = os.getenv("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID           = os.getenv("GOOGLE_CSE_ID", "")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")  # base64-encoded

GMAIL_USER         = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

# --- Claude model ---
CLAUDE_MODEL = "claude-opus-4-5"

# --- Playwright ---
HEADLESS = os.getenv("CI", "false").lower() == "true"
