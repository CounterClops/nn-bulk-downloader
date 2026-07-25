"""Tests for artist-page comic discovery in multporn module."""

from modules.multporn import fetch_artist_comics


class _FakeResponse:
    def __init__(self, text: str, url: str):
        self.text = text
        self.url = url

    def raise_for_status(self):
        return None


class _FakeSession:
    def __init__(self, html_by_url):
        self._html_by_url = html_by_url

    def get(self, url):
        return _FakeResponse(self._html_by_url[url], url)


def test_fetch_artist_comics_includes_mp_urls():
    artist_url = "https://multporn.net/user_content/artists/example_artist"
    html = """
    <html><body>
      <a href="/mp123456789">mp item</a>
      <a href="/comics/example_comic">classic item</a>
    </body></html>
    """

    session = _FakeSession({artist_url: html})
    comics = fetch_artist_comics(session, artist_url)

    assert "https://multporn.net/mp123456789" in comics
    assert "https://multporn.net/comics/example_comic" in comics
