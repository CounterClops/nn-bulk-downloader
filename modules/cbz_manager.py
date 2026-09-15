import hashlib
import io
import os
import re
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

import imagehash
from PIL import Image

from modules import logger

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
# Same extensions without the leading dot, for fast ext-string validation.
_VALID_EXT = {e.lstrip(".") for e in IMAGE_EXTENSIONS}

# Exceptions that indicate a file could not be decoded as an image; these are
# expected when pHash encounters an unsupported format and trigger the SHA-256
# fallback without any warning.
_PHASH_DECODE_ERRORS = (OSError, SyntaxError, ValueError)


def sanitize_filename(name: str) -> str:
    """Strip filesystem-unsafe characters and replace spaces with underscores."""
    clean = re.sub(r'[:*?"<>|\\/$#@&%!`^(){}[\]=+~,;]', "", name)
    clean = clean.replace(" ", "_")
    return clean[:90].strip("._") or "comic"


def _safe_arcname(hint: str, fallback: str) -> str:
    """
    Return a safe zip arcname from *hint*.

    Rules enforced:
    - Stripped to basename only — any directory components (including ``..``)
      are discarded, so path traversal is impossible regardless of input.
    - Must be non-empty after stripping; falls back to *fallback* otherwise.
    - Legacy bare-extension hints like ``img.jpg`` also fall back to *fallback*.

    The result is a plain filename with no path separators, safe for use as a
    CBZ archive entry name.
    """
    # Normalize backslashes to forward slashes, then take only the last
    # component (basename), discarding any directory prefix (including "..").
    cleaned = hint.replace("\\", "/")
    # Take only the last component (basename); this discards any ".." segments
    basename = cleaned.rstrip("/").rpartition("/")[-1]
    if basename in ("", ".", "..") or basename.lower().startswith("img."):
        return fallback
    return basename


COMIC_INFO_NAME = "ComicInfo.xml"

# ComicInfo offers one Tags field, so the vocabularies that share it are marked
# with a namespace prefix the way other providers' ComicInfo files do. It is
# what keeps the site's curated "Tags:" distinguishable from the community
# "User tags:" once both are in the same field.
TAG_NAMESPACE = "tag"
USER_TAG_NAMESPACE = "other"
SECTION_NAMESPACE = "parody"


def _namespaced(namespace: str, terms: List[str]) -> List[str]:
    return [f"{namespace}: {term}" for term in terms]


def generate_comic_info_xml(metadata: Dict) -> bytes:
    """
    Build a ComicInfo.xml document (Anansi / Kavita / Komga compatible).

    Expected metadata keys (all optional):
      title, author, sections (list[str]), characters (list[str]),
      tags (list[str]), user_tags (list[str]), web, page_count (int), notes

    *sections* carries the site's "Section:" field, which names the series or
    franchise and is often multi-valued. ComicInfo's ``Series`` holds a single
    string, so the first section takes it while the full list goes to
    ``SeriesGroup`` — the schema's own field for the collections a series
    belongs to — and to ``Tags`` under the section namespace. A comic with no
    section falls back to its own title as the series, so it stands alone in a
    library rather than being filed under its artist.

    ``Tags`` carries three namespaced vocabularies: the site's curated tags, the
    community-editable user tags, and the sections. Any key that is missing or
    empty simply contributes nothing, and its element is left out entirely.

    Elements are emitted in the order ComicInfo.xsd declares them, since the
    schema defines a sequence rather than a free-order set.
    """
    root = ET.Element("ComicInfo")
    root.set("xmlns:xsd", "http://www.w3.org/2001/XMLSchema")
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")

    def add(tag: str, value):
        if value is not None and value != "" and value != []:
            el = ET.SubElement(root, tag)
            el.text = str(value)

    title = metadata.get("title", "Unknown")
    author = metadata.get("author") or ""
    sections = list(metadata.get("sections") or [])
    characters = list(metadata.get("characters") or [])
    tags = list(metadata.get("tags") or [])
    user_tags = list(metadata.get("user_tags") or [])

    namespaced_tags = (
        _namespaced(TAG_NAMESPACE, tags)
        + _namespaced(USER_TAG_NAMESPACE, user_tags)
        + _namespaced(SECTION_NAMESPACE, sections)
    )

    add("Title",       title)
    add("Series",      sections[0] if sections else title)
    add("Notes",       metadata.get("notes", "Downloaded by multporn-watcher"))
    add("Writer",      author)
    add("Penciller",   author)
    add("Tags",        ", ".join(namespaced_tags))
    add("Web",         metadata.get("web"))
    add("PageCount",   metadata.get("page_count"))
    add("Characters",  ", ".join(characters))
    add("SeriesGroup", ", ".join(sections))

    try:
        ET.indent(root, space="  ")
    except AttributeError:
        # ET.indent requires Python 3.9+; fall back to unindented on older versions
        pass

    buf = io.StringIO()
    buf.write('<?xml version="1.0" encoding="utf-8"?>\n')
    ET.ElementTree(root).write(buf, encoding="unicode")
    return buf.getvalue().encode("utf-8")


