import re
import time
from time import sleep
from typing import Dict, List, NamedTuple, Optional, Sequence, Set
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
import xmltodict
from bs4 import BeautifulSoup

from modules import logger

BASE_URL = "https://multporn.net"

# Maps URL path segment → Juicebox field type
CONTENT_TYPES = {
    "comics":           "field_com_pages",
    "hentai_manga":     "field_com_pages",
    "gay_porn_comics":  "field_com_pages",
    "gif":              "field_com_pages",
    "humor":            "field_com_pages",
    "pictures":         "field_img",
    "hentai":           "field_img",
    "rule_63":          "field_rule_63_img",
    "games":            "field_screenshots",
}

COMIC_PATH_SEGMENTS = set(CONTENT_TYPES.keys())
ARTIST_PATH_SEGMENTS = {
    "comic_author",
    "authors_comics",
    "authors_hentai_comics",
}
MP_COMIC_SEGMENT_RE = re.compile(r"^mp\d+$")

# Drupal renders each taxonomy field in a div carrying its machine name, and the
# two kinds of comic page name the same fields differently: site comics label
# the artist "Author:" in field-author, while user content (/mp<nodeid>) labels
# it "Artist:" in field-artist-term. Each logical field therefore lists every
# machine name it is known to appear under, and a page missing all of them
# simply yields no terms.
AUTHOR_FIELD_CLASSES = ("field-name-field-author", "field-name-field-artist-term")
SECTION_FIELD_CLASSES = ("field-name-field-com-group", "field-name-field-section-term")
CHARACTER_FIELD_CLASSES = ("field-name-field-characters",)
TAG_FIELD_CLASSES = ("field-name-field-category",)
USER_TAG_FIELD_CLASSES = ("field-name-field-user-tags",)


class ArtistListing(NamedTuple):
    """What an artist page currently shows.

    *censored_urls* is a subset of *comic_urls*: the site marks censored entries
    by rendering their preview thumbnail through the ``blur_comics`` image
    style. That marker exists only on listing pages — a comic's own page carries
    no trace of it — so an artist listing is the only place the verdict can be
    observed, and it is reported rather than acted on here.
    """
    comic_urls: List[str]
    censored_urls: Set[str]


# ---------------------------------------------------------------------------
# URL utilities
# ---------------------------------------------------------------------------

def detect_url_type(url: str) -> str:
    """Return 'comic' or 'artist', or raise ValueError if unrecognised."""
    parts = normalise_url(url).split("/")
    if len(parts) < 4:
        raise ValueError(f"Cannot determine type for URL: {url}")
    segment = parts[3]

    # User content artist pages are nested under /user_content/artists/<name>.
    if segment == "user_content" and len(parts) >= 6 and parts[4] == "artists":
        return "artist"

    if segment in COMIC_PATH_SEGMENTS or MP_COMIC_SEGMENT_RE.match(segment):
        return "comic"
    if segment in ARTIST_PATH_SEGMENTS:
        return "artist"
    raise ValueError(f"Unrecognised URL segment '{segment}' in: {url}")


def normalise_url(url: str) -> str:
    """Return a site URL in canonical form: no query string, no fragment, no
    trailing slash.

    Listings link to the same comic under assorted view parameters — ``?r=1``,
    ``?rule34=2`` — which select a presentation rather than distinct content.
    Collapsing them is what keeps one database row per comic. Works on relative
    hrefs as well as absolute URLs.
    """
    split = urlsplit(url)
    return urlunsplit((split.scheme, split.netloc, split.path.rstrip("/"), "", ""))


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _extract_node_id(resp: requests.Response) -> Optional[str]:
    """Pull Drupal node ID from a Link response header or shortlink tag."""
    link_header = resp.headers.get("link", "")
    if link_header:
        m = re.search(r"node/(\d+)", link_header)
        if m:
            return m.group(1)

    m = re.search(
        r'<link\s+rel=["\']shortlink["\']\s+href=["\']([^"\']+)["\']',
        resp.text,
    )
    if m:
        id_m = re.search(r"node/(\d+)", m.group(1))
        if id_m:
            return id_m.group(1)

    return None


def _extract_title(soup: BeautifulSoup) -> Optional[str]:
    h1 = soup.find("h1", id="page-title")
    if h1:
        return h1.get_text(strip=True)
    title_tag = soup.find("title")
    if title_tag:
        return title_tag.get_text(strip=True).split("|")[0].strip()
    return None


