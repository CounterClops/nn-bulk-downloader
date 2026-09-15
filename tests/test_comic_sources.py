"""Tests for linking comics to the watchlist entries that provide them, and for
retiring comics once no watched source provides them any more."""

import os
import sqlite3
import tempfile

from modules import db

DAY = 86400.0
RETENTION = 7 * DAY
NOW = 1_800_000_000.0

ARTIST_A = "https://multporn.net/authors_comics/example_artist_a"
ARTIST_B = "https://multporn.net/authors_comics/example_artist_b"
SHARED = "https://multporn.net/comics/example_shared"
ONLY_A = "https://multporn.net/comics/example_only_a"
DIRECT = "https://multporn.net/comics/example_direct"


def _make_db() -> str:
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    db.init_db(handle.name)
    return handle.name


def _track(db_path: str, *comic_urls: str):
    for comic_url in comic_urls:
        db.upsert_comic(db_path, comic_url)


def _unlinked_since(db_path: str, comic_url: str):
    comic = db.get_comic(db_path, comic_url)
    return comic["unlinked_since"] if comic else None


class _DatabaseTest:
    def setup_method(self):
        self.db_path = _make_db()

    def teardown_method(self):
        os.unlink(self.db_path)


class TestMultipleSources(_DatabaseTest):
    def test_a_comic_listed_by_two_artists_is_linked_to_both(self):
        _track(self.db_path, SHARED)
        db.link_comics_to_source(self.db_path, ARTIST_A, [SHARED])
        db.link_comics_to_source(self.db_path, ARTIST_B, [SHARED])
        assert db.get_monitored_comic_urls(self.db_path, {ARTIST_A}) == {SHARED}
        assert db.get_monitored_comic_urls(self.db_path, {ARTIST_B}) == {SHARED}

    def test_removing_one_of_two_sources_keeps_the_comic_monitored(self):
        _track(self.db_path, SHARED, ONLY_A)
        db.link_comics_to_source(self.db_path, ARTIST_A, [SHARED, ONLY_A])
        db.link_comics_to_source(self.db_path, ARTIST_B, [SHARED])

        monitored = db.get_monitored_comic_urls(self.db_path, {ARTIST_B})
        assert monitored == {SHARED}

    def test_removing_every_source_stops_monitoring(self):
        _track(self.db_path, SHARED)
        db.link_comics_to_source(self.db_path, ARTIST_A, [SHARED])
        db.link_comics_to_source(self.db_path, ARTIST_B, [SHARED])
        assert db.get_monitored_comic_urls(self.db_path, {DIRECT}) == set()

    def test_a_direct_watch_and_an_artist_can_both_provide_a_comic(self):
        _track(self.db_path, DIRECT)
        db.link_comics_to_source(self.db_path, DIRECT, [DIRECT])
        db.link_comics_to_source(self.db_path, ARTIST_A, [DIRECT])
        assert db.get_monitored_comic_urls(self.db_path, {ARTIST_A}) == {DIRECT}
        assert db.get_monitored_comic_urls(self.db_path, {DIRECT}) == {DIRECT}

    def test_linking_the_same_pair_twice_is_harmless(self):
        _track(self.db_path, SHARED)
        db.link_comics_to_source(self.db_path, ARTIST_A, [SHARED])
        db.link_comics_to_source(self.db_path, ARTIST_A, [SHARED])
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM comic_sources").fetchone()[0]
        conn.close()
        assert count == 1

    def test_a_shorter_listing_does_not_unlink_anything(self):
        """A comic pulled from the site, or a listing served incompletely, must
        not make a comic look abandoned while its artist is still watched."""
        _track(self.db_path, SHARED, ONLY_A)
        db.link_comics_to_source(self.db_path, ARTIST_A, [SHARED, ONLY_A])
        db.link_comics_to_source(self.db_path, ARTIST_A, [SHARED])
        assert db.get_monitored_comic_urls(self.db_path, {ARTIST_A}) == {SHARED, ONLY_A}


