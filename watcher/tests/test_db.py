"""Tests for DB-level persistence of the last_synced column."""

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
