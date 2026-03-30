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


def load_allowed_users() -> list[dict]:
    if ALLOWED_USERS_FILE.exists():
        with open(ALLOWED_USERS_FILE) as f:
            return json.load(f)
    return []


def is_user_allowed(tg_id: int) -> bool:
    return any(u["tg_id"] == tg_id for u in load_allowed_users())


def get_user_role(tg_id: int) -> str | None:
    for u in load_allowed_users():
        if u["tg_id"] == tg_id:
            return u.get("role", "user")
    return None
