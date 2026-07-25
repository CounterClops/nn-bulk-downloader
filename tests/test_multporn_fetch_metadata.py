"""Tests for fetch_comic_metadata — field_type selection and error paths."""

from unittest.mock import patch

import pytest

from modules.multporn import fetch_comic_metadata


# ---------------------------------------------------------------------------
# Minimal fake HTTP infrastructure
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, text="", content=b"", headers=None):
        self.text = text
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        pass


class _RecordingSession:
    """Session stub that returns pre-configured responses in order and records URLs."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, **_kwargs):
        self.calls.append(url)
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _html_with_shortlink(node_id: str, title: str = "Test Comic") -> str:
    return f"""<html><head>
      <link rel="shortlink" href="https://multporn.net/node/{node_id}"/>
      <title>{title} | multporn.net</title>
    </head><body>
      <h1 id="page-title">{title}</h1>
    </body></html>"""


def _juicebox_xml(*image_urls: str) -> bytes:
    images = "".join(f'<image linkURL="{u}"/>' for u in image_urls)
    return f'<?xml version="1.0"?><juicebox>{images}</juicebox>'.encode()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFetchComicMetadataMpUrl:
    """Verify that /mp<nodeid> URLs select field_files for the Juicebox query."""

    def test_mp_url_uses_field_files_in_juicebox_query(self):
        node_id = "99999"
        img_urls = [
            "https://cdn.example.com/page1.jpg",
            "https://cdn.example.com/page2.jpg",
        ]

        session = _RecordingSession(
            _FakeResponse(text=_html_with_shortlink(node_id)),
            _FakeResponse(content=_juicebox_xml(*img_urls)),
        )

        with patch("modules.multporn.sleep"):
            meta = fetch_comic_metadata(session, "https://multporn.net/mp99999")

        assert meta["image_urls"] == img_urls
        assert meta["page_count"] == 2
        assert meta["node_id"] == node_id

        juicebox_url = session.calls[1]
        assert "field_files" in juicebox_url, (
            f"Expected field_files in juicebox URL for /mp... comics, got: {juicebox_url}"
        )

    def test_mp_url_juicebox_url_contains_node_id(self):
        node_id = "12345"
        session = _RecordingSession(
            _FakeResponse(text=_html_with_shortlink(node_id)),
            _FakeResponse(content=_juicebox_xml("https://cdn.example.com/p1.jpg")),
        )

        with patch("modules.multporn.sleep"):
            fetch_comic_metadata(session, "https://multporn.net/mp12345")

        juicebox_url = session.calls[1]
        assert f"/node/{node_id}/" in juicebox_url


class TestFetchComicMetadataUnsupportedSection:
    def test_unsupported_section_raises_value_error(self):
        session = _RecordingSession()
        with pytest.raises(ValueError, match="Unsupported content section"):
            fetch_comic_metadata(session, "https://multporn.net/unknown_section/some-comic")

    def test_too_short_url_raises_value_error(self):
        session = _RecordingSession()
        with pytest.raises(ValueError, match="Cannot parse URL"):
            fetch_comic_metadata(session, "https://multporn.net")