def read_comic_info(cbz_path: str) -> Optional[bytes]:
    """Return the raw ComicInfo.xml bytes stored in a CBZ, or None if absent."""
    if not os.path.exists(cbz_path):
        return None
    with zipfile.ZipFile(cbz_path, "r") as archive:
        try:
            return archive.read(COMIC_INFO_NAME)
        except KeyError:
            return None


def update_comic_info(cbz_path: str, metadata: Dict) -> bool:
    """Replace a CBZ's ComicInfo.xml in place, leaving its pages untouched.

    Returns True when the file was rewritten, False when the stored metadata
    already matched (or the CBZ does not exist). PageCount is taken from the
    archive itself rather than the caller, so a metadata refresh can never
    contradict the pages actually present.

    Only the archive's tail is rewritten: the replacement entry and a fresh
    central directory are written over the old central directory, which leaves
    the previous entry's bytes behind as an unreferenced hole of a few hundred
    bytes. That is what keeps this cheap — these archives average ~100 MB and
    live on a network share, so a full repack would move the whole file across
    the wire to change a few lines of XML.
    """
    if not os.path.exists(cbz_path):
        return False

    metadata = dict(metadata)
    metadata["page_count"] = get_cbz_page_count(cbz_path)
    payload = generate_comic_info_xml(metadata)

    if read_comic_info(cbz_path) == payload:
        return False

    with zipfile.ZipFile(cbz_path, "r") as archive:
        members = archive.infolist()
    # Everything from the final member onward is replaced, so keeping a copy of
    # it makes the rewrite recoverable: a failure part-way through would
    # otherwise leave the archive without a readable central directory.
    rewrite_start = max(member.header_offset for member in members)
    with open(cbz_path, "rb") as handle:
        handle.seek(rewrite_start)
        original_tail = handle.read()

    try:
        with open(cbz_path, "r+b") as handle:
            with zipfile.ZipFile(handle, "a", zipfile.ZIP_DEFLATED) as archive:
                # Dropping the old entry from the in-memory index is what stops
                # close() from listing it a second time in the new central
                # directory; a CBZ with two ComicInfo.xml entries reads
                # differently from one viewer to the next.
                stale = archive.NameToInfo.pop(COMIC_INFO_NAME, None)
                if stale is not None:
                    archive.filelist.remove(stale)
                archive.writestr(COMIC_INFO_NAME, payload)
            # close() writes the central directory over the old one; when the
            # new metadata is shorter than what it replaced, the previous
            # archive's tail would otherwise linger past the end record.
            handle.truncate(handle.tell())
        _verify_comic_info(cbz_path, payload)
    except Exception:
        with open(cbz_path, "r+b") as handle:
            handle.seek(rewrite_start)
            handle.write(original_tail)
            handle.truncate()
        raise

    return True


def _verify_comic_info(cbz_path: str, payload: bytes):
    """Confirm a rewritten CBZ still reads as one archive with one ComicInfo.

    The rewrite reaches into ZipFile's entry index to drop the superseded
    entry, so this checks the outcome rather than trusting that internal shape.
    """
    with zipfile.ZipFile(cbz_path, "r") as archive:
        names = archive.namelist()
        if names.count(COMIC_INFO_NAME) != 1:
            raise RuntimeError(
                f"{cbz_path}: expected exactly one {COMIC_INFO_NAME} after rewrite, "
                f"found {names.count(COMIC_INFO_NAME)}"
            )
        if archive.read(COMIC_INFO_NAME) != payload:
            raise RuntimeError(f"{cbz_path}: {COMIC_INFO_NAME} did not read back as written")


