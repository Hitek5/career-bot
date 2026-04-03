import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import DB_PATH
from db.models import Base

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
SessionLocal = sessionmaker(bind=engine)


def _migrate():
    """Add new columns to existing DB if missing."""
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cols = {row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()}
    if "role" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'admin'")
    if "analyses_left" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN analyses_left INTEGER DEFAULT -1")
    if "resumes_left" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN resumes_left INTEGER DEFAULT -1")
    conn.commit()
    conn.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        _migrate()
    Base.metadata.create_all(engine)


def get_session():
    return SessionLocal()
