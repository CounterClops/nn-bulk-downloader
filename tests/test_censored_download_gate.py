"""Tests for confirming a comic's censored verdict before downloading it.

A verdict comes only from reading an artist listing, while each comic falls due
on its own schedule. Without this gate a comic could be downloaded while its
artist's verdict was weeks old or had never been taken — which is how comics
blurred on their artist's page came to be downloaded.
"""

import os
import tempfile
import zipfile
from unittest.mock import patch

import pytest
import requests

import main
from modules import cbz_manager as cbz
from modules import db
from modules.multporn import ArtistListing

ARTIST = "https://multporn.net/authors_comics/example_blurring_artist"
OTHER_ARTIST = "https://multporn.net/authors_comics/example_other_artist"
BLURRED = "https://multporn.net/comics/example_blurred_comic"
CLEAR = "https://multporn.net/comics/example_clear_comic"
DIRECT = "https://multporn.net/comics/example_direct_only"


def _meta(url: str) -> dict:
    return {
        "node_id": "1",
        "title": url.rsplit("/", 1)[-1],
        "author": "Example Author",
        "tags": ["Curated Tag A"],
        "language": "en",
        "image_urls": [f"{url}/1.png", f"{url}/2.png"],
        "page_count": 2,
    }


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"HTTP {status}", response=response)


class _GateTest:
    def setup_method(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.directory.name, "watcher.db")
        self.watchlist_path = os.path.join(self.directory.name, "watchlist.txt")
        self.output_dir = os.path.join(self.directory.name, "media")
        db.init_db(self.db_path)

        self.listings = {ARTIST: ArtistListing(comic_urls=[BLURRED, CLEAR], censored_urls={BLURRED})}
        self.listing_errors = {}
        self.listing_fetches = []
        self.downloaded_urls = []

    def teardown_method(self):
        self.directory.cleanup()

    def _config(self, exclude_censored: bool = True) -> dict:
        return {
            "output_dir": self.output_dir,
            "watchlist_file": self.watchlist_path,
            "full_check_interval_days": 28,
            "check_updated_feed": False,
            "exclude_censored": exclude_censored,
            "blacklisted_tags": [],
            "allowed_languages": ["en"],
            "watched_items": [],
        }

    def _watch(self, *urls: str):
        with open(self.watchlist_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(urls) + "\n")

    def _stale_verdict(self, *comic_urls: str, artist: str = ARTIST):
        """Comics linked to an artist whose page was read before they were blurred:
        the artist is not due, the comics are, and no verdict is recorded."""
        for comic_url in comic_urls:
            db.upsert_comic(self.db_path, comic_url)
        db.link_comics_to_source(self.db_path, artist, list(comic_urls))
        db.upsert_artist_checked(self.db_path, artist)

    def _fetch_listing(self, _session, url):
        self.listing_fetches.append(url)
        if url in self.listing_errors:
            raise self.listing_errors[url]
        return self.listings[url]

    def _download(self, _session, url):
        self.downloaded_urls.append(url)
        return b"\x89PNG\r\n\x1a\n" + url.encode()

    def _cycle(self, exclude_censored: bool = True):
        with patch.object(main.mp, "fetch_artist_comics", side_effect=self._fetch_listing), \
             patch.object(main.mp, "fetch_comic_metadata", side_effect=lambda _s, url: _meta(url)), \
             patch.object(main.mp, "download_image", side_effect=self._download), \
             patch.object(main, "sleep"):
            main.run_once(self._config(exclude_censored), self.db_path)

    def _downloaded(self, comic_url: str) -> bool:
        return any(url.startswith(comic_url + "/") for url in self.downloaded_urls)


class TestStaleVerdicts(_GateTest):
    def test_a_comic_blurred_since_its_artist_was_last_read_is_not_downloaded(self):
        self._watch(ARTIST)
        self._stale_verdict(BLURRED)
        self._cycle()
        assert not self._downloaded(BLURRED)

    def test_the_fresh_verdict_is_recorded(self):
        self._watch(ARTIST)
        self._stale_verdict(BLURRED)
        self._cycle()
        comic = db.get_comic(self.db_path, BLURRED)
        assert comic["skip_reason"] == db.CENSORED_SKIP_REASON
        assert comic["is_blacklisted"] == 1

    def test_a_comic_the_listing_does_not_blur_is_downloaded(self):
        self._watch(ARTIST)
        self._stale_verdict(CLEAR)
        self._cycle()
        assert self._downloaded(CLEAR)

    def test_each_stale_artist_is_read_once_however_many_comics_need_it(self):
        self._watch(ARTIST)
        self._stale_verdict(BLURRED, CLEAR)
        self._cycle()
        assert self.listing_fetches.count(ARTIST) == 1
        assert self._downloaded(CLEAR)
        assert not self._downloaded(BLURRED)

    def test_every_watched_artist_linking_the_comic_is_consulted(self):
        self.listings[OTHER_ARTIST] = ArtistListing(comic_urls=[BLURRED], censored_urls={BLURRED})
        self.listings[ARTIST] = ArtistListing(comic_urls=[BLURRED], censored_urls=set())
        self._watch(ARTIST, OTHER_ARTIST)
        self._stale_verdict(BLURRED, artist=ARTIST)
        self._stale_verdict(BLURRED, artist=OTHER_ARTIST)
        self._cycle()
        assert set(self.listing_fetches) == {ARTIST, OTHER_ARTIST}

    @pytest.mark.parametrize("blurring_artist", [ARTIST, OTHER_ARTIST])
    def test_a_blur_on_any_watched_artists_page_wins_whichever_is_read_last(self, blurring_artist):
        """Artist pages are read in URL order; the verdict must not depend on it."""
        for artist in (ARTIST, OTHER_ARTIST):
            blurred = {BLURRED} if artist == blurring_artist else set()
            self.listings[artist] = ArtistListing(comic_urls=[BLURRED], censored_urls=blurred)
            self._stale_verdict(BLURRED, artist=artist)
        self._watch(ARTIST, OTHER_ARTIST)
        self._cycle()
        assert not self._downloaded(BLURRED)
        assert db.get_comic(self.db_path, BLURRED)["skip_reason"] == db.CENSORED_SKIP_REASON

    @pytest.mark.parametrize("blurring_artist", [ARTIST, OTHER_ARTIST])
    def test_a_blur_on_any_page_holds_when_both_artists_are_due_together(self, blurring_artist):
        """The same ordering rule applies to artists read at the start of a cycle."""
        for artist in (ARTIST, OTHER_ARTIST):
            blurred = {BLURRED} if artist == blurring_artist else set()
            self.listings[artist] = ArtistListing(comic_urls=[BLURRED], censored_urls=blurred)
        self._watch(ARTIST, OTHER_ARTIST)
        self._cycle()
        assert not self._downloaded(BLURRED)
        assert db.get_comic(self.db_path, BLURRED)["skip_reason"] == db.CENSORED_SKIP_REASON


