"""Tests for the ComicInfo.xml field mapping.

The site exposes "Author:", "Section:", "Characters:", "Tags:" and "User tags:"
as separate taxonomy fields; these cover how each lands in ComicInfo.
"""

import xml.etree.ElementTree as ET

from modules.cbz_manager import generate_comic_info_xml


# Element order declared by ComicInfo.xsd, restricted to the fields written here.
SCHEMA_ORDER = [
    "Title", "Series", "Notes", "Writer", "Penciller",
    "Tags", "Web", "PageCount", "Characters", "SeriesGroup",
]


def _parse(metadata: dict) -> ET.Element:
    return ET.fromstring(generate_comic_info_xml(metadata))


def _text(metadata: dict, tag: str):
    found = _parse(metadata).find(tag)
    return found.text if found is not None else None


def _tags(metadata: dict) -> list:
    raw = _text(metadata, "Tags")
    return raw.split(", ") if raw else []


FULL_METADATA = {
    "title":      "The Blame Game",
    "author":     "Palcomix",
    "sections":   ["Teen Titans", "DC Universe"],
    "characters": ["Raven", "Starfire", "Cyborg", "Robin", "Beast Boy"],
    "tags":       ["BDSM", "Mini Girl"],
    "user_tags":  ["Force", "AI Generated"],
    "web":        "https://multporn.net/comics/the_blame_game",
    "page_count": 24,
}


class TestNamespacedTags:
    def test_curated_tags_use_the_tag_namespace(self):
        assert "tag: BDSM" in _tags(FULL_METADATA)
        assert "tag: Mini Girl" in _tags(FULL_METADATA)

    def test_user_tags_use_the_other_namespace(self):
        assert "other: Force" in _tags(FULL_METADATA)
        assert "other: AI Generated" in _tags(FULL_METADATA)

    def test_sections_use_the_parody_namespace(self):
        assert "parody: Teen Titans" in _tags(FULL_METADATA)
        assert "parody: DC Universe" in _tags(FULL_METADATA)

    def test_vocabularies_appear_in_a_stable_order(self):
        assert _tags(FULL_METADATA) == [
            "tag: BDSM", "tag: Mini Girl",
            "other: Force", "other: AI Generated",
            "parody: Teen Titans", "parody: DC Universe",
        ]

    def test_site_casing_is_preserved(self):
        assert "other: AI Generated" in _tags(FULL_METADATA)

    def test_a_term_in_both_vocabularies_stays_distinguishable(self):
        metadata = dict(FULL_METADATA, tags=["BDSM"], user_tags=["BDSM"])
        assert _tags(metadata)[:2] == ["tag: BDSM", "other: BDSM"]

    def test_tags_element_is_omitted_when_every_vocabulary_is_empty(self):
        metadata = dict(FULL_METADATA, tags=[], user_tags=[], sections=[])
        assert _parse(metadata).find("Tags") is None


class TestSeriesFromSection:
    def test_first_section_becomes_series(self):
        assert _text(FULL_METADATA, "Series") == "Teen Titans"

    def test_every_section_is_listed_in_series_group(self):
        assert _text(FULL_METADATA, "SeriesGroup") == "Teen Titans, DC Universe"

    def test_title_is_the_series_when_no_section_is_listed(self):
        assert _text(dict(FULL_METADATA, sections=[]), "Series") == "The Blame Game"

    def test_series_group_omitted_when_no_section_is_listed(self):
        assert _parse(dict(FULL_METADATA, sections=[])).find("SeriesGroup") is None


class TestAuthor:
    def test_author_fills_writer_and_penciller(self):
        assert _text(FULL_METADATA, "Writer") == "Palcomix"
        assert _text(FULL_METADATA, "Penciller") == "Palcomix"

    def test_author_is_not_repeated_as_the_series(self):
        """The artist used to occupy Series as well; the section owns it now."""
        assert _text(FULL_METADATA, "Series") != "Palcomix"

    def test_author_is_not_the_series_fallback_either(self):
        assert _text(dict(FULL_METADATA, sections=[]), "Series") == "The Blame Game"

    def test_author_is_not_duplicated_into_tags(self):
        assert not any("Palcomix" in entry for entry in _tags(FULL_METADATA))


class TestCharacters:
    def test_characters_are_comma_separated_in_page_order(self):
        assert _text(FULL_METADATA, "Characters") == "Raven, Starfire, Cyborg, Robin, Beast Boy"

    def test_characters_omitted_when_the_page_lists_none(self):
        assert _parse(dict(FULL_METADATA, characters=[])).find("Characters") is None

    def test_characters_stay_out_of_tags(self):
        assert not any("Raven" in entry for entry in _tags(FULL_METADATA))


class TestMissingFieldsAreOmitted:
    """A comic missing any optional field must still produce a valid document."""

    def test_each_field_can_be_absent_individually(self):
        for field in ("sections", "characters", "tags", "user_tags", "author"):
            metadata = dict(FULL_METADATA)
            del metadata[field]
            root = _parse(metadata)
            assert root.tag == "ComicInfo"
            assert root.find("Title").text == "The Blame Game"

    def test_none_is_treated_the_same_as_an_empty_list(self):
        metadata = dict(FULL_METADATA, characters=None, user_tags=None, sections=None)
        root = _parse(metadata)
        assert root.find("Characters") is None
        assert root.find("SeriesGroup") is None
        assert _tags(metadata) == ["tag: BDSM", "tag: Mini Girl"]

    def test_empty_metadata_still_produces_a_valid_document(self):
        root = _parse({})
        assert root.tag == "ComicInfo"
        assert root.find("Title").text == "Unknown"
        assert root.find("Tags") is None

    def test_a_comic_with_only_user_tags_still_gets_a_tags_element(self):
        metadata = dict(FULL_METADATA, tags=[], sections=[])
        assert _tags(metadata) == ["other: Force", "other: AI Generated"]


class TestDocumentShape:
    def test_elements_follow_schema_order(self):
        emitted = [child.tag for child in _parse(FULL_METADATA)]
        assert emitted == [tag for tag in SCHEMA_ORDER if tag in emitted]

    def test_page_count_is_carried_through(self):
        assert _text(FULL_METADATA, "PageCount") == "24"
