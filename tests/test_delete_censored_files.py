"""Tests for the delete_censored_files cleanup switch."""

import os
import tempfile
from unittest.mock import patch

import main
from modules import db
from modules.multporn import ArtistListing

ARTIST = "https://multporn.net/authors_comics/example_blurring_artist"
BLURRED = "https://multporn.net/comics/example_blurred_comic"
CLEAR = "https://multporn.net/comics/example_clear_comic"


class _DeletionTest:
    def setup_method(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.directory.name, "watcher.db")
        self.watchlist_path = os.path.join(self.directory.name, "watchlist.txt")
        self.output_dir = os.path.join(self.directory.name, "media")
        os.makedirs(self.output_dir)
        db.init_db(self.db_path)
        with open(self.watchlist_path, "w", encoding="utf-8") as handle:
            handle.write(ARTIST + "\n")
        self.listing = ArtistListing(comic_urls=[BLURRED, CLEAR], censored_urls={BLURRED})

    def teardown_method(self):
        self.directory.cleanup()

    def _config(self, **overrides) -> dict:
        config = {
            "output_dir": self.output_dir,
            "watchlist_file": self.watchlist_path,
            "full_check_interval_days": 28,
            "check_updated_feed": False,
            "exclude_censored": True,
            "delete_censored_files": True,
            "blacklisted_tags": [],
            "allowed_languages": ["en"],
            "watched_items": [],
        }
        config.update(overrides)
        return config

    def _downloaded(self, comic_url: str, filename: str, skip_reason: str = None) -> str:
        """A comic with its CBZ on disk, checked recently so no check re-runs it."""
        cbz_path = os.path.join(self.output_dir, filename)
        with open(cbz_path, "wb") as handle:
            handle.write(b"PK" + b"\x00" * 64)
        db.upsert_comic(self.db_path, comic_url)
        db.update_comic_checked(
            self.db_path, comic_url, 5, "[]", filename, filename, "1",
            cbz_hash="abc", last_synced=1.0,
        )
        db.link_comics_to_source(self.db_path, ARTIST, [comic_url])
        if skip_reason:
            db.upsert_comic(self.db_path, comic_url, is_blacklisted=1, skip_reason=skip_reason)
        return cbz_path

    def _cycle(self, config: dict = None, **config_overrides):
        # A comic the listing introduces is due for a first check; refusing its
        # metadata keeps every cycle offline and download-free.
        with patch.object(main.mp, "fetch_artist_comics", return_value=self.listing), \
             patch.object(main.mp, "fetch_comic_metadata", side_effect=RuntimeError("offline")), \
             patch.object(main.mp, "download_image", side_effect=AssertionError("no download expected")), \
             patch.object(main, "sleep"):
            main.run_once(config or self._config(**config_overrides), self.db_path)


class TestDeletion(_DeletionTest):
    def test_a_censored_comics_file_is_deleted(self):
        cbz_path = self._downloaded(BLURRED, "blurred.cbz", skip_reason=db.CENSORED_SKIP_REASON)
        self._cycle()
        assert not os.path.exists(cbz_path)

    def test_a_comic_blurred_by_this_cycles_artist_read_is_deleted(self):
        """The verdict recorded during the cycle is acted on in the same cycle."""
        cbz_path = self._downloaded(BLURRED, "blurred.cbz")
        self._cycle()
        assert not os.path.exists(cbz_path)

    def test_a_comic_that_is_not_censored_is_left_alone(self):
        cbz_path = self._downloaded(CLEAR, "clear.cbz")
        self._cycle()
        assert os.path.exists(cbz_path)

    def test_other_skip_reasons_are_left_alone(self):
        self.listing = ArtistListing(comic_urls=[CLEAR], censored_urls=set())
        cbz_path = self._downloaded(CLEAR, "tagged.cbz", skip_reason=db.TAG_SKIP_REASON)
        self._cycle()
        assert os.path.exists(cbz_path)

    def test_a_file_already_gone_is_not_an_error(self):
        cbz_path = self._downloaded(BLURRED, "blurred.cbz", skip_reason=db.CENSORED_SKIP_REASON)
        os.remove(cbz_path)
        self._cycle()


