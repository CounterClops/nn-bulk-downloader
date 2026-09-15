"""Poll-cycle tests for removing entries from the watchlist.

The site is mocked out: artist listings come from a dict, and _process_comic is
replaced with a recorder, so each test sees exactly which comics a cycle checks
and what cleanup does to the database afterwards.
"""

import os
import tempfile
from unittest.mock import patch

import requests

import main
from modules import db
from modules.multporn import ArtistListing

ARTIST_A = "https://multporn.net/authors_comics/artist_a"
ARTIST_B = "https://multporn.net/authors_comics/artist_b"
SHARED = "https://multporn.net/comics/shared"
ONLY_A = "https://multporn.net/comics/only_a"
ONLY_B = "https://multporn.net/comics/only_b"
DIRECT = "https://multporn.net/comics/direct"

LISTINGS = {
    ARTIST_A: [SHARED, ONLY_A],
    ARTIST_B: [SHARED, ONLY_B],
}


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"HTTP {status}", response=response)


class _CycleTest:
    def setup_method(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.directory.name, "watcher.db")
        self.watchlist_path = os.path.join(self.directory.name, "watchlist.txt")
        self.output_dir = os.path.join(self.directory.name, "media")
        db.init_db(self.db_path)
        self.listing_errors = {}

    def teardown_method(self):
        self.directory.cleanup()

    def _config(self, retention_days: int = 7) -> dict:
        return {
            "output_dir": self.output_dir,
            "watchlist_file": self.watchlist_path,
            "full_check_interval_days": 0,
            "unwatched_retention_days": retention_days,
            "check_updated_feed": False,
            "exclude_censored": False,
            "watched_items": [],
        }

    def _write_watchlist(self, *urls: str):
        with open(self.watchlist_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(urls) + "\n")

    def _fetch_listing(self, _session, url):
        if url in self.listing_errors:
            raise self.listing_errors[url]
        return ArtistListing(comic_urls=list(LISTINGS[url]), censored_urls=set())

    def _cycle(self, retention_days: int = 7) -> set:
        """Run one poll cycle and return the comic URLs it checked."""
        checked = set()

        def record(_session, url, *_args, **_kwargs):
            checked.add(url)

        with patch.object(main.mp, "fetch_artist_comics", side_effect=self._fetch_listing), \
             patch.object(main, "_process_comic", side_effect=record), \
             patch.object(main, "sleep"):
            main.run_once(self._config(retention_days), self.db_path)
        return checked


class TestRemovingAnArtist(_CycleTest):
    def test_its_comics_stop_being_checked(self):
        self._write_watchlist(ARTIST_A, ARTIST_B)
        assert self._cycle() == {SHARED, ONLY_A, ONLY_B}

        self._write_watchlist(ARTIST_B)
        assert self._cycle() == {SHARED, ONLY_B}

    def test_a_comic_another_watched_artist_provides_keeps_being_checked(self):
        self._write_watchlist(ARTIST_A, ARTIST_B)
        self._cycle()
        self._write_watchlist(ARTIST_B)
        assert SHARED in self._cycle()

    def test_rows_are_kept_through_the_retention_period(self):
        self._write_watchlist(ARTIST_A, ARTIST_B)
        self._cycle()
        self._write_watchlist(ARTIST_B)
        self._cycle(retention_days=7)
        assert db.get_comic(self.db_path, ONLY_A) is not None
        assert db.get_comic(self.db_path, ONLY_A)["unlinked_since"] is not None

    def test_rows_are_removed_once_retention_has_passed(self):
        self._write_watchlist(ARTIST_A, ARTIST_B)
        self._cycle()
        self._write_watchlist(ARTIST_B)
        self._cycle(retention_days=0)
        assert db.get_comic(self.db_path, ONLY_A) is None
        assert db.get_comic(self.db_path, SHARED) is not None

    def test_the_cbz_file_is_left_on_disk(self):
        self._write_watchlist(ARTIST_A)
        self._cycle()
        os.makedirs(self.output_dir, exist_ok=True)
        cbz_path = os.path.join(self.output_dir, "only_a.cbz")
        with open(cbz_path, "wb") as handle:
            handle.write(b"PK")
        db.update_comic_checked(self.db_path, ONLY_A, 3, "[]", "only_a.cbz", "Only A", "1")

        self._write_watchlist(DIRECT)
        self._cycle(retention_days=0)

        assert db.get_comic(self.db_path, ONLY_A) is None
        assert os.path.exists(cbz_path)

    def test_re_adding_the_artist_within_retention_restores_monitoring(self):
        self._write_watchlist(ARTIST_A, ARTIST_B)
        self._cycle()
        self._write_watchlist(ARTIST_B)
        self._cycle(retention_days=7)

        self._write_watchlist(ARTIST_A, ARTIST_B)
        assert ONLY_A in self._cycle(retention_days=7)
        assert db.get_comic(self.db_path, ONLY_A)["unlinked_since"] is None


