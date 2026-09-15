"""Tests for the user-tag blacklist.

User tags are community-editable, so they get their own config list rather than
sharing blacklisted_tags — the same word can be a deliberate filter in the
curated vocabulary and noise in the community one.
"""

import os
import tempfile
from unittest.mock import patch

from main import _process_comic
from modules import db

BASE_META = {
    "title": "Example User Tagged Comic",
    "author": "Example Author",
    "sections": ["Example Series", "Example Universe"],
    "characters": [],
    "tags": ["Curated Tag A", "Curated Tag B"],
    "user_tags": ["User Tag A", "Blocked User Tag", "User Tag C"],
    "language": "en",
    "image_urls": [],
    "page_count": 1,
    "node_id": "1",
}

URL = "https://multporn.net/comics/example_user_tagged_comic"


def _make_db() -> str:
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    db.init_db(handle.name)
    return handle.name


def _config(**overrides) -> dict:
    config = {
        "blacklisted_tags": [],
        "blacklisted_user_tags": [],
        "allowed_languages": ["en"],
        "exclude_censored": False,
        "output_dir": "/tmp",
    }
    config.update(overrides)
    return config


def _run(db_path: str, config: dict, meta: dict = None, is_direct_watch: bool = False):
    # Pre-seed so remote_count == local_count: a comic that passes the filters
    # returns without touching the filesystem.
    db.upsert_comic(db_path, URL)
    db.update_comic_checked(db_path, URL, 1, "[]", "absent.cbz", BASE_META["title"], "1")

    with patch("main.mp.fetch_comic_metadata", return_value=meta or BASE_META), \
         patch("main.sleep"):
        _process_comic(
            session=None, url=URL, config=config, db_path=db_path,
            full_check_interval_s=28 * 86400, is_direct_watch=is_direct_watch,
        )
    return db.get_comic(db_path, URL)


class TestUserTagBlacklist:
    def test_matching_user_tag_skips_the_comic(self):
        db_path = _make_db()
        try:
            row = _run(db_path, _config(blacklisted_user_tags=["Blocked User Tag"]))
            assert row["is_blacklisted"] == 1
            assert row["skip_reason"] == db.USER_TAG_SKIP_REASON
        finally:
            os.unlink(db_path)

    def test_match_is_case_insensitive(self):
        db_path = _make_db()
        try:
            row = _run(db_path, _config(blacklisted_user_tags=["blocked user tag"]))
            assert row["is_blacklisted"] == 1
        finally:
            os.unlink(db_path)

    def test_empty_list_is_the_default_and_skips_nothing(self):
        """An existing config with no blacklisted_user_tags key must behave as before."""
        db_path = _make_db()
        try:
            config = _config()
            del config["blacklisted_user_tags"]
            row = _run(db_path, config)
            assert row["is_blacklisted"] == 0
        finally:
            os.unlink(db_path)

    def test_curated_blacklist_does_not_match_user_tags(self):
        """'Blocked User Tag' here is a user tag, so the curated list must ignore it."""
        db_path = _make_db()
        try:
            row = _run(db_path, _config(blacklisted_tags=["Blocked User Tag"]))
            assert row["is_blacklisted"] == 0
        finally:
            os.unlink(db_path)

    def test_user_blacklist_does_not_match_curated_tags(self):
        db_path = _make_db()
        try:
            row = _run(db_path, _config(blacklisted_user_tags=["Curated Tag A"]))
            assert row["is_blacklisted"] == 0
        finally:
            os.unlink(db_path)

    def test_direct_watch_is_exempt(self):
        db_path = _make_db()
        try:
            row = _run(
                db_path, _config(blacklisted_user_tags=["Blocked User Tag"]),
                is_direct_watch=True,
            )
            assert row["is_blacklisted"] == 0
        finally:
            os.unlink(db_path)

    def test_comic_without_user_tags_is_unaffected(self):
        db_path = _make_db()
        try:
            meta = dict(BASE_META)
            del meta["user_tags"]
            row = _run(db_path, _config(blacklisted_user_tags=["Blocked User Tag"]), meta=meta)
            assert row["is_blacklisted"] == 0
        finally:
            os.unlink(db_path)


class TestDirectWatchClearsBothTagSkips:
    def test_a_user_tag_skip_is_lifted_when_the_comic_becomes_directly_watched(self):
        db_path = _make_db()
        try:
            db.upsert_comic(db_path, URL, is_blacklisted=1,
                            skip_reason=db.USER_TAG_SKIP_REASON)
            db.clear_tag_blacklist_for_urls(db_path, [URL])
            row = db.get_comic(db_path, URL)
            assert row["is_blacklisted"] == 0
            assert row["skip_reason"] is None
        finally:
            os.unlink(db_path)

    def test_a_curated_tag_skip_is_still_lifted(self):
        db_path = _make_db()
        try:
            db.upsert_comic(db_path, URL, is_blacklisted=1, skip_reason=db.TAG_SKIP_REASON)
            db.clear_tag_blacklist_for_urls(db_path, [URL])
            assert db.get_comic(db_path, URL)["is_blacklisted"] == 0
        finally:
            os.unlink(db_path)

    def test_censored_and_language_skips_are_untouched(self):
        for reason in (db.CENSORED_SKIP_REASON, "language"):
            db_path = _make_db()
            try:
                db.upsert_comic(db_path, URL, is_blacklisted=1, skip_reason=reason)
                db.clear_tag_blacklist_for_urls(db_path, [URL])
                row = db.get_comic(db_path, URL)
                assert row["is_blacklisted"] == 1, reason
                assert row["skip_reason"] == reason
            finally:
                os.unlink(db_path)
