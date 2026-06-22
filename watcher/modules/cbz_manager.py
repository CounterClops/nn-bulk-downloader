import hashlib
import io
import os
import re
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Tuple

import imagehash
from PIL import Image

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def sanitize_filename(name: str) -> str:
    """Strip filesystem-unsafe characters and replace spaces with underscores."""
    clean = re.sub(r'[:*?"<>|\\/$#@&%!`^(){}[\]=+~,;]', "", name)
    clean = clean.replace(" ", "_")
    return clean[:90].strip("._") or "comic"


def generate_comic_info_xml(metadata: Dict) -> bytes:
    """
    Build a ComicInfo.xml document (Anansi / Kavita / Komga compatible).

    Expected metadata keys (all optional):
      title, series, web, tags (list[str]), page_count (int),
      writer, penciller, notes
    """
    root = ET.Element("ComicInfo")
    root.set("xmlns:xsd", "http://www.w3.org/2001/XMLSchema")
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")

    def add(tag: str, value):
        if value is not None and value != "" and value != []:
            el = ET.SubElement(root, tag)
            el.text = str(value)

    add("Title",      metadata.get("title", "Unknown"))
    add("Series",     metadata.get("series"))
    add("Web",        metadata.get("web"))

    tags = metadata.get("tags") or []
    if tags:
        add("Tags", ", ".join(tags))

    add("PageCount",  metadata.get("page_count"))
    writer = metadata.get("writer") or metadata.get("series") or ""
    add("Writer",     writer)
    add("Penciller",  metadata.get("penciller") or writer)
    add("Notes",      metadata.get("notes", "Downloaded by multporn-watcher"))

    try:
        ET.indent(root, space="  ")
    except AttributeError:
        # ET.indent requires Python 3.9+; fall back to unindented on older versions
        pass

    buf = io.StringIO()
    buf.write('<?xml version="1.0" encoding="utf-8"?>\n')
    ET.ElementTree(root).write(buf, encoding="unicode")
    return buf.getvalue().encode("utf-8")


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
                ext = hint.rpartition(".")[-1].lower() or "jpg"
                zf.write(src_path, arcname=f"{i:03d}.{ext}")

            metadata = dict(metadata)
            metadata["page_count"] = len(image_paths)
            zf.writestr("ComicInfo.xml", generate_comic_info_xml(metadata))

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
    img = Image.open(image_path).convert("RGB")
    return imagehash.phash(img)


def images_visually_same(path1: str, path2: str, threshold: int = 10) -> bool:
    """
    Return True if two image files are perceptually identical (pHash Hamming
    distance ≤ threshold).  A threshold of 10 tolerates minor JPEG
    re-compression artefacts while flagging genuinely replaced images.
    Falls back to False on any error (treats as different).
    """
    try:
        return (_compute_phash(path1) - _compute_phash(path2)) <= threshold
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
                ext = hint.rpartition(".")[-1].lower() or "jpg"
                target = f"{pnum:03d}.{ext}"

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
                ext = hint.rpartition(".")[-1].lower() or "jpg"
                new_pages.append((f"{pnum:03d}.{ext}", re_path))

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
                dst.writestr("ComicInfo.xml", generate_comic_info_xml(metadata))

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