def _extract_taxonomy_terms(
    soup: BeautifulSoup, field_classes: Sequence[str]
) -> List[str]:
    """Return the taxonomy term names linked from a field's div, if the page has one.

    *field_classes* lists the machine names the field is known to appear under;
    all of them are read, since a page may use either spelling. Order is the
    page's own and duplicates are dropped — a term occasionally appears twice
    when the site links both a term and its alias. A page carrying none of these
    fields yields an empty list rather than an error: most optional fields are
    absent on most comics.
    """
    terms: List[str] = []
    seen: Set[str] = set()

    for field_class in field_classes:
        for div in soup.find_all("div", class_=field_class):
            for anchor in div.find_all("a"):
                text = anchor.get_text(strip=True)
                if text and text not in seen:
                    terms.append(text)
                    seen.add(text)

    return terms


def _extract_author(soup: BeautifulSoup) -> str:
    """Return the comic's author.

    The ``Author:`` field div is authoritative where the page has one. User
    content pages (``/mp<nodeid>``) omit it, so those fall back to scanning for
    the first link that points at an artist page.
    """
    field_authors = _extract_taxonomy_terms(soup, AUTHOR_FIELD_CLASSES)
    if field_authors:
        return field_authors[0]

    artist_tokens = tuple(f"/{segment}/" for segment in ARTIST_PATH_SEGMENTS)
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "/user_content/artists/" in href or any(token in href for token in artist_tokens):
            name = anchor.get_text(strip=True)
            if name:
                return name
    return ""


def _extract_language(soup: BeautifulSoup) -> str:
    """Return the BCP-47 language code from the dcterms.language meta tag (e.g. 'en', 'ru', 'en-US').

    The original casing from the page is preserved. All comparisons against
    this value should be done case-insensitively.
    """
    tag = soup.find("meta", attrs={"name": "dcterms.language"})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_comic_metadata(session: requests.Session, url: str) -> Dict:
    """
    Return a dict with keys:
      node_id, title, author, sections, characters, tags, user_tags,
      language, image_urls, page_count

    *sections* is the site's "Section:" field — the series or franchise a comic
    belongs to, which is frequently multi-valued. *tags* is the curated "Tags:"
    field and *user_tags* the community-editable "User tags:" field; they are
    kept apart because the two vocabularies differ in both quality and intent.

    Every field but the title and pages is optional, and each is reported as an
    empty list when the page does not carry it.
    """
    parts = normalise_url(url).split("/")
    if len(parts) < 4:
        raise ValueError(f"Cannot parse URL: {url}")
    section = parts[3]
    field_type = CONTENT_TYPES.get(section)
    if field_type is None and MP_COMIC_SEGMENT_RE.match(section):
        # User-content comic pages use /mp<nodeid> and expose pages via field_files.
        field_type = "field_files"
    if field_type is None:
        raise ValueError(f"Unsupported content section '{section}' in: {url}")

    resp = session.get(url)
    resp.raise_for_status()

    node_id = _extract_node_id(resp)
    if not node_id:
        raise ValueError(f"Could not extract Drupal node ID from: {url}")

    soup = BeautifulSoup(resp.text, "lxml")
    title = _extract_title(soup) or parts[-1].replace("-", " ").replace("_", " ")
    tags = _extract_taxonomy_terms(soup, TAG_FIELD_CLASSES)
    user_tags = _extract_taxonomy_terms(soup, USER_TAG_FIELD_CLASSES)
    sections = _extract_taxonomy_terms(soup, SECTION_FIELD_CLASSES)
    characters = _extract_taxonomy_terms(soup, CHARACTER_FIELD_CLASSES)
    author = _extract_author(soup)
    language = _extract_language(soup)

    sleep(1)  # polite pause between page fetch and XML fetch

    juicebox_url = (
        f"{BASE_URL}/juicebox/xml/field/node/{node_id}/{field_type}/full"
    )
    jresp = session.get(juicebox_url)
    jresp.raise_for_status()

    data = xmltodict.parse(jresp.content)
    images_raw = data.get("juicebox", {}).get("image") or []
    if isinstance(images_raw, dict):
        images_raw = [images_raw]

    image_urls = [img["@linkURL"] for img in images_raw if "@linkURL" in img]

    return {
        "node_id": node_id,
        "title": title,
        "author": author,
        "sections": sections,
        "characters": characters,
        "tags": tags,
        "user_tags": user_tags,
        "language": language,
        "image_urls": image_urls,
        "page_count": len(image_urls),
    }


