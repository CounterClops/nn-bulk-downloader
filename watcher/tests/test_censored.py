"""Tests for censored-content detection and config re-evaluation logic."""

import sys
import os
import sqlite3
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from main import _is_censored
from modules import db
from modules.multporn import CENSORED_TAGS
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# _is_censored
# ---------------------------------------------------------------------------

class TestIsCensored:
    def test_mini_girl_tag(self):
        assert _is_censored(["Anal", "Mini Girl", "Oral"]) is True

    def test_mini_male_tag(self):
        assert _is_censored(["Rape", "Mini Male"]) is True

    def test_both_tags(self):
        assert _is_censored(["Mini Girl", "Mini Male"]) is True

    def test_no_censored_tags(self):
        assert _is_censored(["Anal", "Oral", "Lesbians"]) is False

    def test_empty_tags(self):
        assert _is_censored([]) is False

    def test_case_insensitive_lower(self):
        assert _is_censored(["mini girl"]) is True

    def test_case_insensitive_upper(self):
        assert _is_censored(["MINI GIRL", "MINI MALE"]) is True

    def test_partial_match_not_triggered(self):
        # "mini" alone should not match
        assert _is_censored(["mini"]) is False
        assert _is_censored(["girl"]) is False


# ---------------------------------------------------------------------------
# CENSORED_TAGS constant
# ---------------------------------------------------------------------------

class TestCensoredTagsConstant:
    def test_contains_expected_tags(self):
        assert "mini girl" in CENSORED_TAGS
        assert "mini male" in CENSORED_TAGS

    def test_is_frozenset(self):
        assert isinstance(CENSORED_TAGS, frozenset)


# ---------------------------------------------------------------------------
# db.clear_blacklisted_by_reason
# ---------------------------------------------------------------------------

class TestClearBlacklistedByReason:
    def _make_db(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db.init_db(tmp.name)
        return tmp.name

    def test_clears_censored_entries(self):
        db_path = self._make_db()
        try:
            db.upsert_comic(db_path, "https://example.com/comics/a",
                            is_blacklisted=1, skip_reason="censored")
            db.upsert_comic(db_path, "https://example.com/comics/b",
                            is_blacklisted=1, skip_reason="censored")

            db.clear_blacklisted_by_reason(db_path, "censored")

            for url in ["https://example.com/comics/a", "https://example.com/comics/b"]:
                row = db.get_comic(db_path, url)
                assert row["is_blacklisted"] == 0
                assert row["skip_reason"] is None
        finally:
            os.unlink(db_path)

    def test_does_not_clear_other_reasons(self):
        db_path = self._make_db()
        try:
            db.upsert_comic(db_path, "https://example.com/comics/c",
                            is_blacklisted=1, skip_reason="tag")
            db.upsert_comic(db_path, "https://example.com/comics/d",
                            is_blacklisted=1, skip_reason="language")

            db.clear_blacklisted_by_reason(db_path, "censored")

            row_c = db.get_comic(db_path, "https://example.com/comics/c")
            assert row_c["is_blacklisted"] == 1
            assert row_c["skip_reason"] == "tag"

            row_d = db.get_comic(db_path, "https://example.com/comics/d")
            assert row_d["is_blacklisted"] == 1
            assert row_d["skip_reason"] == "language"
        finally:
            os.unlink(db_path)

    def test_no_op_when_no_matching_entries(self):
        db_path = self._make_db()
        try:
            db.upsert_comic(db_path, "https://example.com/comics/e")
            db.clear_blacklisted_by_reason(db_path, "censored")
            row = db.get_comic(db_path, "https://example.com/comics/e")
            assert row["is_blacklisted"] == 0
        finally:
            os.unlink(db_path)