class TestUnconfirmableVerdicts(_GateTest):
    def test_an_unreadable_artist_page_blocks_the_download(self):
        self._watch(ARTIST)
        self._stale_verdict(CLEAR)
        self.listing_errors[ARTIST] = requests.ConnectionError("site unreachable")
        self._cycle()
        assert not self._downloaded(CLEAR)

    def test_a_blocked_comic_stays_due_for_the_next_cycle(self):
        self._watch(ARTIST)
        self._stale_verdict(CLEAR)
        self.listing_errors[ARTIST] = _http_error(503)
        self._cycle()
        assert db.get_comic(self.db_path, CLEAR)["last_checked"] is None

        del self.listing_errors[ARTIST]
        self._cycle()
        assert self._downloaded(CLEAR)

    def test_a_blocked_comic_is_not_recorded_as_censored(self):
        self._watch(ARTIST)
        self._stale_verdict(CLEAR)
        self.listing_errors[ARTIST] = requests.ConnectionError("site unreachable")
        self._cycle()
        assert db.get_comic(self.db_path, CLEAR)["skip_reason"] is None

    def test_an_artist_page_that_no_longer_exists_blocks_the_download(self):
        self._watch(ARTIST)
        self._stale_verdict(CLEAR)
        self.listing_errors[ARTIST] = _http_error(404)
        self._cycle()
        assert not self._downloaded(CLEAR)


class TestWhenNoVerdictIsNeeded(_GateTest):
    def test_nothing_is_fetched_when_censored_comics_are_not_excluded(self):
        self._watch(ARTIST)
        self._stale_verdict(BLURRED)
        self._cycle(exclude_censored=False)
        assert self.listing_fetches == []
        assert self._downloaded(BLURRED)

    def test_a_directly_watched_comic_no_artist_lists_downloads_without_a_fetch(self):
        self._watch(DIRECT)
        self._cycle()
        assert self.listing_fetches == []
        assert self._downloaded(DIRECT)

    def test_an_artist_no_longer_watched_is_not_consulted(self):
        self._watch(DIRECT)
        self._stale_verdict(DIRECT, artist=ARTIST)
        self._cycle()
        assert ARTIST not in self.listing_fetches
        assert self._downloaded(DIRECT)

    def test_a_metadata_only_check_does_not_fetch_the_listing(self):
        """Confirming a verdict costs a request; only a download justifies it."""
        self._watch(ARTIST)
        self._stale_verdict(CLEAR)
        db.update_comic_checked(self.db_path, CLEAR, 2, "[]", "clear.cbz", "Clear", "1")
        assert db.get_comic(self.db_path, CLEAR)["page_count"] == 2
        db.upsert_comic(self.db_path, CLEAR, last_checked=0)

        self._cycle()
        assert self.listing_fetches == []
        assert self.downloaded_urls == []


class TestInterruptedMetadataRewrite:
    def test_a_keyboard_interrupt_mid_rewrite_restores_the_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            cbz_path = os.path.join(directory, "comic.cbz")
            page_path = os.path.join(directory, "0001.png")
            with open(page_path, "wb") as handle:
                handle.write(b"\x89PNG\r\n\x1a\npage")
            cbz.create_cbz(cbz_path, [("0001.png", page_path)], {"title": "Before"})
            with open(cbz_path, "rb") as handle:
                before = handle.read()

            with patch.object(cbz, "_verify_comic_info", side_effect=KeyboardInterrupt):
                with pytest.raises(KeyboardInterrupt):
                    cbz.update_comic_info(cbz_path, {"title": "After", "tags": ["Changed"]})

            with open(cbz_path, "rb") as handle:
                assert handle.read() == before
            with zipfile.ZipFile(cbz_path) as archive:
                assert archive.testzip() is None
