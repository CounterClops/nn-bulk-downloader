import os
import sqlite3
import time
from typing import Dict, List, NamedTuple, Optional, Set

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
            ("unlinked_since", "REAL"),
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
        # Which watchlist entries provide each comic: an artist page for every
        # comic its listing contains, or a direct comic entry for itself. A
        # comic stays monitored while any one of its sources is still watched.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS comic_sources (
                comic_url  TEXT NOT NULL,
                source_url TEXT NOT NULL,
                PRIMARY KEY (comic_url, source_url)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comic_sources_source ON comic_sources (source_url)"
        )
        _clear_legacy_censored_skips(conn)
        _normalise_stored_urls(conn)
        _queue_comic_info_recheck(conn)
        _queue_source_link_backfill(conn)
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


COMIC_INFO_RECHECK_KEY = "comic_info_fields_recheck_queued"


def _queue_comic_info_recheck(conn: sqlite3.Connection):
    """Bring every tracked comic up for one full re-check on the next run.

    ComicInfo.xml gained sections, characters and namespaced user tags, and
    /mp<nodeid> pages had their section and artist fields read for the first
    time. Every CBZ written before that carries metadata the site has held all
    along, but a comic is only re-read once its full-check interval lapses — so
    left alone the library would take a whole interval to catch up.

    Clearing last_checked makes each comic due immediately. A comic whose page
    count still matches is never re-downloaded: its page is re-read and only
    ComicInfo.xml is rewritten, and only where it differs. The clear survives an
    interrupted run, since a comic re-stamps last_checked only once it has
    actually been checked.
    """
    already_queued = conn.execute(
        "SELECT 1 FROM settings WHERE key = ?", (COMIC_INFO_RECHECK_KEY,)
    ).fetchone()
    if already_queued:
        return

    conn.execute("UPDATE tracked_comics SET last_checked = NULL")
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (COMIC_INFO_RECHECK_KEY, str(time.time())),
    )


SOURCE_LINK_BACKFILL_KEY = "comic_source_links_backfill_queued"


def _queue_source_link_backfill(conn: sqlite3.Connection):
    """Refetch every artist page once so existing comics gain their source links.

    Links are recorded as sources are read, and comics tracked before links
    existed have none. An artist page is only re-read once its full-check
    interval lapses, so without this those comics would sit unmonitored — and
    eventually be cleaned up — until then. Forgetting every artist's last check
    makes each one due on the next run, which links everything it still lists.
    """
    already_queued = conn.execute(
        "SELECT 1 FROM settings WHERE key = ?", (SOURCE_LINK_BACKFILL_KEY,)
    ).fetchone()
    if already_queued:
        return

    conn.execute("DELETE FROM tracked_artists")
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (SOURCE_LINK_BACKFILL_KEY, str(time.time())),
    )


def _load_active_sources(conn: sqlite3.Connection, source_urls: Set[str]):
    """Stage the watched source URLs in a temp table for set-based queries.

    Cleanup needs NOT IN against the whole watchlist, which cannot be split
    across the chunked IN clauses used elsewhere.
    """
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS active_sources (url TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM active_sources")
    conn.executemany(
        "INSERT OR IGNORE INTO active_sources (url) VALUES (?)",
        [(url,) for url in source_urls],
    )


def link_comics_to_source(db_path: str, source_url: str, comic_urls: List[str]):
    """Record that *source_url* provides each of *comic_urls*.

    Links are only ever added here. A listing that comes back shorter than
    before — a comic pulled from the site, or a page the site served
    incompletely — does not unlink anything; a link ends only when its source
    leaves the watchlist.
    """
    if not comic_urls:
        return
    conn = _connect(db_path)
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO comic_sources (comic_url, source_url) VALUES (?, ?)",
            [(comic_url, source_url) for comic_url in comic_urls],
        )
    conn.close()


def get_comic_source_urls(db_path: str, comic_url: str) -> Set[str]:
    """Return every source recorded as providing *comic_url*."""
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT source_url FROM comic_sources WHERE comic_url = ?", (comic_url,)
    ).fetchall()
    conn.close()
    return {row["source_url"] for row in rows}


def get_monitored_comic_urls(db_path: str, active_source_urls: Set[str]) -> Set[str]:
    """Return the comics linked to at least one source still on the watchlist."""
    conn = _connect(db_path)
    with conn:
        _load_active_sources(conn, active_source_urls)
        rows = conn.execute(
            """
            SELECT DISTINCT comic_sources.comic_url
              FROM comic_sources
              JOIN active_sources ON active_sources.url = comic_sources.source_url
            """
        ).fetchall()
    conn.close()
    return {row["comic_url"] for row in rows}


