"""Tests for reading the site's taxonomy fields.

Drupal renders each taxonomy field in a div named after its machine name, and
the two kinds of comic page spell some of those names differently, so these
fixtures mirror the markup both variants carry.
"""

from bs4 import BeautifulSoup

from modules.multporn import (
    AUTHOR_FIELD_CLASSES,
    CHARACTER_FIELD_CLASSES,
    SECTION_FIELD_CLASSES,
    TAG_FIELD_CLASSES,
    USER_TAG_FIELD_CLASSES,
    _extract_author,
    _extract_taxonomy_terms,
)


def _field(field_class: str, label: str, *terms: str) -> str:
    links = "".join(f'<a href="/category/{t.lower()}">{t}</a>' for t in terms)
    return (
        f'<div class="field {field_class} field-type-taxonomy-term-reference '
        f'field-label-inline clearfix">'
        f'<div class="field-label">{label}:&nbsp;</div>'
        f'<div class="field-items">{links}</div></div>'
    )


def _page(*fields: str) -> BeautifulSoup:
    return BeautifulSoup(f"<html><body>{''.join(fields)}</body></html>", "lxml")


# A site comic page: field-author / field-com-group.
SITE_COMIC_PAGE = _page(
    _field("field-name-field-author", "Author", "Palcomix"),
    _field("field-name-field-com-group", "Section", "Teen Titans", "DC Universe"),
    _field("field-name-field-characters", "Characters", "Raven", "Starfire", "Cyborg"),
    _field("field-name-field-category", "Tags", "BDSM", "Mini Girl"),
    _field("field-name-field-user-tags", "User tags", "Force", "AI Generated"),
)

# A user content page (/mp<nodeid>): field-artist-term / field-section-term.
USER_CONTENT_PAGE = _page(
    _field("field-name-field-artist-term", "Artist", "The Man"),
    _field("field-name-field-section-term", "Section", "Furry"),
    _field("field-name-field-category", "Tags", "Anal", "Bondage"),
)


class TestSiteComicPage:
    def test_sections_are_read(self):
        assert _extract_taxonomy_terms(SITE_COMIC_PAGE, SECTION_FIELD_CLASSES) == [
            "Teen Titans", "DC Universe",
        ]

    def test_characters_are_read(self):
        assert _extract_taxonomy_terms(SITE_COMIC_PAGE, CHARACTER_FIELD_CLASSES) == [
            "Raven", "Starfire", "Cyborg",
        ]

    def test_curated_tags_are_read(self):
        assert _extract_taxonomy_terms(SITE_COMIC_PAGE, TAG_FIELD_CLASSES) == ["BDSM", "Mini Girl"]

    def test_user_tags_are_read(self):
        assert _extract_taxonomy_terms(SITE_COMIC_PAGE, USER_TAG_FIELD_CLASSES) == [
            "Force", "AI Generated",
        ]

    def test_curated_and_user_tags_stay_separate(self):
        """The two vocabularies differ in quality and get separate blacklists."""
        curated = _extract_taxonomy_terms(SITE_COMIC_PAGE, TAG_FIELD_CLASSES)
        assert "Force" not in curated
        assert "AI Generated" not in curated

    def test_author_is_read(self):
        assert _extract_author(SITE_COMIC_PAGE) == "Palcomix"


class TestUserContentPage:
    """/mp<nodeid> pages name the artist and section fields differently."""

    def test_artist_term_field_is_read_as_the_author(self):
        assert _extract_author(USER_CONTENT_PAGE) == "The Man"

    def test_section_term_field_is_read_as_a_section(self):
        assert _extract_taxonomy_terms(USER_CONTENT_PAGE, SECTION_FIELD_CLASSES) == ["Furry"]

    def test_tags_share_the_same_field_name_as_site_comics(self):
        assert _extract_taxonomy_terms(USER_CONTENT_PAGE, TAG_FIELD_CLASSES) == ["Anal", "Bondage"]

    def test_absent_optional_fields_yield_empty_lists(self):
        assert _extract_taxonomy_terms(USER_CONTENT_PAGE, CHARACTER_FIELD_CLASSES) == []
        assert _extract_taxonomy_terms(USER_CONTENT_PAGE, USER_TAG_FIELD_CLASSES) == []


class TestMissingFields:
    """Most optional fields are absent on most comics, so absence is normal."""

    BARE_PAGE = _page("<p>nothing but prose</p>")

    def test_every_field_is_empty_on_a_page_with_no_taxonomy_markup(self):
        for field_classes in (
            SECTION_FIELD_CLASSES, CHARACTER_FIELD_CLASSES,
            TAG_FIELD_CLASSES, USER_TAG_FIELD_CLASSES, AUTHOR_FIELD_CLASSES,
        ):
            assert _extract_taxonomy_terms(self.BARE_PAGE, field_classes) == []

    def test_author_is_an_empty_string_when_nothing_identifies_one(self):
        assert _extract_author(self.BARE_PAGE) == ""

    def test_a_field_present_but_holding_no_links_yields_nothing(self):
        page = _page(_field("field-name-field-characters", "Characters"))
        assert _extract_taxonomy_terms(page, CHARACTER_FIELD_CLASSES) == []

    def test_blank_link_text_is_ignored(self):
        page = _page(
            '<div class="field field-name-field-category">'
            '<a href="/a"></a><a href="/b">Oral</a></div>'
        )
        assert _extract_taxonomy_terms(page, TAG_FIELD_CLASSES) == ["Oral"]

    def test_duplicate_terms_are_collapsed_in_page_order(self):
        page = _page(
            _field("field-name-field-com-group", "Section",
                   "Teen Titans", "DC Universe", "Teen Titans")
        )
        assert _extract_taxonomy_terms(page, SECTION_FIELD_CLASSES) == [
            "Teen Titans", "DC Universe",
        ]


class TestAuthorFallback:
    def test_author_field_wins_over_an_earlier_artist_link(self):
        page = _page(
            '<a href="/authors_comics/someone_else">Someone Else</a>',
            _field("field-name-field-author", "Author", "Palcomix"),
        )
        assert _extract_author(page) == "Palcomix"

    def test_falls_back_to_artist_links_when_no_author_field_exists(self):
        page = _page('<a href="/user_content/artists/someone">Someone</a>')
        assert _extract_author(page) == "Someone"
