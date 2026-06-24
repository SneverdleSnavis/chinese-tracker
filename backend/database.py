import sqlite3
import shutil
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent / "data" / "app.db"
BACKUP_DIR = Path(__file__).parent / "data" / "backups"
MAX_BACKUPS = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS words (
    word TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'unknown',  -- unknown | learning | known
    pinyin TEXT,
    definition TEXT,
    seen_count INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT,
    last_seen TEXT,
    status_updated TEXT
);

CREATE TABLE IF NOT EXISTS texts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source TEXT,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_read_at TEXT
);

CREATE TABLE IF NOT EXISTS reading_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text_id INTEGER NOT NULL REFERENCES texts(id),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    words_looked_up INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lookups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    word TEXT NOT NULL,
    text_id INTEGER REFERENCES texts(id),
    looked_up_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subtitle_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text_id INTEGER NOT NULL REFERENCES texts(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    start_ms INTEGER,
    end_ms INTEGER,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subtitle_lines_text ON subtitle_lines(text_id, idx);

CREATE TABLE IF NOT EXISTS custom_definitions (
    word TEXT PRIMARY KEY,
    pinyin TEXT,
    definition TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segmentation_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,          -- 'merge' (force word to stay together) | 'split' (force word apart)
    word TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(kind, word)
);
"""

# Columns added after the original schema shipped; applied idempotently in init_db.
MIGRATIONS = [
    ("texts", "kind", "ALTER TABLE texts ADD COLUMN kind TEXT NOT NULL DEFAULT 'article'"),
    ("texts", "video_url", "ALTER TABLE texts ADD COLUMN video_url TEXT"),
    ("texts", "completion", "ALTER TABLE texts ADD COLUMN completion TEXT NOT NULL DEFAULT 'unread'"),
]


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _column_exists(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        for table, column, ddl in MIGRATIONS:
            if not _column_exists(conn, table, column):
                conn.execute(ddl)


def backup_db():
    """Take a timestamped copy of the database if it has any user data.
    Uses SQLite's online backup API so it's safe even if the DB is open.
    Skips backup when empty (e.g. first run) and prunes old backups."""
    if not DB_PATH.exists():
        return None
    src = sqlite3.connect(DB_PATH)
    try:
        has_data = src.execute(
            "SELECT (SELECT COUNT(*) FROM texts) + (SELECT COUNT(*) FROM words)"
        ).fetchone()[0]
        if not has_data:
            return None
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_path = BACKUP_DIR / f"app_{stamp}.db"
        dest = sqlite3.connect(dest_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()

    backups = sorted(BACKUP_DIR.glob("app_*.db"))
    for old in backups[:-MAX_BACKUPS]:
        old.unlink()
    return dest_path
