"""Tests that skip decisions are re-derived from fresh metadata rather than
being frozen at the moment of discovery."""

import os
import tempfile
import time
from unittest.mock import patch

from main import _mark_skipped, _needs_check, _process_comic
from modules import db

INTERVAL_S = 28 * 86400

CLEAN_META = {
    "title": "Clean Comic",
    "tags": ["Curated Tag A"],
    "author": "Some Author",
    "language": "en",
    "image_urls": [],
    "page_count": 1,
    "node_id": "1",
}

CONFIG = {
    "blacklisted_tags": ["Blacklisted Tag"],
    "allowed_languages": ["en"],
    "exclude_censored": False,
    "output_dir": "/tmp",
}


def _make_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db.init_db(tmp.name)
    return tmp.name


# ---------------------------------------------------------------------------
# _needs_check
# ---------------------------------------------------------------------------

class TestNeedsCheck:
    def test_skipped_comic_is_rechecked_when_interval_elapses(self):
        assert _needs_check(
            is_skipped=True, skip_reason="tag", never_downloaded=True,
            in_updated_feed=False, due_for_full_check=True,
        ) is True

    def test_skipped_comic_is_rechecked_when_in_updated_feed(self):
        assert _needs_check(
            is_skipped=True, skip_reason="tag", never_downloaded=True,
            in_updated_feed=True, due_for_full_check=False,
        ) is True

    def test_skipped_comic_is_left_alone_between_intervals(self):
        """never_downloaded must not drag the whole skip list back every poll."""
        assert _needs_check(
            is_skipped=True, skip_reason="tag", never_downloaded=True,
            in_updated_feed=False, due_for_full_check=False,
        ) is False

    def test_new_comic_is_checked_immediately(self):
        assert _needs_check(
            is_skipped=False, skip_reason=None, never_downloaded=True,
            in_updated_feed=False, due_for_full_check=False,
        ) is True

    def test_current_comic_is_left_alone(self):
        assert _needs_check(
            is_skipped=False, skip_reason=None, never_downloaded=False,
            in_updated_feed=False, due_for_full_check=False,
        ) is False

    def test_censored_comic_is_never_fetched_for_recheck(self):
        """Its own page carries no blur marker, so a fetch would learn nothing."""
        assert _needs_check(
            is_skipped=True, skip_reason="censored", never_downloaded=True,
            in_updated_feed=True, due_for_full_check=True,
        ) is False


# ---------------------------------------------------------------------------
# _mark_skipped
# ---------------------------------------------------------------------------

class TestMarkSkipped:
    def test_stamps_last_checked(self):
        """Without a timestamp the comic would be re-fetched on every poll."""
        db_path = _make_db()
        try:
            url = "https://multporn.net/comics/example_skipped"
            before = time.time()
            _mark_skipped(db_path, url, "Skipped Comic", ["Some Tag"], "tag")

            row = db.get_comic(db_path, url)
            assert row["is_blacklisted"] == 1
            assert row["skip_reason"] == "tag"
            assert row["last_checked"] >= before
        finally:
            os.unlink(db_path)


# ---------------------------------------------------------------------------
# Lifting a stale skip
# ---------------------------------------------------------------------------

class TestStaleSkipIsLifted:
    def test_process_comic_clears_skip_when_metadata_now_passes(self):
        db_path = _make_db()
        try:
            url = "https://multporn.net/comics/example_was_skipped"
            _mark_skipped(db_path, url, "Clean Comic", ["Blacklisted Tag"], "tag")

            # The site has since dropped the offending tag.
            with patch("main.mp.fetch_comic_metadata", return_value=CLEAN_META):
                _process_comic(None, url, CONFIG, db_path, INTERVAL_S)

            row = db.get_comic(db_path, url)
            assert row["is_blacklisted"] == 0
            assert row["skip_reason"] is None
        finally:
            os.unlink(db_path)

    def test_skip_is_reapplied_when_metadata_still_matches(self):
        db_path = _make_db()
        try:
            url = "https://multporn.net/comics/example_still_skipped"
            _mark_skipped(db_path, url, "Dirty Comic", ["Blacklisted Tag"], "tag")

            still_tagged = dict(CLEAN_META, tags=["Blacklisted Tag"])
            with patch("main.mp.fetch_comic_metadata", return_value=still_tagged):
                _process_comic(None, url, CONFIG, db_path, INTERVAL_S)

            row = db.get_comic(db_path, url)
            assert row["is_blacklisted"] == 1
            assert row["skip_reason"] == "tag"
        finally:
            os.unlink(db_path)

    def test_censored_skip_survives_a_comic_level_pass(self):
        """Only artist discovery may revisit the site's blur verdict."""
        db_path = _make_db()
        try:
            url = "https://multporn.net/comics/example_blurred"
            _mark_skipped(db_path, url, "Clean Comic", [], "censored")

            with patch("main.mp.fetch_comic_metadata", return_value=CLEAN_META):
                _process_comic(None, url, CONFIG, db_path, INTERVAL_S)

            row = db.get_comic(db_path, url)
            assert row["is_blacklisted"] == 1
            assert row["skip_reason"] == "censored"
        finally:
            os.unlink(db_path)

    def test_language_skip_is_lifted_when_language_changes(self):
        db_path = _make_db()
        try:
            url = "https://multporn.net/comics/example_was_russian"
            _mark_skipped(db_path, url, "Clean Comic", [], "language")

            with patch("main.mp.fetch_comic_metadata", return_value=CLEAN_META):
                _process_comic(None, url, CONFIG, db_path, INTERVAL_S)

            row = db.get_comic(db_path, url)
            assert row["is_blacklisted"] == 0
            assert row["skip_reason"] is None
        finally:
            os.unlink(db_path)
