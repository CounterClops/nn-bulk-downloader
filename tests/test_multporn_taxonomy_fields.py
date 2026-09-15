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
    links = "".join(
        f'<a href="/category/example_{term.lower().replace(" ", "_")}">{term}</a>'
        for term in terms
    )
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
    _field("field-name-field-author", "Author", "Example Author"),
    _field("field-name-field-com-group", "Section", "Example Series", "Example Universe"),
    _field("field-name-field-characters", "Characters", "Character One", "Character Two", "Character Three"),
    _field("field-name-field-category", "Tags", "Curated Tag A", "Curated Tag B"),
    _field("field-name-field-user-tags", "User tags", "User Tag A", "User Tag B"),
)

# A user content page (/mp<nodeid>): field-artist-term / field-section-term.
USER_CONTENT_PAGE = _page(
    _field("field-name-field-artist-term", "Artist", "Example User Artist"),
    _field("field-name-field-section-term", "Section", "Example User Section"),
    _field("field-name-field-category", "Tags", "Curated Tag C", "Curated Tag D"),
)


class TestSiteComicPage:
    def test_sections_are_read(self):
        assert _extract_taxonomy_terms(SITE_COMIC_PAGE, SECTION_FIELD_CLASSES) == [
            "Example Series", "Example Universe",
        ]

    def test_characters_are_read(self):
        assert _extract_taxonomy_terms(SITE_COMIC_PAGE, CHARACTER_FIELD_CLASSES) == [
            "Character One", "Character Two", "Character Three",
        ]

    def test_curated_tags_are_read(self):
        assert _extract_taxonomy_terms(SITE_COMIC_PAGE, TAG_FIELD_CLASSES) == ["Curated Tag A", "Curated Tag B"]

    def test_user_tags_are_read(self):
        assert _extract_taxonomy_terms(SITE_COMIC_PAGE, USER_TAG_FIELD_CLASSES) == [
            "User Tag A", "User Tag B",
        ]

    def test_curated_and_user_tags_stay_separate(self):
        """The two vocabularies differ in quality and get separate blacklists."""
        curated = _extract_taxonomy_terms(SITE_COMIC_PAGE, TAG_FIELD_CLASSES)
        assert "User Tag A" not in curated
        assert "User Tag B" not in curated

    def test_author_is_read(self):
        assert _extract_author(SITE_COMIC_PAGE) == "Example Author"


class TestUserContentPage:
    """/mp<nodeid> pages name the artist and section fields differently."""

    def test_artist_term_field_is_read_as_the_author(self):
        assert _extract_author(USER_CONTENT_PAGE) == "Example User Artist"

    def test_section_term_field_is_read_as_a_section(self):
        assert _extract_taxonomy_terms(USER_CONTENT_PAGE, SECTION_FIELD_CLASSES) == ["Example User Section"]

    def test_tags_share_the_same_field_name_as_site_comics(self):
        assert _extract_taxonomy_terms(USER_CONTENT_PAGE, TAG_FIELD_CLASSES) == ["Curated Tag C", "Curated Tag D"]

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
            '<a href="/category/example_blank"></a><a href="/category/example_curated_tag_a">Curated Tag A</a></div>'
        )
        assert _extract_taxonomy_terms(page, TAG_FIELD_CLASSES) == ["Curated Tag A"]

    def test_duplicate_terms_are_collapsed_in_page_order(self):
        page = _page(
            _field("field-name-field-com-group", "Section",
                   "Example Series", "Example Universe", "Example Series")
        )
        assert _extract_taxonomy_terms(page, SECTION_FIELD_CLASSES) == [
            "Example Series", "Example Universe",
        ]


class TestAuthorFallback:
    def test_author_field_wins_over_an_earlier_artist_link(self):
        page = _page(
            '<a href="/authors_comics/example_other_artist">Someone Else</a>',
            _field("field-name-field-author", "Author", "Example Author"),
        )
        assert _extract_author(page) == "Example Author"

    def test_falls_back_to_artist_links_when_no_author_field_exists(self):
        page = _page('<a href="/user_content/artists/example_someone">Someone</a>')
        assert _extract_author(page) == "Someone"
