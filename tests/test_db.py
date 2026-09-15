"""Tests for DB-level persistence of the last_synced column and failed_page tracking."""

import os
import tempfile
import time

import pytest

from modules import db


class TestLastSynced:
    def _make_db(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db.init_db(tmp.name)
        return tmp.name

    def test_last_synced_written_when_provided(self):
        db_path = self._make_db()
        try:
            url = "https://example.com/comics/test"
            db.upsert_comic(db_path, url)
            ts = time.time()
            db.update_comic_checked(
                db_path, url, 10, "[]", "test.cbz", "Test", "123",
                last_synced=ts,
            )
            row = db.get_comic(db_path, url)
            assert row["last_synced"] == pytest.approx(ts, abs=1.0)
        finally:
            os.unlink(db_path)

    def test_last_synced_unchanged_when_none(self):
        db_path = self._make_db()
        try:
            url = "https://example.com/comics/test2"
            db.upsert_comic(db_path, url)
            ts = time.time() - 100
            # First call sets last_synced
            db.update_comic_checked(
                db_path, url, 5, "[]", "test2.cbz", "Test2", "456",
                last_synced=ts,
            )
            # Second call with last_synced=None must NOT overwrite the stored value
            db.update_comic_checked(
                db_path, url, 5, "[]", "test2.cbz", "Test2", "456",
                last_synced=None,
            )
            row = db.get_comic(db_path, url)
            assert row["last_synced"] == pytest.approx(ts, abs=1.0)
        finally:
            os.unlink(db_path)

    def test_last_synced_null_initially(self):
        db_path = self._make_db()
        try:
            url = "https://example.com/comics/test3"
            db.upsert_comic(db_path, url)
            # update_comic_checked without last_synced — should remain NULL
            db.update_comic_checked(
                db_path, url, 0, "[]", "test3.cbz", "Test3", "789",
            )
            row = db.get_comic(db_path, url)
            assert row["last_synced"] is None
        finally:
            os.unlink(db_path)

    def test_last_synced_can_be_updated(self):
        db_path = self._make_db()
        try:
            url = "https://example.com/comics/test4"
            db.upsert_comic(db_path, url)
            ts1 = time.time() - 200
            ts2 = time.time()
            db.update_comic_checked(
                db_path, url, 3, "[]", "test4.cbz", "Test4", "000",
                last_synced=ts1,
            )
            db.update_comic_checked(
                db_path, url, 3, "[]", "test4.cbz", "Test4", "000",
                last_synced=ts2,
            )
            row = db.get_comic(db_path, url)
            assert row["last_synced"] == pytest.approx(ts2, abs=1.0)
        finally:
            os.unlink(db_path)


class TestFailedPage:
    def _make_db(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db.init_db(tmp.name)
        return tmp.name

    def test_failed_page_initially_none(self):
        db_path = self._make_db()
        try:
            url = "https://example.com/comics/fp1"
            db.upsert_comic(db_path, url)
            row = db.get_comic(db_path, url)
            assert row["failed_page"] is None
        finally:
            os.unlink(db_path)

    def test_set_failed_page(self):
        db_path = self._make_db()
        try:
            url = "https://example.com/comics/fp2"
            db.upsert_comic(db_path, url)
            db.set_comic_failed_page(db_path, url, 42)
            row = db.get_comic(db_path, url)
            assert row["failed_page"] == 42
        finally:
            os.unlink(db_path)

    def test_clear_failed_page(self):
        db_path = self._make_db()
        try:
            url = "https://example.com/comics/fp3"
            db.upsert_comic(db_path, url)
            db.set_comic_failed_page(db_path, url, 7)
            db.set_comic_failed_page(db_path, url, None)
            row = db.get_comic(db_path, url)
            assert row["failed_page"] is None
        finally:
            os.unlink(db_path)


class TestClearTagBlacklistForUrls:
    def _make_db(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db.init_db(tmp.name)
        return tmp.name

    def test_clears_matching_tag_entries_in_list(self):
        db_path = self._make_db()
        try:
            url = "https://example.com/comics/tag1"
            db.upsert_comic(db_path, url, is_blacklisted=1, skip_reason="tag")

            db.clear_tag_blacklist_for_urls(db_path, [url])

            row = db.get_comic(db_path, url)
            assert row["is_blacklisted"] == 0
            assert row["skip_reason"] is None
        finally:
            os.unlink(db_path)

    def test_does_not_clear_tag_entries_not_in_list(self):
        db_path = self._make_db()
        try:
            url = "https://example.com/comics/tag2"
            db.upsert_comic(db_path, url, is_blacklisted=1, skip_reason="tag")

            db.clear_tag_blacklist_for_urls(db_path, ["https://example.com/comics/other"])

            row = db.get_comic(db_path, url)
            assert row["is_blacklisted"] == 1
            assert row["skip_reason"] == "tag"
        finally:
            os.unlink(db_path)

    def test_does_not_clear_other_reasons_even_if_url_in_list(self):
        db_path = self._make_db()
        try:
            censored_url = "https://example.com/comics/censored1"
            language_url = "https://example.com/comics/language1"
            db.upsert_comic(db_path, censored_url, is_blacklisted=1, skip_reason="censored")
            db.upsert_comic(db_path, language_url, is_blacklisted=1, skip_reason="language")

            db.clear_tag_blacklist_for_urls(db_path, [censored_url, language_url])

            row_censored = db.get_comic(db_path, censored_url)
            assert row_censored["is_blacklisted"] == 1
            assert row_censored["skip_reason"] == "censored"

            row_language = db.get_comic(db_path, language_url)
            assert row_language["is_blacklisted"] == 1
            assert row_language["skip_reason"] == "language"
        finally:
            os.unlink(db_path)

    def test_empty_urls_list_is_no_op(self):
        db_path = self._make_db()
        try:
            url = "https://example.com/comics/tag3"
            db.upsert_comic(db_path, url, is_blacklisted=1, skip_reason="tag")

            db.clear_tag_blacklist_for_urls(db_path, [])

            row = db.get_comic(db_path, url)
            assert row["is_blacklisted"] == 1
            assert row["skip_reason"] == "tag"
        finally:
            os.unlink(db_path)

    def test_no_op_when_no_matching_entries(self):
        db_path = self._make_db()
        try:
            url = "https://example.com/comics/tag4"
            db.upsert_comic(db_path, url)

            db.clear_tag_blacklist_for_urls(db_path, [url])

            row = db.get_comic(db_path, url)
            assert row["is_blacklisted"] == 0
        finally:
            os.unlink(db_path)
