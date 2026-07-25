"""Tests for cbz_manager utilities."""

import os
import tempfile

from modules.cbz_manager import images_visually_same, _safe_arcname


# ---------------------------------------------------------------------------
# images_visually_same — SHA-256 fallback when pHash fails
# ---------------------------------------------------------------------------

class TestImagesVisuallySame:
    def _write(self, dir_: str, name: str, data: bytes) -> str:
        path = os.path.join(dir_, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_phash_identical_png(self):
        """Two copies of a valid PNG are treated as the same via pHash."""
        import struct, zlib

        def _minimal_png(color: tuple) -> bytes:
            """Return a 1×1 PNG with the given (R, G, B) colour."""
            def chunk(tag, data):
                c = zlib.crc32(tag + data) & 0xFFFFFFFF
                return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", c)
            sig = b"\x89PNG\r\n\x1a\n"
            ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            raw = b"\x00" + bytes(color)
            idat = chunk(b"IDAT", zlib.compress(raw))
            iend = chunk(b"IEND", b"")
            return sig + ihdr + idat + iend

        with tempfile.TemporaryDirectory() as d:
            p1 = self._write(d, "a.png", _minimal_png((255, 0, 0)))
            p2 = self._write(d, "b.png", _minimal_png((255, 0, 0)))
            assert images_visually_same(p1, p2)  # pHash may return np.bool_

    def test_sha256_fallback_identical_files(self):
        """Identical non-image files (binary blobs) return True via SHA-256 fallback."""
        data = b"\x00\x01\x02" * 100  # not a valid image
        with tempfile.TemporaryDirectory() as d:
            p1 = self._write(d, "a.bin", data)
            p2 = self._write(d, "b.bin", data)
            assert images_visually_same(p1, p2) is True

    def test_sha256_fallback_different_files(self):
        """Different non-image files return False via SHA-256 fallback."""
        with tempfile.TemporaryDirectory() as d:
            p1 = self._write(d, "a.bin", b"\x00" * 100)
            p2 = self._write(d, "b.bin", b"\xFF" * 100)
            assert images_visually_same(p1, p2) is False

    def test_sha256_fallback_empty_vs_nonempty(self):
        """Empty file vs non-empty file returns False via SHA-256 fallback."""
        with tempfile.TemporaryDirectory() as d:
            p1 = self._write(d, "empty.bin", b"")
            p2 = self._write(d, "data.bin", b"\x01\x02\x03")
            assert images_visually_same(p1, p2) is False

    def test_sha256_fallback_both_empty(self):
        """Two empty files are identical via SHA-256 fallback."""
        with tempfile.TemporaryDirectory() as d:
            p1 = self._write(d, "a.bin", b"")
            p2 = self._write(d, "b.bin", b"")
            assert images_visually_same(p1, p2) is True


# ---------------------------------------------------------------------------
# _safe_arcname
# ---------------------------------------------------------------------------

class TestSafeArcname:
    def test_normal_hint_used_directly(self):
        assert _safe_arcname("0001-page_name.jpg", "0001.jpg") == "0001-page_name.jpg"

    def test_legacy_img_hint_falls_back(self):
        assert _safe_arcname("img.jpg", "0001.jpg") == "0001.jpg"

    def test_legacy_img_hint_with_prefix_path_falls_back(self):
        # The "img." check is on basename, so subdir/img.jpg also falls back
        assert _safe_arcname("subdir/img.jpg", "0001.jpg") == "0001.jpg"

    def test_legacy_img_hint_case_insensitive(self):
        assert _safe_arcname("IMG.JPG", "0001.jpg") == "0001.jpg"

    def test_forward_slash_stripped_to_basename(self):
        # A hint with a path separator should return just the basename, not fall back
        assert _safe_arcname("subdir/0001-page.jpg", "0001.jpg") == "0001-page.jpg"

    def test_backslash_stripped_to_basename(self):
        assert _safe_arcname("subdir\\0001-page.jpg", "0001.jpg") == "0001-page.jpg"

    def test_dotdot_traversal_stripped_to_safe_basename(self):
        # A leading "../" is stripped; the resulting basename "evil.jpg" is safe.
        assert _safe_arcname("../evil.jpg", "0001.jpg") == "evil.jpg"

    def test_bare_dotdot_falls_back(self):
        # A hint whose basename IS ".." (e.g. "subdir/..") falls back.
        assert _safe_arcname("subdir/..", "0001.jpg") == "0001.jpg"

    def test_absolute_path_stripped_to_basename(self):
        result = _safe_arcname("/etc/passwd", "0001.jpg")
        assert result == "passwd"

    def test_empty_hint_falls_back(self):
        assert _safe_arcname("", "0001.jpg") == "0001.jpg"

    def test_dot_hint_falls_back(self):
        assert _safe_arcname(".", "0001.jpg") == "0001.jpg"