def fetch_artist_comics(session: requests.Session, url: str) -> ArtistListing:
    """Return every comic an artist page lists, following Drupal pagination.

    Drupal uses non-sequential page tokens (e.g. ``?page=0%2C1``) rather than
    simple integers, so we follow the ``pager-next`` link href directly instead
    of constructing page numbers manually.

    Censored entries are reported in the result's *censored_urls* rather than
    dropped, so the caller can record the verdict and re-derive it on the next
    refresh instead of silently losing track of the comic.
    """
    comic_urls: List[str] = []
    censored_urls: Set[str] = set()
    seen: set = set()
    next_url: Optional[str] = url

    while next_url:
        resp = session.get(next_url)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")

        # Collect the URLs the site has marked censored on this page: their
        # preview thumbnail is rendered through the blur_comics image style.
        for img in soup.find_all("img", src=True):
            if "blur_comics" not in img["src"]:
                continue
            thumbnail_link = img.find_parent("a", href=True)
            if not thumbnail_link:
                continue
            href = thumbnail_link["href"]
            if href.startswith("/"):
                href = BASE_URL + href
            censored_urls.add(normalise_url(href))

        found_on_page = 0

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/"):
                href = BASE_URL + href
            if not href.startswith(BASE_URL):
                continue

            parts = normalise_url(href).split("/")
            segment = parts[3] if len(parts) >= 4 else ""
            is_classic_comic = len(parts) >= 5 and segment in COMIC_PATH_SEGMENTS
            is_mp_comic = len(parts) >= 4 and bool(MP_COMIC_SEGMENT_RE.match(segment))

            if is_classic_comic or is_mp_comic:
                norm = normalise_url(href)
                if norm not in seen:
                    comic_urls.append(norm)
                    seen.add(norm)
                    found_on_page += 1

        if found_on_page == 0:
            break

        next_li = soup.find("li", class_="pager-next")
        if not next_li:
            break
        next_a = next_li.find("a", href=True)
        if not next_a:
            break

        next_href = next_a["href"]
        next_url = urljoin(resp.url, next_href)
        sleep(2)

    return ArtistListing(comic_urls=comic_urls, censored_urls=censored_urls)


def fetch_updated_feed(
    session: requests.Session, max_pages: int = 2
) -> Set[str]:
    """
    Scrape /updated_comics (and subsequent pages) to get recently updated
    comic URLs.  Returns a set of normalised URLs.
    """
    updated: Set[str] = set()

    for page in range(max_pages):
        page_url = (
            f"{BASE_URL}/updated_comics?page={page}" if page > 0
            else f"{BASE_URL}/updated_comics"
        )
        resp = session.get(page_url)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/"):
                href = BASE_URL + href
            if not href.startswith(BASE_URL):
                continue

            parts = normalise_url(href).split("/")
            if len(parts) >= 5 and parts[3] in COMIC_PATH_SEGMENTS:
                updated.add(normalise_url(href))

        sleep(2)

    return updated


MAX_DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_DELAY = 5   # seconds between retries
DOWNLOAD_TIMEOUT = (10, 30)  # (connect timeout, read timeout) in seconds


def download_image(session: requests.Session, url: str) -> bytes:
    """Download a single image, retrying on transient errors.

    Retries up to MAX_DOWNLOAD_RETRIES times on network errors and HTTP 5xx
    responses, waiting DOWNLOAD_RETRY_DELAY seconds between attempts.
    HTTP 4xx responses are considered permanent failures and are raised
    immediately without retrying.
    """
    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
        try:
            resp = session.get(url, timeout=DOWNLOAD_TIMEOUT)
            resp.raise_for_status()
            return resp.content
        except requests.HTTPError as exc:
            last_exc = exc
            status = exc.response.status_code if exc.response is not None else 0
            if 400 <= status < 500:
                raise  # permanent — propagate immediately
            if attempt < MAX_DOWNLOAD_RETRIES:
                logger.warning(
                    f"    HTTP {status} downloading {url} "
                    f"(attempt {attempt}/{MAX_DOWNLOAD_RETRIES}) — retrying in {DOWNLOAD_RETRY_DELAY}s"
                )
                sleep(DOWNLOAD_RETRY_DELAY)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < MAX_DOWNLOAD_RETRIES:
                logger.warning(
                    f"    Network error downloading {url} "
                    f"(attempt {attempt}/{MAX_DOWNLOAD_RETRIES}) — retrying in {DOWNLOAD_RETRY_DELAY}s: {exc}"
                )
                sleep(DOWNLOAD_RETRY_DELAY)
    raise last_exc


def get_image_extension(url: str) -> str:
    path = url.split("?")[0]
    ext = path.rpartition(".")[-1].lower()
    return ext if ext in ("jpg", "jpeg", "png", "gif", "webp") else "jpg"
