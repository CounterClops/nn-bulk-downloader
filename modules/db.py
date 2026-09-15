import os
import sqlite3
import time
from typing import Dict, List, Optional, Set

from modules.multporn import normalise_url


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
                cbz_hash    TEXT,
                node_id     TEXT,
                page_count  INTEGER DEFAULT 0,
                tags_json   TEXT    DEFAULT '[]',
                last_checked REAL,
                last_synced  REAL,
                first_seen  REAL    NOT NULL,
                is_blacklisted INTEGER DEFAULT 0,
                skip_reason TEXT,
                failed_page INTEGER DEFAULT NULL
            )
        """)
        # Migrations: add columns to databases created before they existed
        for col, definition in [
            ("skip_reason",  "TEXT"),
            ("cbz_hash",     "TEXT"),
            ("last_synced",  "REAL"),
            ("failed_page",  "INTEGER"),
        ]:
            try:
                conn.execute(f"ALTER TABLE tracked_comics ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
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
        _clear_legacy_censored_skips(conn)
        _normalise_stored_urls(conn)
    conn.close()


LEGACY_URL_NORMALISATION_KEY = "urls_normalised"

# Content fields, in the order a better-developed row should win them.
_MERGED_CONTENT_FIELDS = ("title", "cbz_path", "cbz_hash", "node_id", "tags_json", "failed_page")

# tags_json defaults to an empty list, so an untouched row carries no real tags.
_EMPTY_FIELD_VALUES = (None, "", "[]")


def _merge_duplicate_rows(rows: List[sqlite3.Row], canonical_url: str) -> Dict:
    """Combine rows that denote the same comic into one set of field values.

    The row with the most pages is the better-developed record and supplies the
    content fields; the others fill in whatever it lacks. Timestamps take the
    widest span so nothing looks newer or older than it was, and a skip on any
    row survives — erring toward not downloading, which the per-comic re-check
    lifts on its own if the skip no longer applies.
    """
    by_completeness = sorted(rows, key=lambda row: row["page_count"] or 0, reverse=True)
    # The already-canonical row breaks ties for the skip verdict.
    by_canonical_first = sorted(rows, key=lambda row: row["url"] != canonical_url)

    merged: Dict = {"url": canonical_url}
    for field in _MERGED_CONTENT_FIELDS:
        merged[field] = next(
            (row[field] for row in by_completeness if row[field] not in _EMPTY_FIELD_VALUES),
            None,
        )
    merged["tags_json"] = merged["tags_json"] or "[]"
    merged["page_count"] = max(row["page_count"] or 0 for row in rows)
    merged["first_seen"] = min(row["first_seen"] for row in rows if row["first_seen"])
    for field in ("last_checked", "last_synced"):
        stamps = [row[field] for row in rows if row[field]]
        merged[field] = max(stamps) if stamps else None
    merged["is_blacklisted"] = 1 if any(row["is_blacklisted"] for row in rows) else 0
    merged["skip_reason"] = next(
        (row["skip_reason"] for row in by_canonical_first if row["skip_reason"]), None
    )
    return merged


def _normalise_stored_urls(conn: sqlite3.Connection):
    """Collapse rows keyed by a non-canonical URL into their canonical row.

    Listings link the same comic under view parameters such as ``?r=1``, and
    URLs were previously stored as found, so one comic could occupy several
    rows — each tracked, filtered and downloaded independently. Input is now
    canonicalised, and this reconciles what earlier runs recorded.
    """
    already_normalised = conn.execute(
        "SELECT 1 FROM settings WHERE key = ?", (LEGACY_URL_NORMALISATION_KEY,)
    ).fetchone()
    if already_normalised:
        return

    rows_by_canonical: Dict[str, List[sqlite3.Row]] = {}
    for row in conn.execute("SELECT * FROM tracked_comics").fetchall():
        rows_by_canonical.setdefault(normalise_url(row["url"]), []).append(row)

    for canonical_url, rows in rows_by_canonical.items():
        if len(rows) == 1 and rows[0]["url"] == canonical_url:
            continue

        merged = _merge_duplicate_rows(rows, canonical_url)
        placeholders = ", ".join("?" for _ in rows)
        conn.execute(
            f"DELETE FROM tracked_comics WHERE url IN ({placeholders})",
            [row["url"] for row in rows],
        )
        columns = ", ".join(merged)
        conn.execute(
            f"INSERT INTO tracked_comics ({columns}) "
            f"VALUES ({', '.join('?' for _ in merged)})",
            list(merged.values()),
        )

    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (LEGACY_URL_NORMALISATION_KEY, str(time.time())),
    )


LEGACY_CENSORED_CLEANUP_KEY = "legacy_censored_skips_cleared"


def _clear_legacy_censored_skips(conn: sqlite3.Connection):
    """Release comics skipped by the retired tag-based "censored" filter.

    That filter matched a hardcoded tag list rather than the site's own blur
    marker, and nothing writes skip_reason 'censored' any more. The rows it left
    behind are decisions with no rule behind them, so they are lifted once and
    the run recorded, leaving any future use of the reason untouched.
    """
    already_cleared = conn.execute(
        "SELECT 1 FROM settings WHERE key = ?", (LEGACY_CENSORED_CLEANUP_KEY,)
    ).fetchone()
    if already_cleared:
        return

    conn.execute(
        "UPDATE tracked_comics SET is_blacklisted = 0, skip_reason = NULL "
        "WHERE skip_reason = 'censored'"
    )
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (LEGACY_CENSORED_CLEANUP_KEY, str(time.time())),
    )


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
    cbz_hash: Optional[str] = None,
    last_synced: Optional[float] = None,
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
                   last_checked = ?,
                   cbz_hash     = COALESCE(?, cbz_hash),
                   last_synced  = COALESCE(?, last_synced)
             WHERE url = ?
            """,
            (page_count, tags_json, cbz_path, title, node_id, time.time(), cbz_hash, last_synced, url),
        )
    conn.close()