def create_cbz(
    cbz_path: str,
    image_paths: List[Tuple[str, str]],
    metadata: Dict,
):
    """
    Create a brand-new CBZ at *cbz_path*.

    image_paths: list of (filename_hint, source_file_path)
    Pages are stored as zero-padded 3-digit filenames: 001.jpg, 002.png …
    Images are streamed from disk — never fully loaded into memory.
    Written atomically via a .tmp file.
    """
    cbz_dir = os.path.dirname(cbz_path)
    if cbz_dir:
        os.makedirs(cbz_dir, exist_ok=True)

    tmp_path = cbz_path + ".tmp"
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, (hint, src_path) in enumerate(image_paths, start=1):
                raw_ext = hint.rpartition(".")[-1].lower()
                ext = raw_ext if raw_ext in _VALID_EXT else "jpg"
                fallback = f"{i:04d}.{ext}"
                # _safe_arcname normalizes to basename, discards any directory
                # components, and falls back for legacy bare-extension hints like "img.jpg".
                arcname = _safe_arcname(hint, fallback)
                zf.write(src_path, arcname=arcname)

            metadata = dict(metadata)
            metadata["page_count"] = len(image_paths)
            zf.writestr(COMIC_INFO_NAME, generate_comic_info_xml(metadata))

        os.replace(tmp_path, cbz_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def update_cbz(
    cbz_path: str,
    new_image_paths: List[Tuple[str, str]],
    updated_metadata: Dict,
):
    """Kept for backwards compatibility — delegates to sync_cbz."""
    sync_cbz(cbz_path, new_image_paths, updated_metadata)


def _compute_phash(image_path: str) -> "imagehash.ImageHash":
    with Image.open(image_path) as im:
        img = im.convert("RGB")
    return imagehash.phash(img)


def _file_sha256(path: str) -> str:
    """Return the SHA-256 hex digest of a file, streamed in chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def images_visually_same(path1: str, path2: str, threshold: int = 10) -> bool:
    """
    Return True if two image files are perceptually identical.

    Primary method: pHash Hamming distance <= threshold (tolerates minor JPEG
    re-compression artefacts while flagging genuinely replaced images).

    Fallback: if pHash cannot decode the file (e.g. an animated GIF frame that
    PIL cannot process, or any unsupported format), falls back to a byte-exact
    SHA-256 comparison.  This avoids incorrectly treating identical files as
    changed and unnecessarily archiving them.
    """
    try:
        return bool((_compute_phash(path1) - _compute_phash(path2)) <= threshold)
    except _PHASH_DECODE_ERRORS:
        pass  # expected decode failure — fall through to SHA-256 comparison
    except Exception as exc:
        logger.warning(f"Unexpected error during pHash comparison: {exc}")
    try:
        return _file_sha256(path1) == _file_sha256(path2)
    except Exception:
        return False


def sync_cbz(
    cbz_path: str,
    remote_image_paths: List[Tuple[str, str]],
    metadata: Dict,
    phash_threshold: int = 10,
) -> Dict:
    """
    Full-sync a CBZ against the complete current set of remote pages.

    For each position (1-indexed):
      - remote == existing (pHash ≤ threshold): keep existing bytes (original quality)
      - remote != existing: archive the old page as ``NNN.ext.archive_K``, use remote
      - position in CBZ but not remote: archive it (page removed from site)
      - position only in remote: add as new page

    remote_image_paths: list of (hint, src_path) for ALL current remote pages

    Returns a dict with keys: live_pages, pages_archived, pages_unchanged.
    Written atomically via a .tmp file.
    """
    tmp_cbz = cbz_path + ".tmp"
    cbz_dir = os.path.dirname(cbz_path)
    if cbz_dir:
        os.makedirs(cbz_dir, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="multporn_sync_") as extract_dir:
        existing_pages: List[Tuple[str, str]] = []  # (arcname, local_path) sorted
        carry_archives: List[Tuple[str, str]] = []  # already-archived entries to carry over
        max_archive_idx = 0

        if os.path.exists(cbz_path):
            with zipfile.ZipFile(cbz_path, "r") as zf:
                for name in sorted(zf.namelist()):
                    if ".archive_" in name:
                        # Carry existing archive entries into the new CBZ
                        safe = re.sub(r'[/\\]', '_', name)
                        ep = os.path.join(extract_dir, "arch_" + safe)
                        with open(ep, "wb") as fh:
                            fh.write(zf.read(name))
                        carry_archives.append((name, ep))
                        m = re.search(r'\.archive_(\d+)', name)
                        if m:
                            max_archive_idx = max(max_archive_idx, int(m.group(1)))
                    else:
                        _, ext = os.path.splitext(name.lower())
                        if ext not in IMAGE_EXTENSIONS:
                            continue
                        ep = os.path.join(extract_dir, name)
                        zf.extract(name, extract_dir)
                        existing_pages.append((name, ep))

        next_arch = max_archive_idx + 1
        new_pages:    List[Tuple[str, str]] = []   # (arcname, filepath) — live pages
        new_archives: List[Tuple[str, str]] = []   # (arcname, filepath) — newly archived
        unchanged_count = 0

        n_positions = max(len(existing_pages), len(remote_image_paths))
        for i in range(n_positions):
            pnum = i + 1
            has_ex = i < len(existing_pages)
            has_re = i < len(remote_image_paths)

            if has_ex and has_re:
                ex_name, ex_path = existing_pages[i]
                hint, re_path = remote_image_paths[i]
                raw_ext = hint.rpartition(".")[-1].lower()
                ext = raw_ext if raw_ext in _VALID_EXT else "jpg"
                fallback = f"{pnum:04d}.{ext}"
                target = _safe_arcname(hint, fallback)

                if images_visually_same(ex_path, re_path, phash_threshold):
                    new_pages.append((target, ex_path))
                    unchanged_count += 1
                else:
                    arch_name = f"{ex_name}.archive_{next_arch}"
                    new_archives.append((arch_name, ex_path))
                    next_arch += 1
                    new_pages.append((target, re_path))

            elif has_ex:
                # Page exists locally but no longer on the remote site — archive it
                ex_name, ex_path = existing_pages[i]
                arch_name = f"{ex_name}.archive_{next_arch}"
                new_archives.append((arch_name, ex_path))
                next_arch += 1

            else:
                # New page on remote that we didn't have
                hint, re_path = remote_image_paths[i]
                raw_ext = hint.rpartition(".")[-1].lower()
                ext = raw_ext if raw_ext in _VALID_EXT else "jpg"
                fallback = f"{pnum:04d}.{ext}"
                new_pages.append((_safe_arcname(hint, fallback), re_path))

        try:
            with zipfile.ZipFile(tmp_cbz, "w", zipfile.ZIP_DEFLATED) as dst:
                for arcname, fp in new_pages:
                    dst.write(fp, arcname=arcname)
                for arcname, fp in new_archives:
                    dst.write(fp, arcname=arcname)
                for arcname, fp in carry_archives:
                    dst.write(fp, arcname=arcname)

                metadata = dict(metadata)
                metadata["page_count"] = len(new_pages)
                dst.writestr(COMIC_INFO_NAME, generate_comic_info_xml(metadata))

            os.replace(tmp_cbz, cbz_path)
        except Exception:
            if os.path.exists(tmp_cbz):
                os.remove(tmp_cbz)
            raise

    return {
        "live_pages":      len(new_pages),
        "pages_archived":  len(new_archives),
        "pages_unchanged": unchanged_count,
    }


def hash_cbz(cbz_path: str) -> str:
    """Return the SHA-256 hex digest of the CBZ file, streamed in chunks."""
    h = hashlib.sha256()
    with open(cbz_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def get_cbz_page_count(cbz_path: str) -> int:
    """Count live image pages in an existing CBZ (excludes ComicInfo.xml and archives)."""
    if not os.path.exists(cbz_path):
        return 0
    with zipfile.ZipFile(cbz_path, "r") as zf:
        return sum(
            1 for n in zf.namelist()
            if os.path.splitext(n.lower())[-1] in IMAGE_EXTENSIONS
            and ".archive_" not in n
        )
