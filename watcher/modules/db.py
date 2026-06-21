import os
import sqlite3
import time
from typing import Dict, List, Optional


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str):
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = _connect(db_path)
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tracked_comics (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url         TEXT    UNIQUE NOT NULL,
                title       TEXT,
                cbz_path    TEXT,
                node_id     TEXT,
                page_count  INTEGER DEFAULT 0,
                tags_json   TEXT    DEFAULT '[]',
                last_checked REAL,
                first_seen  REAL    NOT NULL,
                is_blacklisted INTEGER DEFAULT 0,
                skip_reason TEXT
            )
        """)
        # Migration: add skip_reason to databases created before this column existed
        try:
            conn.execute("ALTER TABLE tracked_comics ADD COLUMN skip_reason TEXT")
        except Exception:
            pass  # column already exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tracked_artists (
                url          TEXT PRIMARY KEY,
                last_checked REAL
            )
        """)
    conn.close()


def upsert_comic(db_path: str, url: str, **fields):
    """Insert a new comic if it doesn't exist, then apply any extra field updates."""
    conn = _connect(db_path)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO tracked_comics (url, first_seen) VALUES (?, ?)",
            (url, time.time()),
        )
        if fields:
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            values = list(fields.values()) + [url]
            conn.execute(
                f"UPDATE tracked_comics SET {set_clause} WHERE url = ?", values
            )
    conn.close()


def get_comic(db_path: str, url: str) -> Optional[Dict]:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT * FROM tracked_comics WHERE url = ?", (url,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_comics(db_path: str) -> List[Dict]:
    conn = _connect(db_path)
    rows = conn.execute("SELECT * FROM tracked_comics").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_comic_checked(
    db_path: str,
    url: str,
    page_count: int,
    tags_json: str,
    cbz_path: str,
    title: str,
    node_id: str,
):
    conn = _connect(db_path)
    with conn:
        conn.execute(
            """
            UPDATE tracked_comics
               SET page_count   = ?,
                   tags_json    = ?,
                   cbz_path     = ?,
                   title        = ?,
                   node_id      = ?,
                   last_checked = ?
             WHERE url = ?
            """,
            (page_count, tags_json, cbz_path, title, node_id, time.time(), url),
        )
    conn.close()


def get_artist_last_checked(db_path: str, url: str) -> Optional[float]:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT last_checked FROM tracked_artists WHERE url = ?", (url,)
    ).fetchone()
    conn.close()
    return row["last_checked"] if row else None


def upsert_artist_checked(db_path: str, url: str):
    conn = _connect(db_path)
    with conn:
        conn.execute(
            """
            INSERT INTO tracked_artists (url, last_checked)
            VALUES (?, ?)
            ON CONFLICT(url) DO UPDATE SET last_checked = excluded.last_checked
            """,
            (url, time.time()),
        )
    conn.close()


def get_setting(db_path: str, key: str, default: Optional[str] = None) -> Optional[str]:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(db_path: str, key: str, value: str):
    conn = _connect(db_path)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.close()
