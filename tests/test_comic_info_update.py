"""Tests for updating a CBZ's ComicInfo.xml without repacking the archive."""

import hashlib
import os
import struct
import tempfile
import zipfile
import zlib
from unittest.mock import patch

from modules import cbz_manager as cbz
from modules.cbz_manager import (
    COMIC_INFO_NAME,
    generate_comic_info_xml,
    read_comic_info,
    update_comic_info,
)


def _minimal_png(color: tuple) -> bytes:
    """Return a 1x1 PNG with the given (R, G, B) colour."""
    def chunk(tag, data):
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    signature = b"\x89PNG\r\n\x1a\n"
    header = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    pixels = chunk(b"IDAT", zlib.compress(b"\x00" + bytes(color)))
    return signature + header + pixels + chunk(b"IEND", b"")


ORIGINAL_METADATA = {
    "title":      "The Blame Game",
    "author":     "Palcomix",
    "sections":   ["Teen Titans"],
    "characters": [],
    "tags":       ["BDSM"],
    "web":        "https://multporn.net/comics/the_blame_game",
}

ENRICHED_METADATA = dict(
    ORIGINAL_METADATA,
    sections=["Teen Titans", "DC Universe"],
    characters=["Raven", "Starfire", "Cyborg"],
)


def _build_cbz(directory: str, page_count: int = 3, metadata: dict = None) -> str:
    """Write a CBZ whose pages are distinguishable 1x1 PNGs."""
    cbz_path = os.path.join(directory, "comic.cbz")
    pages = []
    for page_number in range(1, page_count + 1):
        page_path = os.path.join(directory, f"{page_number:04d}.png")
        with open(page_path, "wb") as handle:
            handle.write(_minimal_png((page_number * 30 % 256, 0, 0)))
        pages.append((f"{page_number:04d}.png", page_path))
    cbz.create_cbz(cbz_path, pages, dict(metadata or ORIGINAL_METADATA))
    return cbz_path


def _page_digests(cbz_path: str) -> dict:
    with zipfile.ZipFile(cbz_path) as archive:
        return {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if name != COMIC_INFO_NAME
        }


class TestPagesArePreserved:
    def test_page_bytes_are_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            cbz_path = _build_cbz(directory)
            before = _page_digests(cbz_path)
            update_comic_info(cbz_path, ENRICHED_METADATA)
            assert _page_digests(cbz_path) == before

    def test_archive_remains_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            cbz_path = _build_cbz(directory)
            update_comic_info(cbz_path, ENRICHED_METADATA)
            with zipfile.ZipFile(cbz_path) as archive:
                assert archive.testzip() is None

    def test_exactly_one_comic_info_entry_remains(self):
        """Two entries under one name read differently from viewer to viewer."""
        with tempfile.TemporaryDirectory() as directory:
            cbz_path = _build_cbz(directory)
            update_comic_info(cbz_path, ENRICHED_METADATA)
            with zipfile.ZipFile(cbz_path) as archive:
                assert archive.namelist().count(COMIC_INFO_NAME) == 1

    def test_no_bytes_linger_past_the_end_record(self):
        """A shorter replacement must not leave the previous archive's tail behind."""
        with tempfile.TemporaryDirectory() as directory:
            cbz_path = _build_cbz(directory, metadata=ENRICHED_METADATA)
            update_comic_info(cbz_path, ORIGINAL_METADATA)
            with open(cbz_path, "rb") as handle:
                data = handle.read()
            assert data.rfind(b"PK\x05\x06") == len(data) - 22


class TestWrittenMetadata:
    def test_new_fields_reach_the_stored_document(self):
        with tempfile.TemporaryDirectory() as directory:
            cbz_path = _build_cbz(directory)
            update_comic_info(cbz_path, ENRICHED_METADATA)
            stored = read_comic_info(cbz_path).decode()
            assert "<Characters>Raven, Starfire, Cyborg</Characters>" in stored
            assert "<SeriesGroup>Teen Titans, DC Universe</SeriesGroup>" in stored

    def test_page_count_comes_from_the_archive_not_the_caller(self):
        """A refresh must never contradict the pages actually present."""
        with tempfile.TemporaryDirectory() as directory:
            cbz_path = _build_cbz(directory, page_count=3)
            update_comic_info(cbz_path, dict(ENRICHED_METADATA, page_count=99))
            assert b"<PageCount>3</PageCount>" in read_comic_info(cbz_path)

    def test_stored_document_matches_a_freshly_generated_one(self):
        with tempfile.TemporaryDirectory() as directory:
            cbz_path = _build_cbz(directory, page_count=3)
            update_comic_info(cbz_path, ENRICHED_METADATA)
            expected = generate_comic_info_xml(dict(ENRICHED_METADATA, page_count=3))
            assert read_comic_info(cbz_path) == expected


class TestChangeDetection:
    def test_returns_true_when_metadata_differs(self):
        with tempfile.TemporaryDirectory() as directory:
            cbz_path = _build_cbz(directory)
            assert update_comic_info(cbz_path, ENRICHED_METADATA) is True

    def test_returns_false_when_metadata_already_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            cbz_path = _build_cbz(directory)
            assert update_comic_info(cbz_path, ORIGINAL_METADATA) is False

    def test_unchanged_metadata_leaves_the_file_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            cbz_path = _build_cbz(directory)
            with open(cbz_path, "rb") as handle:
                before = handle.read()
            update_comic_info(cbz_path, ORIGINAL_METADATA)
            with open(cbz_path, "rb") as handle:
                assert handle.read() == before

    def test_missing_file_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as directory:
            absent = os.path.join(directory, "nothing.cbz")
            assert update_comic_info(absent, ENRICHED_METADATA) is False
            assert not os.path.exists(absent)

    def test_repeated_updates_converge(self):
        with tempfile.TemporaryDirectory() as directory:
            cbz_path = _build_cbz(directory)
            assert update_comic_info(cbz_path, ENRICHED_METADATA) is True
            assert update_comic_info(cbz_path, ENRICHED_METADATA) is False


class TestArchiveWithoutComicInfo:
    def test_metadata_is_added_to_an_archive_that_has_none(self):
        with tempfile.TemporaryDirectory() as directory:
            cbz_path = os.path.join(directory, "bare.cbz")
            with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("0001.png", _minimal_png((10, 0, 0)))

            assert read_comic_info(cbz_path) is None
            assert update_comic_info(cbz_path, ENRICHED_METADATA) is True
            assert b"<Characters>Raven, Starfire, Cyborg</Characters>" in read_comic_info(cbz_path)
            with zipfile.ZipFile(cbz_path) as archive:
                assert archive.testzip() is None
                assert "0001.png" in archive.namelist()


class TestFailureRollback:
    def test_a_failed_rewrite_restores_the_original_archive(self):
        """The rewrite overwrites the central directory, so a half-done write
        would otherwise leave the CBZ unreadable."""
        with tempfile.TemporaryDirectory() as directory:
            cbz_path = _build_cbz(directory)
            with open(cbz_path, "rb") as handle:
                before = handle.read()

            with patch.object(cbz, "_verify_comic_info", side_effect=RuntimeError("boom")):
                try:
                    update_comic_info(cbz_path, ENRICHED_METADATA)
                    raised = False
                except RuntimeError:
                    raised = True

            assert raised, "the failure should propagate to the caller"
            with open(cbz_path, "rb") as handle:
                assert handle.read() == before
            with zipfile.ZipFile(cbz_path) as archive:
                assert archive.testzip() is None
