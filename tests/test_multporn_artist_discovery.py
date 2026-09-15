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
    listing = fetch_artist_comics(session, artist_url)

    assert "https://multporn.net/mp123456789" in listing.comic_urls
    assert "https://multporn.net/comics/example_comic" in listing.comic_urls
    assert listing.censored_urls == set()


# ---------------------------------------------------------------------------
# Censored-thumbnail reporting
# ---------------------------------------------------------------------------

# Mirrors the real markup: the thumbnail <img> sits inside the entry's <a>, and
# the site renders censored previews through the blur_comics image style.
LISTING_HTML = """
<html><body>
  <div class="views-field views-field-field-preview"><div class="field-content">
    <a href="/comics/clean_one">
      <img src="https://multporn.net/sites/default/files/styles/taxonomy_comics/public/clean.jpg">
    </a>
  </div></div>
  <div class="views-field views-field-field-preview"><div class="field-content">
    <a href="/comics/blurred_one">
      <img src="https://multporn.net/sites/default/files/styles/blur_comics/public/blurred.png">
    </a>
  </div></div>
</body></html>
"""


class TestCensoredReporting:
    def test_censored_entries_are_reported_not_dropped(self):
        """The caller records the verdict, so the comic must still be listed."""
        artist_url = "https://multporn.net/authors_comics/example"
        session = _FakeSession({artist_url: LISTING_HTML})

        listing = fetch_artist_comics(session, artist_url)

        assert "https://multporn.net/comics/blurred_one" in listing.comic_urls
        assert "https://multporn.net/comics/clean_one" in listing.comic_urls

    def test_only_blurred_entries_are_marked_censored(self):
        artist_url = "https://multporn.net/authors_comics/example"
        session = _FakeSession({artist_url: LISTING_HTML})

        listing = fetch_artist_comics(session, artist_url)

        assert listing.censored_urls == {"https://multporn.net/comics/blurred_one"}

    def test_censored_urls_are_a_subset_of_comic_urls(self):
        artist_url = "https://multporn.net/authors_comics/example"
        session = _FakeSession({artist_url: LISTING_HTML})

        listing = fetch_artist_comics(session, artist_url)

        assert listing.censored_urls <= set(listing.comic_urls)
