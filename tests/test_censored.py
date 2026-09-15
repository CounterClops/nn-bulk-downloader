"""Tests for censored-content handling.

Censoring is a discovery-time concern only: artist listings omit comics whose
preview thumbnail carries the site's ``blur_comics`` marker. There is no
tag-based censoring — the retired hardcoded tag list used to skip comics the
site itself had not marked, and its leftover rows are cleared on first init.
"""

import os
import sqlite3
import tempfile
from unittest.mock import patch

import main
from main import _process_comic
from modules import db

FORMERLY_CENSORED_TAG_META = {
    "title": "Tagged Comic",
    "tags": ["Curated Tag A", "Formerly Censored Tag", "Curated Tag B"],
    "author": "Some Author",
    "language": "en",
    "image_urls": [],
    "page_count": 1,
    "node_id": "1",
}

CONFIG = {
    "blacklisted_tags": [],
    "allowed_languages": ["en"],
    "exclude_censored": True,
    "output_dir": "/tmp",
}


def _make_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db.init_db(tmp.name)
    return tmp.name


def _rewind_cleanup_marker(db_path: str):
    """Drop the cleanup's completion marker to simulate a database created
    before the migration existed, so the next init_db() applies it."""
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "DELETE FROM settings WHERE key = ?", (db.LEGACY_CENSORED_CLEANUP_KEY,)
        )
    conn.close()


# ---------------------------------------------------------------------------
# No tag-based censoring
# ---------------------------------------------------------------------------

class TestNoTagBasedCensoring:
    def test_formerly_censored_tag_is_not_skipped(self):
        """A tag the site has not blurred must not be treated as censored."""
        db_path = _make_db()
        try:
            url = "https://multporn.net/comics/example_formerly_censored_tag"
            db.upsert_comic(db_path, url)
            db.update_comic_checked(db_path, url, 1, "[]", "x.cbz", "Tagged Comic", "1")

            with patch("main.mp.fetch_comic_metadata", return_value=FORMERLY_CENSORED_TAG_META):
                _process_comic(None, url, CONFIG, db_path, 28 * 86400)

            row = db.get_comic(db_path, url)
            assert row["is_blacklisted"] == 0
            assert row["skip_reason"] is None
        finally:
            os.unlink(db_path)

    def test_no_censored_helper_remains(self):
        assert not hasattr(main, "_is_censored")


# ---------------------------------------------------------------------------
# One-time cleanup of the retired filter's rows
# ---------------------------------------------------------------------------

class TestLegacyCensoredCleanup:
    def test_clears_censored_rows_on_init(self):
        db_path = _make_db()
        try:
            url = "https://multporn.net/comics/example_a"
            db.upsert_comic(db_path, url, is_blacklisted=1, skip_reason="censored")
            _rewind_cleanup_marker(db_path)

            db.init_db(db_path)

            row = db.get_comic(db_path, url)
            assert row["is_blacklisted"] == 0
            assert row["skip_reason"] is None
        finally:
            os.unlink(db_path)

    def test_leaves_other_skip_reasons_alone(self):
        db_path = _make_db()
        try:
            db.upsert_comic(db_path, "https://multporn.net/comics/example_t",
                            is_blacklisted=1, skip_reason="tag")
            db.upsert_comic(db_path, "https://multporn.net/comics/example_l",
                            is_blacklisted=1, skip_reason="language")
            _rewind_cleanup_marker(db_path)

            db.init_db(db_path)

            tag_row = db.get_comic(db_path, "https://multporn.net/comics/example_t")
            assert tag_row["is_blacklisted"] == 1
            assert tag_row["skip_reason"] == "tag"

            lang_row = db.get_comic(db_path, "https://multporn.net/comics/example_l")
            assert lang_row["is_blacklisted"] == 1
            assert lang_row["skip_reason"] == "language"
        finally:
            os.unlink(db_path)

    def test_runs_only_once(self):
        """A censored skip written after the cleanup must survive later inits."""
        db_path = _make_db()
        try:
            db.init_db(db_path)  # cleanup runs and is recorded here

            url = "https://multporn.net/comics/example_later"
            db.upsert_comic(db_path, url, is_blacklisted=1, skip_reason="censored")

            db.init_db(db_path)

            row = db.get_comic(db_path, url)
            assert row["is_blacklisted"] == 1
            assert row["skip_reason"] == "censored"
        finally:
            os.unlink(db_path)


# ---------------------------------------------------------------------------
# Reconciling the blur verdict against an artist listing
# ---------------------------------------------------------------------------

BLURRED = "https://multporn.net/comics/example_blurred"
CLEAN = "https://multporn.net/comics/example_clean"


class TestSyncCensoredSkips:
    def test_marks_blurred_comics(self):
        db_path = _make_db()
        try:
            for url in (BLURRED, CLEAN):
                db.upsert_comic(db_path, url)

            db.sync_censored_skips(db_path, [BLURRED, CLEAN], {BLURRED})

            blurred_row = db.get_comic(db_path, BLURRED)
            assert blurred_row["is_blacklisted"] == 1
            assert blurred_row["skip_reason"] == "censored"

            clean_row = db.get_comic(db_path, CLEAN)
            assert clean_row["is_blacklisted"] == 0
        finally:
            os.unlink(db_path)

    def test_releases_comics_the_site_no_longer_blurs(self):
        db_path = _make_db()
        try:
            db.upsert_comic(db_path, BLURRED)
            db.sync_censored_skips(db_path, [BLURRED], {BLURRED})

            # Next refresh: the site has lifted the blur.
            db.sync_censored_skips(db_path, [BLURRED], set())

            row = db.get_comic(db_path, BLURRED)
            assert row["is_blacklisted"] == 0
            assert row["skip_reason"] is None
        finally:
            os.unlink(db_path)

    def test_releasing_leaves_other_skip_reasons_intact(self):
        """A tag skip must survive; only the censored reason is reconciled."""
        db_path = _make_db()
        try:
            db.upsert_comic(db_path, CLEAN, is_blacklisted=1, skip_reason="tag")

            db.sync_censored_skips(db_path, [CLEAN], set())

            row = db.get_comic(db_path, CLEAN)
            assert row["is_blacklisted"] == 1
            assert row["skip_reason"] == "tag"
        finally:
            os.unlink(db_path)

    def test_censored_takes_precedence_over_a_tag_skip(self):
        db_path = _make_db()
        try:
            db.upsert_comic(db_path, BLURRED, is_blacklisted=1, skip_reason="tag")

            db.sync_censored_skips(db_path, [BLURRED], {BLURRED})

            row = db.get_comic(db_path, BLURRED)
            assert row["skip_reason"] == "censored"
        finally:
            os.unlink(db_path)

    def test_comics_outside_the_listing_are_untouched(self):
        db_path = _make_db()
        try:
            other = "https://multporn.net/comics/example_other_artist"
            db.upsert_comic(db_path, other, is_blacklisted=1, skip_reason="censored")
            db.upsert_comic(db_path, CLEAN)

            db.sync_censored_skips(db_path, [CLEAN], set())

            row = db.get_comic(db_path, other)
            assert row["is_blacklisted"] == 1
            assert row["skip_reason"] == "censored"
        finally:
            os.unlink(db_path)