class CleanupResult(NamedTuple):
    newly_unlinked: int
    relinked: int
    rows_deleted: int


def cleanup_unwatched(
    db_path: str,
    active_source_urls: Set[str],
    retention_s: float,
    now: float,
) -> CleanupResult:
    """Retire comics no watched source provides any more.

    Links belonging to sources that have left the watchlist are dropped, along
    with those artists' check timestamps, so re-adding an artist fetches its page
    straight away and restores its links in the same cycle.

    A comic left with no link is stamped *unlinked_since* and, once that has
    stood for *retention_s*, its row is deleted. A comic that regains a link
    before then has the stamp cleared. The CBZ on disk is never touched — a
    comic found again later is matched to its existing file rather than
    re-downloaded.

    Every column on a comic row is derived from the site or its CBZ, so there is
    nothing a deletion loses that the next discovery would not rebuild.
    """
    conn = _connect(db_path)
    with conn:
        _load_active_sources(conn, active_source_urls)
        conn.execute(
            "DELETE FROM comic_sources WHERE source_url NOT IN (SELECT url FROM active_sources)"
        )
        conn.execute(
            "DELETE FROM tracked_artists WHERE url NOT IN (SELECT url FROM active_sources)"
        )
        relinked = conn.execute(
            """
            UPDATE tracked_comics SET unlinked_since = NULL
             WHERE unlinked_since IS NOT NULL
               AND url IN (SELECT comic_url FROM comic_sources)
            """
        ).rowcount
        newly_unlinked = conn.execute(
            """
            UPDATE tracked_comics SET unlinked_since = ?
             WHERE unlinked_since IS NULL
               AND url NOT IN (SELECT comic_url FROM comic_sources)
            """,
            (now,),
        ).rowcount
        rows_deleted = conn.execute(
            "DELETE FROM tracked_comics WHERE unlinked_since IS NOT NULL AND unlinked_since <= ?",
            (now - retention_s,),
        ).rowcount
    conn.close()
    return CleanupResult(
        newly_unlinked=newly_unlinked, relinked=relinked, rows_deleted=rows_deleted,
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


def get_censored_comics_with_cbz(db_path: str) -> List[Dict]:
    """Return censored comics that still record a CBZ path (url and cbz_path only)."""
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT url, cbz_path FROM tracked_comics WHERE skip_reason = ? AND cbz_path IS NOT NULL",
        (CENSORED_SKIP_REASON,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def forget_comic_download(db_path: str, url: str):
    """Record that a comic's CBZ no longer exists, leaving its skip untouched.

    Page count and path are reset as for a comic never downloaded, so if the
    skip is ever lifted the comic downloads afresh rather than being judged up
    to date against a file that is gone.
    """
    conn = _connect(db_path)
    with conn:
        conn.execute(
            """
            UPDATE tracked_comics
               SET page_count = 0, cbz_path = NULL, cbz_hash = NULL, last_synced = NULL
             WHERE url = ?
            """,
            (url,),
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
TAG_SKIP_REASON = "tag"
USER_TAG_SKIP_REASON = "user_tag"

# Both tag filters are bulk-discovery decisions a direct watch overrides.
_TAG_SKIP_REASONS = (TAG_SKIP_REASON, USER_TAG_SKIP_REASON)

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
    """Clear is_blacklisted/skip_reason for specific URLs flagged by a tag filter.

    Used when a comic previously discovered (and tag-blacklisted) via a bulk
    artist listing becomes directly watched — the user's explicit intent to
    track it overrides the earlier bulk-scoped blacklist decision, so it is
    unblocked for re-evaluation. Both tag filters are cleared, since a direct
    watch is exempt from each; 'censored' and 'language' skip reasons are
    untouched even for direct watches.
    """
    if not urls:
        return
    conn = _connect(db_path)
    with conn:
        url_placeholders = ", ".join("?" for _ in urls)
        reason_placeholders = ", ".join("?" for _ in _TAG_SKIP_REASONS)
        conn.execute(
            f"""
            UPDATE tracked_comics
               SET is_blacklisted = 0, skip_reason = NULL
             WHERE skip_reason IN ({reason_placeholders})
               AND url IN ({url_placeholders})
            """,
            list(_TAG_SKIP_REASONS) + list(urls),
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
