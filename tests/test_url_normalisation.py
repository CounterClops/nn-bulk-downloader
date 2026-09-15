"""Tests that URLs are canonical on the way in, and that rows recorded before
canonicalisation are reconciled."""

import os
import sqlite3
import tempfile

from modules import db
from modules.multporn import normalise_url


def _make_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db.init_db(tmp.name)
    return tmp.name


def _rewind_migration_marker(db_path: str):
    """Simulate a database written before the migration existed."""
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "DELETE FROM settings WHERE key = ?", (db.LEGACY_URL_NORMALISATION_KEY,)
        )
    conn.close()


def _insert_raw(db_path: str, url: str, **fields):
    """Insert a row under a literal URL, bypassing any canonicalisation."""
    columns = {"url": url, "first_seen": 1000.0, **fields}
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            f"INSERT INTO tracked_comics ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            list(columns.values()),
        )
    conn.close()


# ---------------------------------------------------------------------------
# normalise_url
# ---------------------------------------------------------------------------

class TestNormaliseUrl:
    def test_strips_query_string(self):
        assert normalise_url("https://multporn.net/comics/foo?r=1") == \
            "https://multporn.net/comics/foo"

    def test_strips_multi_valued_query_string(self):
        assert normalise_url("https://multporn.net/comics/foo?rule34=2") == \
            "https://multporn.net/comics/foo"

    def test_strips_fragment(self):
        assert normalise_url("https://multporn.net/comics/foo#page3") == \
            "https://multporn.net/comics/foo"

    def test_strips_trailing_slash(self):
        assert normalise_url("https://multporn.net/comics/foo/") == \
            "https://multporn.net/comics/foo"

    def test_leaves_clean_urls_untouched(self):
        url = "https://multporn.net/comics/foo"
        assert normalise_url(url) == url

    def test_handles_relative_hrefs(self):
        assert normalise_url("/comics/foo?r=1") == "/comics/foo"

    def test_is_idempotent(self):
        once = normalise_url("https://multporn.net/comics/foo/?r=1#x")
        assert normalise_url(once) == once


# ---------------------------------------------------------------------------
# Reconciling rows recorded before canonicalisation
# ---------------------------------------------------------------------------

BARE = "https://example.com/comics/dup"
VARIANT = "https://example.com/comics/dup?r=1"


class TestStoredUrlNormalisation:
    def test_renames_a_lone_variant_row(self):
        db_path = _make_db()
        try:
            _insert_raw(db_path, VARIANT, page_count=12, node_id="7")
            _rewind_migration_marker(db_path)

            db.init_db(db_path)

            assert db.get_comic(db_path, VARIANT) is None
            row = db.get_comic(db_path, BARE)
            assert row["page_count"] == 12
            assert row["node_id"] == "7"
        finally:
            os.unlink(db_path)

    def test_merges_variant_into_existing_bare_row(self):
        db_path = _make_db()
        try:
            _insert_raw(db_path, BARE, page_count=33, node_id="7")
            _insert_raw(db_path, VARIANT, page_count=34, node_id="7")
            _rewind_migration_marker(db_path)

            db.init_db(db_path)

            assert db.get_comic(db_path, VARIANT) is None
            row = db.get_comic(db_path, BARE)
            assert row["page_count"] == 34, "the better-developed row supplies content"
        finally:
            os.unlink(db_path)

    def test_merge_keeps_the_downloaded_archive(self):
        db_path = _make_db()
        try:
            _insert_raw(db_path, BARE, page_count=0)
            _insert_raw(db_path, VARIANT, page_count=20, cbz_path="dup.cbz",
                        cbz_hash="abc", title="Dup")
            _rewind_migration_marker(db_path)

            db.init_db(db_path)

            row = db.get_comic(db_path, BARE)
            assert row["cbz_path"] == "dup.cbz"
            assert row["cbz_hash"] == "abc"
            assert row["title"] == "Dup"
        finally:
            os.unlink(db_path)

    def test_merge_preserves_a_skip_from_either_row(self):
        """Erring toward not downloading; the per-comic re-check lifts it."""
        db_path = _make_db()
        try:
            _insert_raw(db_path, BARE, is_blacklisted=1, skip_reason="tag")
            _insert_raw(db_path, VARIANT)
            _rewind_migration_marker(db_path)

            db.init_db(db_path)

            row = db.get_comic(db_path, BARE)
            assert row["is_blacklisted"] == 1
            assert row["skip_reason"] == "tag"
        finally:
            os.unlink(db_path)

    def test_merge_spans_timestamps(self):
        db_path = _make_db()
        try:
            _insert_raw(db_path, BARE, first_seen=100.0, last_checked=500.0)
            _insert_raw(db_path, VARIANT, first_seen=200.0, last_checked=900.0)
            _rewind_migration_marker(db_path)

            db.init_db(db_path)

            row = db.get_comic(db_path, BARE)
            assert row["first_seen"] == 100.0, "earliest sighting wins"
            assert row["last_checked"] == 900.0, "most recent check wins"
        finally:
            os.unlink(db_path)

    def test_collapses_three_variants_into_one_row(self):
        db_path = _make_db()
        try:
            _insert_raw(db_path, BARE, page_count=5)
            _insert_raw(db_path, VARIANT, page_count=5)
            _insert_raw(db_path, "https://example.com/comics/dup?r=2", page_count=5)
            _rewind_migration_marker(db_path)

            db.init_db(db_path)

            rows = [c for c in db.get_all_comics(db_path) if "dup" in c["url"]]
            assert len(rows) == 1
            assert rows[0]["url"] == BARE
        finally:
            os.unlink(db_path)

    def test_leaves_distinct_comics_alone(self):
        db_path = _make_db()
        try:
            _insert_raw(db_path, "https://example.com/comics/one", page_count=1)
            _insert_raw(db_path, "https://example.com/comics/two", page_count=2)
            _rewind_migration_marker(db_path)

            db.init_db(db_path)

            assert db.get_comic(db_path, "https://example.com/comics/one")["page_count"] == 1
            assert db.get_comic(db_path, "https://example.com/comics/two")["page_count"] == 2
        finally:
            os.unlink(db_path)

    def test_runs_only_once(self):
        db_path = _make_db()
        try:
            db.init_db(db_path)  # marker recorded here

            _insert_raw(db_path, VARIANT, page_count=3)
            db.init_db(db_path)

            assert db.get_comic(db_path, VARIANT) is not None
        finally:
            os.unlink(db_path)
