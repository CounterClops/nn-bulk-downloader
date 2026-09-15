"""Tests for the one-off queue that re-checks every comic's metadata.

The ComicInfo layout gained fields, so every existing comic is brought up for a
single full re-check rather than waiting out its full-check interval.
"""

import os
import sqlite3
import tempfile
import time

from main import _describe_last_checked, _needs_check
from modules import db

FULL_CHECK_INTERVAL_S = 28 * 86400


def _legacy_db_with_comics(*urls: str) -> str:
    """Build a database as it stood before the recheck existed, holding recently
    checked comics that would not otherwise be due for weeks."""
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    db.init_db(handle.name)

    conn = sqlite3.connect(handle.name)
    with conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (db.COMIC_INFO_RECHECK_KEY,))
        for url in urls:
            conn.execute(
                "INSERT INTO tracked_comics (url, first_seen, last_checked, page_count) "
                "VALUES (?, ?, ?, 10)",
                (url, time.time(), time.time() - 86400),
            )
    conn.close()
    return handle.name


def _is_due(comic: dict, now: float) -> bool:
    last_checked = comic.get("last_checked") or 0
    return _needs_check(
        is_skipped=bool(comic["is_blacklisted"]),
        skip_reason=comic["skip_reason"],
        never_downloaded=comic["page_count"] == 0,
        in_updated_feed=False,
        due_for_full_check=(now - last_checked) >= FULL_CHECK_INTERVAL_S,
    )


class TestRecheckIsQueued:
    def test_recently_checked_comics_are_not_due_before_the_recheck(self):
        db_path = _legacy_db_with_comics("https://multporn.net/comics/example_a")
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            comic = dict(conn.execute("SELECT * FROM tracked_comics").fetchone())
            conn.close()
            assert not _is_due(comic, time.time())
        finally:
            os.unlink(db_path)

    def test_every_comic_is_due_after_init(self):
        urls = [f"https://multporn.net/comics/example_comic_{index}" for index in range(5)]
        db_path = _legacy_db_with_comics(*urls)
        try:
            db.init_db(db_path)
            now = time.time()
            comics = db.get_all_comics(db_path)
            assert len(comics) == 5
            assert all(_is_due(comic, now) for comic in comics)
        finally:
            os.unlink(db_path)

    def test_page_counts_and_paths_are_left_alone(self):
        """The recheck only makes comics due; it must not look undownloaded."""
        db_path = _legacy_db_with_comics("https://multporn.net/comics/example_a")
        try:
            db.init_db(db_path)
            comic = db.get_comic(db_path, "https://multporn.net/comics/example_a")
            assert comic["page_count"] == 10
        finally:
            os.unlink(db_path)

    def test_censored_comics_stay_out_of_the_recheck(self):
        """Their verdict lives only on artist listings, so a page fetch learns nothing."""
        url = "https://multporn.net/comics/example_censored"
        db_path = _legacy_db_with_comics(url)
        try:
            db.upsert_comic(db_path, url, is_blacklisted=1, skip_reason=db.CENSORED_SKIP_REASON)
            db.init_db(db_path)
            assert not _is_due(db.get_comic(db_path, url), time.time())
        finally:
            os.unlink(db_path)


class TestRecheckRunsOnce:
    def test_a_second_init_does_not_queue_it_again(self):
        url = "https://multporn.net/comics/example_a"
        db_path = _legacy_db_with_comics(url)
        try:
            db.init_db(db_path)
            db.update_comic_checked(db_path, url, 10, "[]", "a.cbz", "A", "1")
            checked_at = db.get_comic(db_path, url)["last_checked"]

            db.init_db(db_path)
            db.init_db(db_path)

            assert db.get_comic(db_path, url)["last_checked"] == checked_at
            assert not _is_due(db.get_comic(db_path, url), time.time())
        finally:
            os.unlink(db_path)

    def test_the_queue_is_recorded_in_settings(self):
        db_path = _legacy_db_with_comics("https://multporn.net/comics/example_a")
        try:
            assert db.get_setting(db_path, db.COMIC_INFO_RECHECK_KEY) is None
            db.init_db(db_path)
            assert db.get_setting(db_path, db.COMIC_INFO_RECHECK_KEY) is not None
        finally:
            os.unlink(db_path)

    def test_an_interrupted_run_leaves_unchecked_comics_due(self):
        """Only comics actually checked are re-stamped, so a run stopped part-way
        still reaches the rest next time."""
        checked, unchecked = "https://multporn.net/comics/example_done", "https://multporn.net/comics/example_pending"
        db_path = _legacy_db_with_comics(checked, unchecked)
        try:
            db.init_db(db_path)
            db.update_comic_checked(db_path, checked, 10, "[]", "d.cbz", "Done", "1")

            db.init_db(db_path)
            now = time.time()
            assert not _is_due(db.get_comic(db_path, checked), now)
            assert _is_due(db.get_comic(db_path, unchecked), now)
        finally:
            os.unlink(db_path)

    def test_a_fresh_database_is_marked_without_error(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "fresh.db")
            db.init_db(db_path)
            assert db.get_setting(db_path, db.COMIC_INFO_RECHECK_KEY) is not None
            assert db.get_all_comics(db_path) == []


class TestLastCheckedDescription:
    def test_an_unset_timestamp_reads_as_not_yet_checked(self):
        assert _describe_last_checked(0, time.time()) == "not yet checked"
        assert _describe_last_checked(None, time.time()) == "not yet checked"

    def test_a_real_timestamp_reads_as_days_ago(self):
        now = time.time()
        assert _describe_last_checked(now - 3 * 86400, now) == "last checked 3.0d ago"
