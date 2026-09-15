"""Tests for watchlist URL type detection."""

import pytest

from modules.multporn import detect_url_type
from modules.watchlist import load_watchlist


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://multporn.net/comics/example", "comic"),
        ("https://multporn.net/hentai_manga/example", "comic"),
        ("https://multporn.net/mp123456789", "comic"),
        ("https://multporn.net/authors_comics/example_artist", "artist"),
        ("https://multporn.net/authors_hentai_comics/example_artist", "artist"),
        ("https://multporn.net/user_content/artists/example_artist", "artist"),
    ],
)
def test_detect_url_type_supported_segments(url, expected):
    assert detect_url_type(url) == expected


def test_detect_url_type_rejects_unknown_segment():
    with pytest.raises(ValueError, match="Unrecognised URL segment"):
        detect_url_type("https://multporn.net/not_a_real_section/foo")


# ---------------------------------------------------------------------------
# URL canonicalisation on load
# ---------------------------------------------------------------------------

class TestWatchlistNormalisation:
    def _write(self, tmp_path, body):
        path = tmp_path / "watchlist.txt"
        path.write_text(body, encoding="utf-8")
        return str(path)

    def test_query_string_is_stripped(self, tmp_path):
        path = self._write(tmp_path, "https://multporn.net/comics/foo?r=1\n")
        items = load_watchlist(path)
        assert [i["url"] for i in items] == ["https://multporn.net/comics/foo"]

    def test_variants_collapse_to_one_entry(self, tmp_path):
        path = self._write(tmp_path, "\n".join([
            "https://multporn.net/comics/foo",
            "https://multporn.net/comics/foo?r=1",
            "https://multporn.net/comics/foo?rule34=2",
            "https://multporn.net/comics/foo/",
        ]) + "\n")
        items = load_watchlist(path)
        assert [i["url"] for i in items] == ["https://multporn.net/comics/foo"]

    def test_distinct_comics_are_kept(self, tmp_path):
        path = self._write(tmp_path, "\n".join([
            "https://multporn.net/comics/foo?r=1",
            "https://multporn.net/comics/bar?r=1",
        ]) + "\n")
        items = load_watchlist(path)
        assert [i["url"] for i in items] == [
            "https://multporn.net/comics/foo",
            "https://multporn.net/comics/bar",
        ]