def update_comic_hash(db_path: str, url: str, cbz_hash: str):
    """Store a newly computed hash for an existing comic entry."""
    conn = _connect(db_path)
    with conn:
        conn.execute(
            "UPDATE tracked_comics SET cbz_hash = ? WHERE url = ?",
            (cbz_hash, url),
        )
    conn.close()


def get_comics_missing_hash(db_path: str) -> List[Dict]:
    """Return comics that have a cbz_path but no cbz_hash yet (url and cbz_path only)."""
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT url, cbz_path FROM tracked_comics WHERE cbz_path IS NOT NULL AND cbz_hash IS NULL"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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


CENSORED_SKIP_REASON = "censored"

# SQLite caps the variables allowed in one statement; artist listings are far
# smaller than this, but chunking keeps the query safe for any listing size.
_SQL_VARIABLE_CHUNK = 500


def _chunked(items: List[str]):
    for start in range(0, len(items), _SQL_VARIABLE_CHUNK):
        yield items[start:start + _SQL_VARIABLE_CHUNK]


def sync_censored_skips(db_path: str, listed_urls: List[str], censored_urls: Set[str]):
    """Reconcile the censored skip against an artist listing just fetched.

    The site's blur marker lives only on listing pages, so this is the one place
    the verdict can be observed and the only place it may be changed. Applying
    it on every refresh is what keeps it current: a comic the site has newly
    blurred starts being skipped, and one it no longer blurs is released.

    The censored reason takes precedence over a tag or language skip, since
    those can be re-derived from the comic's own page later while this cannot.
    Releasing a comic clears only the censored reason, leaving other skips
    intact for the normal per-comic re-check to reconsider.
    """
    to_censor = [url for url in listed_urls if url in censored_urls]
    to_release = [url for url in listed_urls if url not in censored_urls]

    conn = _connect(db_path)
    with conn:
        for chunk in _chunked(to_censor):
            placeholders = ", ".join("?" for _ in chunk)
            conn.execute(
                f"""
                UPDATE tracked_comics
                   SET is_blacklisted = 1, skip_reason = ?
                 WHERE url IN ({placeholders})
                """,
                [CENSORED_SKIP_REASON] + chunk,
            )
        for chunk in _chunked(to_release):
            placeholders = ", ".join("?" for _ in chunk)
            conn.execute(
                f"""
                UPDATE tracked_comics
                   SET is_blacklisted = 0, skip_reason = NULL
                 WHERE skip_reason = ? AND url IN ({placeholders})
                """,
                [CENSORED_SKIP_REASON] + chunk,
            )
    conn.close()


def clear_tag_blacklist_for_urls(db_path: str, urls: List[str]):
    """Clear is_blacklisted/skip_reason for specific URLs currently flagged
    skip_reason == 'tag'.

    Used when a comic previously discovered (and tag-blacklisted) via a bulk
    artist listing becomes directly watched — the user's explicit intent to
    track it overrides the earlier bulk-scoped blacklist decision, so it is
    unblocked for re-evaluation. Only 'tag' entries are cleared; 'censored'
    and 'language' skip reasons are untouched even for direct watches.
    """
    if not urls:
        return
    conn = _connect(db_path)
    with conn:
        placeholders = ", ".join("?" for _ in urls)
        conn.execute(
            f"""
            UPDATE tracked_comics
               SET is_blacklisted = 0, skip_reason = NULL
             WHERE skip_reason = 'tag' AND url IN ({placeholders})
            """,
            list(urls),
        )
    conn.close()


def clear_comic_skip(db_path: str, url: str):
    """Clear a single comic's skip flag after it has passed the filters again.

    Called when a re-check finds the comic no longer matches the language or
    tag filters — its metadata changed on the site, or the config did. Applies
    to any skip_reason, since the decision is re-derived from fresh metadata.
    """
    conn = _connect(db_path)
    with conn:
        conn.execute(
            "UPDATE tracked_comics SET is_blacklisted = 0, skip_reason = NULL WHERE url = ?",
            (url,),
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


def set_comic_failed_page(db_path: str, url: str, page_num: Optional[int]):
    """Record (or clear) the 1-based page number that last failed to download.

    Pass *page_num=None* to clear the stored failure after a successful retry.
    """
    conn = _connect(db_path)
    with conn:
        conn.execute(
            "UPDATE tracked_comics SET failed_page = ? WHERE url = ?",
            (page_num, url),
        )
    conn.close()