class TestRetention(_DatabaseTest):
    def _orphan(self, comic_url: str = ONLY_A):
        """A comic whose only source has just left the watchlist."""
        _track(self.db_path, comic_url, DIRECT)
        db.link_comics_to_source(self.db_path, ARTIST_A, [comic_url])
        db.link_comics_to_source(self.db_path, DIRECT, [DIRECT])

    def test_a_comic_losing_its_last_source_is_stamped_not_deleted(self):
        self._orphan()
        result = db.cleanup_unwatched(self.db_path, {DIRECT}, RETENTION, NOW)
        assert result.newly_unlinked == 1
        assert result.rows_deleted == 0
        assert _unlinked_since(self.db_path, ONLY_A) == NOW

    def test_the_row_survives_until_the_retention_period_has_passed(self):
        self._orphan()
        db.cleanup_unwatched(self.db_path, {DIRECT}, RETENTION, NOW)
        result = db.cleanup_unwatched(self.db_path, {DIRECT}, RETENTION, NOW + RETENTION - 60)
        assert result.rows_deleted == 0
        assert db.get_comic(self.db_path, ONLY_A) is not None

    def test_the_row_is_deleted_once_the_retention_period_has_passed(self):
        self._orphan()
        db.cleanup_unwatched(self.db_path, {DIRECT}, RETENTION, NOW)
        result = db.cleanup_unwatched(self.db_path, {DIRECT}, RETENTION, NOW + RETENTION)
        assert result.rows_deleted == 1
        assert db.get_comic(self.db_path, ONLY_A) is None

    def test_repeated_cleanups_do_not_restart_the_clock(self):
        self._orphan()
        db.cleanup_unwatched(self.db_path, {DIRECT}, RETENTION, NOW)
        db.cleanup_unwatched(self.db_path, {DIRECT}, RETENTION, NOW + 3 * DAY)
        assert _unlinked_since(self.db_path, ONLY_A) == NOW

    def test_regaining_a_source_within_retention_clears_the_stamp(self):
        self._orphan()
        db.cleanup_unwatched(self.db_path, {DIRECT}, RETENTION, NOW)

        db.link_comics_to_source(self.db_path, ARTIST_A, [ONLY_A])
        result = db.cleanup_unwatched(self.db_path, {DIRECT, ARTIST_A}, RETENTION, NOW + 6 * DAY)

        assert result.relinked == 1
        assert _unlinked_since(self.db_path, ONLY_A) is None
        result = db.cleanup_unwatched(self.db_path, {DIRECT, ARTIST_A}, RETENTION, NOW + 30 * DAY)
        assert result.rows_deleted == 0

    def test_a_comic_still_provided_by_a_second_source_is_never_stamped(self):
        _track(self.db_path, SHARED)
        db.link_comics_to_source(self.db_path, ARTIST_A, [SHARED])
        db.link_comics_to_source(self.db_path, ARTIST_B, [SHARED])
        db.cleanup_unwatched(self.db_path, {ARTIST_B}, RETENTION, NOW)
        result = db.cleanup_unwatched(self.db_path, {ARTIST_B}, RETENTION, NOW + 30 * DAY)
        assert result.rows_deleted == 0
        assert _unlinked_since(self.db_path, SHARED) is None

    def test_zero_retention_deletes_in_the_same_cleanup(self):
        self._orphan()
        result = db.cleanup_unwatched(self.db_path, {DIRECT}, 0, NOW)
        assert result.rows_deleted == 1

    def test_only_unlinked_rows_are_ever_deleted(self):
        self._orphan()
        db.cleanup_unwatched(self.db_path, {DIRECT}, 0, NOW)
        assert db.get_comic(self.db_path, DIRECT) is not None


class TestRemovedSourceHousekeeping(_DatabaseTest):
    def test_links_of_removed_sources_are_dropped(self):
        _track(self.db_path, SHARED)
        db.link_comics_to_source(self.db_path, ARTIST_A, [SHARED])
        db.link_comics_to_source(self.db_path, ARTIST_B, [SHARED])
        db.cleanup_unwatched(self.db_path, {ARTIST_B}, RETENTION, NOW)

        conn = sqlite3.connect(self.db_path)
        sources = {row[0] for row in conn.execute("SELECT source_url FROM comic_sources")}
        conn.close()
        assert sources == {ARTIST_B}

    def test_removed_artists_forget_their_last_check(self):
        """Re-adding an artist must fetch its page straight away to restore links."""
        db.upsert_artist_checked(self.db_path, ARTIST_A)
        db.upsert_artist_checked(self.db_path, ARTIST_B)
        db.cleanup_unwatched(self.db_path, {ARTIST_B}, RETENTION, NOW)
        assert db.get_artist_last_checked(self.db_path, ARTIST_A) is None
        assert db.get_artist_last_checked(self.db_path, ARTIST_B) is not None


class TestSourceLinkBackfill:
    def test_every_artist_is_made_due_once(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        try:
            db.init_db(handle.name)
            conn = sqlite3.connect(handle.name)
            with conn:
                conn.execute("DELETE FROM settings WHERE key = ?", (db.SOURCE_LINK_BACKFILL_KEY,))
            conn.close()
            db.upsert_artist_checked(handle.name, ARTIST_A)

            db.init_db(handle.name)
            assert db.get_artist_last_checked(handle.name, ARTIST_A) is None

            db.upsert_artist_checked(handle.name, ARTIST_A)
            db.init_db(handle.name)
            assert db.get_artist_last_checked(handle.name, ARTIST_A) is not None
        finally:
            os.unlink(handle.name)

    def test_existing_databases_gain_the_unlinked_since_column(self):
        db_path = _make_db()
        try:
            db.upsert_comic(db_path, SHARED)
            assert "unlinked_since" in db.get_comic(db_path, SHARED)
        finally:
            os.unlink(db_path)
