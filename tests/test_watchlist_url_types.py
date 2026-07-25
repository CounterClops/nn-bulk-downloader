"""Tests for watchlist URL type detection."""

import pytest

from modules.multporn import detect_url_type


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