class TestRemovingADirectComic(_CycleTest):
    def test_it_stops_being_checked(self):
        self._write_watchlist(DIRECT, ARTIST_B)
        assert DIRECT in self._cycle()
        self._write_watchlist(ARTIST_B)
        assert DIRECT not in self._cycle()

    def test_an_artist_still_listing_it_keeps_it_checked(self):
        self._write_watchlist(SHARED, ARTIST_A)
        self._cycle()
        self._write_watchlist(ARTIST_A)
        assert SHARED in self._cycle()


class TestCleanupSafeguards(_CycleTest):
    def test_a_transient_source_failure_holds_back_cleanup(self):
        self._write_watchlist(ARTIST_A, ARTIST_B)
        self._cycle()

        self._write_watchlist(ARTIST_B)
        self.listing_errors[ARTIST_B] = requests.ConnectionError("site unreachable")
        self._cycle(retention_days=0)

        assert db.get_comic(self.db_path, ONLY_A) is not None
        assert db.get_comic(self.db_path, ONLY_A)["unlinked_since"] is None

    def test_cleanup_resumes_once_every_source_reads_again(self):
        self._write_watchlist(ARTIST_A, ARTIST_B)
        self._cycle()
        self._write_watchlist(ARTIST_B)
        self.listing_errors[ARTIST_B] = _http_error(503)
        self._cycle(retention_days=0)

        del self.listing_errors[ARTIST_B]
        self._cycle(retention_days=0)
        assert db.get_comic(self.db_path, ONLY_A) is None

    def test_an_artist_page_that_no_longer_exists_does_not_hold_back_cleanup(self):
        """Otherwise one dead entry on the watchlist would block cleanup forever."""
        self._write_watchlist(ARTIST_A, ARTIST_B)
        self._cycle()

        self._write_watchlist(ARTIST_B, DIRECT)
        self.listing_errors[ARTIST_B] = _http_error(404)
        self._cycle(retention_days=0)

        assert db.get_comic(self.db_path, ONLY_A) is None

    def test_a_missing_watchlist_never_retires_the_library(self):
        self._write_watchlist(ARTIST_A, ARTIST_B)
        self._cycle()

        os.remove(self.watchlist_path)
        assert self._cycle(retention_days=0) == set()

        for comic_url in (SHARED, ONLY_A, ONLY_B):
            comic = db.get_comic(self.db_path, comic_url)
            assert comic is not None
            assert comic["unlinked_since"] is None


class TestRetentionSetting:
    def test_defaults_to_seven_days(self):
        assert main._parse_retention_days({}) == 7

    def test_accepts_a_configured_value(self):
        assert main._parse_retention_days({"unwatched_retention_days": 14}) == 14

    def test_falls_back_on_invalid_values(self):
        assert main._parse_retention_days({"unwatched_retention_days": "a week"}) == 7

    def test_negative_values_are_clamped_to_zero(self):
        assert main._parse_retention_days({"unwatched_retention_days": -3}) == 0