class TestRowAfterDeletion(_DeletionTest):
    def test_the_row_stays_marked_censored(self):
        self._downloaded(BLURRED, "blurred.cbz", skip_reason=db.CENSORED_SKIP_REASON)
        self._cycle()
        comic = db.get_comic(self.db_path, BLURRED)
        assert comic["skip_reason"] == db.CENSORED_SKIP_REASON
        assert comic["is_blacklisted"] == 1

    def test_the_row_is_reset_to_not_downloaded(self):
        """So an un-blurred comic downloads afresh instead of matching a gone file."""
        self._downloaded(BLURRED, "blurred.cbz", skip_reason=db.CENSORED_SKIP_REASON)
        self._cycle()
        comic = db.get_comic(self.db_path, BLURRED)
        assert comic["page_count"] == 0
        assert comic["cbz_path"] is None
        assert comic["cbz_hash"] is None
        assert comic["last_synced"] is None

    def test_the_deleted_comic_is_not_downloaded_again_while_censored(self):
        self._downloaded(BLURRED, "blurred.cbz", skip_reason=db.CENSORED_SKIP_REASON)
        self._cycle()
        self._cycle()
        assert db.get_comic(self.db_path, BLURRED)["page_count"] == 0


class TestSwitches(_DeletionTest):
    def test_nothing_is_deleted_when_the_option_is_off(self):
        cbz_path = self._downloaded(BLURRED, "blurred.cbz", skip_reason=db.CENSORED_SKIP_REASON)
        self._cycle(delete_censored_files=False)
        assert os.path.exists(cbz_path)

    def test_the_option_is_off_by_default(self):
        cbz_path = self._downloaded(BLURRED, "blurred.cbz", skip_reason=db.CENSORED_SKIP_REASON)
        config = self._config()
        del config["delete_censored_files"]
        self._cycle(config)
        assert os.path.exists(cbz_path)

    def test_nothing_is_deleted_without_exclude_censored(self):
        """With exclusion off, leftover censored marks are not current verdicts."""
        cbz_path = self._downloaded(BLURRED, "blurred.cbz", skip_reason=db.CENSORED_SKIP_REASON)
        main._delete_censored_files(self._config(exclude_censored=False), self.db_path)
        assert os.path.exists(cbz_path)


class TestPathSafety(_DeletionTest):
    def test_a_file_outside_output_dir_is_not_deleted(self):
        outside = os.path.join(self.directory.name, "elsewhere.cbz")
        with open(outside, "wb") as handle:
            handle.write(b"PK")
        db.upsert_comic(
            self.db_path, BLURRED, cbz_path=outside, page_count=5,
            is_blacklisted=1, skip_reason=db.CENSORED_SKIP_REASON,
        )
        main._delete_censored_files(self._config(), self.db_path)
        assert os.path.exists(outside)

    def test_a_relative_path_escaping_output_dir_is_not_deleted(self):
        outside = os.path.join(self.directory.name, "escaped.cbz")
        with open(outside, "wb") as handle:
            handle.write(b"PK")
        db.upsert_comic(
            self.db_path, BLURRED, cbz_path="../escaped.cbz", page_count=5,
            is_blacklisted=1, skip_reason=db.CENSORED_SKIP_REASON,
        )
        main._delete_censored_files(self._config(), self.db_path)
        assert os.path.exists(outside)

    def test_a_file_that_is_not_a_cbz_is_not_deleted(self):
        not_cbz = os.path.join(self.output_dir, "notes.txt")
        with open(not_cbz, "w", encoding="utf-8") as handle:
            handle.write("keep me")
        db.upsert_comic(
            self.db_path, BLURRED, cbz_path="notes.txt", page_count=5,
            is_blacklisted=1, skip_reason=db.CENSORED_SKIP_REASON,
        )
        main._delete_censored_files(self._config(), self.db_path)
        assert os.path.exists(not_cbz)


class TestOrderingWithCleanup(_DeletionTest):
    def test_the_file_is_deleted_before_cleanup_removes_the_row(self):
        """Cleanup deleting the row first would lose the only record of the path."""
        cbz_path = self._downloaded(BLURRED, "blurred.cbz", skip_reason=db.CENSORED_SKIP_REASON)
        other_artist = "https://multporn.net/authors_comics/example_still_watched"
        with open(self.watchlist_path, "w", encoding="utf-8") as handle:
            handle.write(other_artist + "\n")
        self.listing = ArtistListing(comic_urls=[], censored_urls=set())

        self._cycle(unwatched_retention_days=0)

        assert db.get_comic(self.db_path, BLURRED) is None
        assert not os.path.exists(cbz_path)
