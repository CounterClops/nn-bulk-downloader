"""Tests for language-filtering logic."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from main import _is_language_allowed
from modules.multporn import _extract_language
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# _is_language_allowed
# ---------------------------------------------------------------------------

class TestIsLanguageAllowed:
    def test_empty_list_allows_all(self):
        assert _is_language_allowed("ru", []) is True
        assert _is_language_allowed("zh", []) is True
        assert _is_language_allowed("en", []) is True

    def test_unknown_language_allows_by_default(self):
        assert _is_language_allowed("", ["en"]) is True
        assert _is_language_allowed("", []) is True

    def test_exact_match(self):
        assert _is_language_allowed("en", ["en"]) is True
        assert _is_language_allowed("ru", ["en"]) is False

    def test_case_insensitive(self):
        assert _is_language_allowed("EN", ["en"]) is True
        assert _is_language_allowed("en", ["EN"]) is True
        assert _is_language_allowed("RU", ["en"]) is False

    def test_primary_subtag_match(self):
        # "en" config should match "en-US", "en-GB", "en-us", etc.
        assert _is_language_allowed("en-US", ["en"]) is True
        assert _is_language_allowed("en-us", ["en"]) is True
        assert _is_language_allowed("en-GB", ["en"]) is True
        assert _is_language_allowed("pt-BR", ["en"]) is False

    def test_primary_subtag_in_config(self):
        # Config can also use full BCP-47; primary subtag is compared
        assert _is_language_allowed("en", ["en-US"]) is True
        assert _is_language_allowed("en-GB", ["en-US"]) is True

    def test_multiple_allowed_languages(self):
        assert _is_language_allowed("en", ["en", "zh"]) is True
        assert _is_language_allowed("zh", ["en", "zh"]) is True
        assert _is_language_allowed("ru", ["en", "zh"]) is False

    def test_chinese_blocked_by_default(self):
        assert _is_language_allowed("zh", ["en"]) is False

    def test_russian_blocked_by_default(self):
        assert _is_language_allowed("ru", ["en"]) is False


# ---------------------------------------------------------------------------
# _extract_language
# ---------------------------------------------------------------------------

def _make_soup(content_value: str) -> BeautifulSoup:
    html = f'<html><head><meta name="dcterms.language" content="{content_value}"/></head></html>'
    return BeautifulSoup(html, "lxml")


class TestExtractLanguage:
    def test_returns_lowercase(self):
        soup = _make_soup("RU")
        assert _extract_language(soup) == "ru"

    def test_english(self):
        soup = _make_soup("en")
        assert _extract_language(soup) == "en"

    def test_russian(self):
        soup = _make_soup("ru")
        assert _extract_language(soup) == "ru"

    def test_bcp47_with_region_lowercased(self):
        soup = _make_soup("en-US")
        assert _extract_language(soup) == "en-us"

    def test_missing_meta_returns_empty(self):
        soup = BeautifulSoup("<html><head></head></html>", "lxml")
        assert _extract_language(soup) == ""

    def test_empty_content_returns_empty(self):
        soup = BeautifulSoup(
            '<html><head><meta name="dcterms.language" content=""/></head></html>',
            "lxml",
        )
        assert _extract_language(soup) == ""
