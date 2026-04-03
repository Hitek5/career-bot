import os
import json
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "db" / "career_bot.db"
OUTPUT_DIR = BASE_DIR / "output"
LOGS_DIR = BASE_DIR / "logs"
TEMPLATES_DIR = BASE_DIR / "templates"
ALLOWED_USERS_FILE = BASE_DIR / "allowed_users.json"

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"

ADMIN_TG_ID = 292168972

# Trial limits for new users
TRIAL_ANALYSES = 3
TRIAL_RESUMES = 2

# Paymaster: BotFather provider_token (for native Telegram Payments)
PAYMASTER_TOKEN = os.getenv("PAYMASTER_TOKEN", "")
# Paymaster REST API (for payment link generation)
PAYMASTER_API_TOKEN = os.getenv("PAYMASTER_API_TOKEN", "")
PAYMASTER_MERCHANT_ID = os.getenv("PAYMASTER_MERCHANT_ID", "")

# Payment packages: {id: (analyses, resumes, stars_price, rub_price_kopecks, label)}
PACKAGES = {
    "pack_5":  {"analyses": 5,   "resumes": 5,   "stars": 25,  "rub": 19900,  "label": "Старт"},
    "pack_20": {"analyses": 20,  "resumes": 15,  "stars": 60,  "rub": 49900,  "label": "Стандарт"},
    "pack_50": {"analyses": 50,  "resumes": 30,  "stars": 120, "rub": 99900,  "label": "Про"},
    "pack_100":{"analyses": 100, "resumes": 100, "stars": 200, "rub": 199900, "label": "Макс"},
}


def load_allowed_users() -> list[dict]:
    if ALLOWED_USERS_FILE.exists():
        with open(ALLOWED_USERS_FILE) as f:
            return json.load(f)
    return []


def get_user_role(tg_id: int) -> str:
    """Return role from allowed_users.json, or 'trial' for unknown users."""
    for u in load_allowed_users():
        if u["tg_id"] == tg_id:
            return u.get("role", "paid")
    return "trial"


def is_unlimited(role: str) -> bool:
    return role in ("admin", "paid")
