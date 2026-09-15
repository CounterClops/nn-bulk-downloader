"""Integration-style tests confirming _process_comic wires is_direct_watch
through to the tag blacklist check correctly."""

import os
import tempfile
from unittest.mock import patch

from main import _process_comic
from modules import db

FAKE_META = {
    "title": "Test Comic",
    "tags": ["Blacklisted Tag"],
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


class TestProcessComicBlacklistScope:
    def test_direct_watch_bypasses_tag_blacklist(self):
        db_path = _make_db()
        try:
            url = "https://multporn.net/comics/example_direct"
            # Pre-seed so remote_count == local_count -> needs_sync is False,
            # so _process_comic returns without touching the filesystem.
            db.upsert_comic(db_path, url)
            db.update_comic_checked(db_path, url, 1, "[]", "x.cbz", "Test Comic", "1")

            with patch("main.mp.fetch_comic_metadata", return_value=FAKE_META):
                _process_comic(
                    None, url, CONFIG, db_path, 28 * 86400,
                    is_direct_watch=True,
                )

            row = db.get_comic(db_path, url)
            assert row["is_blacklisted"] == 0
            assert row["skip_reason"] is None
        finally:
            os.unlink(db_path)

    def test_bulk_discovered_is_blacklisted(self):
        db_path = _make_db()
        try:
            url = "https://multporn.net/comics/example_bulk"

            with patch("main.mp.fetch_comic_metadata", return_value=FAKE_META):
                _process_comic(
                    None, url, CONFIG, db_path, 28 * 86400,
                    is_direct_watch=False,
                )

            row = db.get_comic(db_path, url)
            assert row["is_blacklisted"] == 1
            assert row["skip_reason"] == "tag"
        finally:
            os.unlink(db_path)

    def test_default_is_direct_watch_false_still_blacklists(self):
        db_path = _make_db()
        try:
            url = "https://multporn.net/comics/example_default"

            with patch("main.mp.fetch_comic_metadata", return_value=FAKE_META):
                _process_comic(None, url, CONFIG, db_path, 28 * 86400)

            row = db.get_comic(db_path, url)
            assert row["is_blacklisted"] == 1
            assert row["skip_reason"] == "tag"
        finally:
            os.unlink(db_path)
