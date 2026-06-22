#!/usr/bin/env python3
"""
multporn-watcher — monitor comics/artists on multporn.net and keep local
CBZ archives up-to-date.

Usage:
  python main.py [--config PATH] [--watchlist PATH] [--output DIR] [--db PATH] [--watch]
"""

import argparse
import json
import os
import sys
import tempfile
import time
from time import sleep
from typing import Dict, Set

import requests

from modules import cbz_manager as cbz
from modules import config as cfg
from modules import db
from modules import logger
from modules import multporn as mp
from modules import watchlist as wl

VERSION = "1.0.0"
USER_AGENT = f"multporn-watcher/{VERSION} (github.com/CounterClops/nn-bulk-downloader)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def _cbz_path_for(output_dir: str, title: str, author: str = "") -> str:
    sanitized_title = cbz.sanitize_filename(title)
    if author:
        sanitized_author = cbz.sanitize_filename(author)
        filename = f"[{sanitized_author}]_{sanitized_title}"
    else:
        filename = sanitized_title
    return os.path.join(output_dir, filename + ".cbz")


def _make_rel_cbz(output_dir: str, abs_path: str) -> str:
    """Convert an absolute CBZ path to a path relative to output_dir."""
    return os.path.relpath(abs_path, output_dir)


def _resolve_abs_cbz(output_dir: str, raw: str) -> str:
    """Resolve a stored cbz_path (relative or legacy absolute) to an absolute path."""
    if os.path.isabs(raw):
        return raw
    return os.path.normpath(os.path.join(output_dir, raw))


def _is_blacklisted(tags: list, blacklist: list) -> bool:
    tags_lower = {t.lower() for t in tags}
    return any(b.lower() in tags_lower for b in blacklist)


def _is_censored(tags: list) -> bool:
    """Return True if *tags* indicate this is a censored/minor-content comic."""
    tags_lower = {t.lower() for t in tags}
    return bool(tags_lower & mp.CENSORED_TAGS)


def _is_language_allowed(language: str, allowed_languages: list) -> bool:
    """Return True if *language* is in the allowed list, or the list is empty (allow all).

    Comparison is done on the primary language subtag so that a config entry of
    ``"en"`` matches both ``"en"`` and ``"en-us"`` (or ``"en-US"`` from the site).
    An unknown/empty *language* is allowed by default to avoid silently dropping
    content whose language the site has not declared.
    """
    if not allowed_languages:
        return True
    if not language:
        return True
    page_primary = language.split("-")[0].lower()
    return page_primary in {lang.split("-")[0].lower() for lang in allowed_languages}


# ---------------------------------------------------------------------------
# Per-comic processing
# ---------------------------------------------------------------------------

def _process_comic(
    session: requests.Session,
    url: str,
    config: Dict,
    db_path: str,
    in_updated_feed: bool = False,
):
    """
    Check a single comic and update its CBZ if anything has changed.

    Always fetches fresh metadata (2 HTTP requests).
    Downloads ALL current remote pages only when a sync is needed:
      - comic has never been downloaded (page_count == 0)
      - remote page count differs from local
      - comic appeared in the updated feed (pages may have changed even if
        the count is the same)

    When syncing an existing CBZ, each page position is compared using a
    perceptual hash.  Pages that have been replaced or removed are archived
    inside the CBZ as ``NNN.ext.archive_K`` rather than deleted.
    """
    blacklist = config.get("blacklisted_tags", [])
    allowed_languages = config.get("allowed_languages", ["en"])
    exclude_censored = config.get("exclude_censored", False)
    output_dir = config["output_dir"]

    existing = db.get_comic(db_path, url)

    logger.info(f"Checking: {url}")
    try:
        meta = mp.fetch_comic_metadata(session, url)
    except Exception as exc:
        logger.error(f"  Failed to fetch metadata: {exc}")
        return

    title = meta["title"]
    tags = meta["tags"]
    author = meta["author"]
    language = meta["language"]
    image_urls = meta["image_urls"]
    remote_count = meta["page_count"]

    # Language filter check
    if not _is_language_allowed(language, allowed_languages):
        logger.warning(
            f"  '{title}' is language '{language}' — not in allowed_languages {allowed_languages}. Skipping."
        )
        db.upsert_comic(
            db_path, url,
            title=title, is_blacklisted=1, skip_reason="language", tags_json=json.dumps(tags),
        )
        return

    # Tag blacklist check
    if _is_blacklisted(tags, blacklist):
        logger.warning(f"  '{title}' matches blacklisted tag — skipping and marking.")
        db.upsert_comic(
            db_path, url,
            title=title, is_blacklisted=1, skip_reason="tag", tags_json=json.dumps(tags),
        )
        return

    # Censored content check
    if exclude_censored and _is_censored(tags):
        logger.warning(f"  '{title}' is censored content (minor characters) — skipping and marking.")
        db.upsert_comic(
            db_path, url,
            title=title, is_blacklisted=1, skip_reason="censored", tags_json=json.dumps(tags),
        )
        return

    local_count = (existing or {}).get("page_count", 0)
    raw_path = (existing or {}).get("cbz_path") or ""
    if raw_path:
        cbz_path = _resolve_abs_cbz(output_dir, raw_path)
    else:
        cbz_path = _cbz_path_for(output_dir, title, author)

    # Decide whether a sync is needed
    counts_changed = remote_count != local_count
    needs_sync = (local_count == 0) or counts_changed or in_updated_feed

    if not needs_sync:
        logger.info(f"  '{title}' is up to date ({local_count} pages).")
        db.update_comic_checked(
            db_path, url, local_count, json.dumps(tags),
            _make_rel_cbz(output_dir, cbz_path), title, meta["node_id"],
        )
        return

    # If the CBZ already exists on disk, check whether it already contains all
    # remote pages before downloading anything.
    if local_count == 0 and os.path.exists(cbz_path):
        disk_count = cbz.get_cbz_page_count(cbz_path)
        if disk_count >= remote_count:
            logger.info(
                f"  '{title}' CBZ exists on disk with {disk_count} page(s) "
                f"(remote: {remote_count}) — skipping download."
            )
            cbz_hash = cbz.hash_cbz(cbz_path)
            db.update_comic_checked(
                db_path, url, disk_count, json.dumps(tags),
                _make_rel_cbz(output_dir, cbz_path), title, meta["node_id"],
                cbz_hash=cbz_hash,
            )
            return
        # File exists but is incomplete — fall through to sync
        logger.info(
            f"  '{title}' CBZ exists but has {disk_count}/{remote_count} page(s) — syncing."
        )

    logger.info(
        f"  '{title}': downloading all {remote_count} page(s) for sync "
        f"(stored: {local_count}{', in updated feed' if in_updated_feed else ''})"
    )

    # Download ALL remote pages to a temporary directory
    with tempfile.TemporaryDirectory(prefix="multporn_dl_") as tmpdir:
        remote_image_paths: list = []
        failed = False

        for idx, img_url in enumerate(image_urls, start=1):
            logger.info(f"    [{idx}/{remote_count}] {img_url}")
            try:
                img_bytes = mp.download_image(session, img_url)
                ext = mp.get_image_extension(img_url)
                tmp_file = os.path.join(tmpdir, f"{idx:03d}.{ext}")
                with open(tmp_file, "wb") as f:
                    f.write(img_bytes)
                remote_image_paths.append((f"img.{ext}", tmp_file))
                sleep(1)
            except Exception as exc:
                logger.error(f"    Download failed for page {idx}: {exc}")
                failed = True
                break

        if failed or not remote_image_paths:
            logger.warn(f"  Download incomplete for '{title}' — CBZ not modified.")
            return

        metadata = {
            "title":   title,
            "series":  author,
            "web":     url,
            "tags":    tags,
            "writer":  author,
            "notes":   f"Downloaded by multporn-watcher v{VERSION}",
        }

        try:
            if local_count == 0:
                logger.info(f"  Creating CBZ: {cbz_path}")
                cbz.create_cbz(cbz_path, remote_image_paths, metadata)
                stats = {"live_pages": len(remote_image_paths), "pages_archived": 0, "pages_unchanged": 0}
            else:
                logger.info(f"  Syncing CBZ: {cbz_path}")
                stats = cbz.sync_cbz(cbz_path, remote_image_paths, metadata)
        except Exception as exc:
            logger.error(f"  Failed to write CBZ: {exc}")
            return

    archived = stats.get("pages_archived", 0)
    if archived:
        logger.info(f"  {archived} page(s) replaced/removed on site — archived inside CBZ.")

    final_count = stats["live_pages"]
    cbz_hash = cbz.hash_cbz(cbz_path)
    db.update_comic_checked(
        db_path, url, final_count, json.dumps(tags),
        _make_rel_cbz(output_dir, cbz_path), title, meta["node_id"],
        cbz_hash=cbz_hash,
    )
    logger.info(f"  Done: '{title}' — {final_count} live page(s), {archived} archived.")


# ---------------------------------------------------------------------------
# Hash backfill
# ---------------------------------------------------------------------------

def _backfill_hashes(config: Dict, db_path: str):
    """Hash any CBZ files that are missing a hash in the DB."""
    output_dir = config["output_dir"]
    missing = db.get_comics_missing_hash(db_path)
    if not missing:
        return
    logger.info(f"Backfilling hashes for {len(missing)} comic(s) …")
    for comic in missing:
        raw_path = comic.get("cbz_path") or ""
        if not raw_path:
            continue
        abs_path = _resolve_abs_cbz(output_dir, raw_path)
        if not os.path.exists(abs_path):
            logger.warning(f"  CBZ not found for hash backfill: {abs_path}")
            continue
        try:
            cbz_hash = cbz.hash_cbz(abs_path)
            db.update_comic_hash(db_path, comic["url"], cbz_hash)
            logger.info(f"  Hashed: {abs_path}")
        except Exception as exc:
            logger.exception(f"  Failed to hash {abs_path}: {exc}")


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------

def run_once(config: Dict, db_path: str):
    session = _make_session()
    output_dir = config["output_dir"]
    os.makedirs(output_dir, exist_ok=True)

    # Interval used for per-comic and per-artist re-check gating
    interval_days = config.get("full_check_interval_days", 28)
    full_check_interval_s = interval_days * 86400
    now = time.time()

    exclude_censored = config.get("exclude_censored", False)

    # If censored exclusion is currently OFF, lift any previously-applied censored
    # skip so those comics are re-evaluated against the current config this run.
    if not exclude_censored:
        db.clear_blacklisted_by_reason(db_path, "censored")

    # 1. Fetch the updated-comics feed to know which comics need attention
    updated_set: Set[str] = set()
    if config.get("check_updated_feed", True):
        logger.info("Fetching /updated_comics feed …")
        try:
            updated_set = mp.fetch_updated_feed(
                session, max_pages=config.get("updated_feed_pages", 2)
            )
            logger.info(f"  {len(updated_set)} recently updated comic(s) found in feed.")
        except Exception as exc:
            logger.warn(f"  Could not fetch updated feed: {exc}")

    # 2. Expand watched items: register comics, discover artist catalogues
    # Sources: config watched_items + optional watchlist text file
    all_watched = list(config.get("watched_items", []))
    watchlist_file = config["watchlist_file"]
    file_items = wl.load_watchlist(watchlist_file)
    if file_items:
        logger.info(f"Loaded {len(file_items)} URL(s) from watchlist file: {watchlist_file}")
    all_watched.extend(file_items)

    watched_comics: Set[str] = set()
    for item in all_watched:
        url = item["url"].rstrip("/")
        item_type = item.get("type", "")

        if item_type == "artist":
            artist_last_checked = db.get_artist_last_checked(db_path, url) or 0
            artist_elapsed_days = (now - artist_last_checked) / 86400
            if (now - artist_last_checked) < full_check_interval_s:
                logger.info(
                    f"Skipping artist page (checked {artist_elapsed_days:.1f}d ago, "
                    f"interval is {interval_days}d): {url}"
                )
                # Still collect any comics already in DB that belong to this artist
                # so they remain in watched_comics for the processing step.
                for comic in db.get_all_comics(db_path):
                    watched_comics.add(comic["url"])
                continue
            logger.info(f"Fetching artist catalogue: {url}")
            try:
                artist_comics = mp.fetch_artist_comics(session, url, exclude_censored=exclude_censored)
                logger.info(f"  Found {len(artist_comics)} comic(s).")
                for comic_url in artist_comics:
                    watched_comics.add(comic_url)
                    db.upsert_comic(db_path, comic_url)
                db.upsert_artist_checked(db_path, url)
            except Exception as exc:
                logger.error(f"  Failed to fetch artist page: {exc}")

        elif item_type == "comic":
            watched_comics.add(url)
            db.upsert_comic(db_path, url)

    # 3. Process every tracked comic that needs attention
    all_comics = db.get_all_comics(db_path)
    for comic in all_comics:
        comic_url = comic["url"]

        if comic["is_blacklisted"]:
            continue

        never_downloaded = comic["page_count"] == 0
        in_updated_feed  = comic_url in updated_set
        last_checked = comic.get("last_checked") or 0
        due_for_full_check = (now - last_checked) >= full_check_interval_s
        if due_for_full_check and not never_downloaded and not in_updated_feed:
            elapsed = (now - last_checked) / 86400
            logger.info(f"Full-check due for: {comic_url} (last checked {elapsed:.1f}d ago)")

        # Only hit the site if there's a reason to:
        #   - never downloaded yet (first run for this comic), OR
        #   - the updated feed says it changed, OR
        #   - per-comic full-check interval has elapsed
        if not (never_downloaded or in_updated_feed or due_for_full_check):
            continue

        _process_comic(session, comic_url, config, db_path, in_updated_feed=in_updated_feed)
        sleep(1)

    # Per-comic last_checked timestamps are updated by _process_comic() via
    # update_comic_checked() — no global full-check timestamp needed.

    _backfill_hashes(config, db_path)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def resolve_dirs() -> tuple:
    """
    Resolve config and data directories.

    Priority for each:
      1. ./config/  /  ./data/   — relative to CWD
      2. /config/   /  /data/    — filesystem root (Docker / system install)
      3. Create ./config/ and ./data/ in the CWD as a fallback.

    Returns (config_dir, data_dir) as absolute paths.
    """
    def _find_or_create(local_rel: str, root_abs: str) -> str:
        local_abs = os.path.abspath(local_rel)
        if os.path.isdir(local_abs):
            return local_abs
        if os.path.isdir(root_abs):
            return root_abs
        os.makedirs(local_abs, exist_ok=True)
        return local_abs

    config_dir = _find_or_create("./config", "/config")
    data_dir   = _find_or_create("./data",   "/data")
    return config_dir, data_dir


def run_watch(config: Dict, db_path: str):
    interval_s = int(config.get("poll_interval_minutes", 60)) * 60
    logger.info(
        f"Watch mode active — polling every {config['poll_interval_minutes']} minute(s)."
    )
    while True:
        logger.info("─── Poll cycle starting ───")
        try:
            run_once(config, db_path)
        except Exception as exc:
            logger.error(f"Poll cycle error: {exc}")
        logger.info(
            f"─── Poll complete. Next check in {config['poll_interval_minutes']} minute(s). ───"
        )
        sleep(interval_s)


def main():
    config_dir, data_dir = resolve_dirs()

    parser = argparse.ArgumentParser(
        description="Multporn Comic Watcher — keep local CBZ archives up-to-date.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                               # one-shot run, auto-detects config/ and data/
  python main.py --config ~/my_config.json    # custom config path
  python main.py --watchlist ~/watchlist.txt  # custom watchlist path
  python main.py --output ~/comics            # override output directory
  python main.py --watch                      # run continuously as a daemon
""",
    )
    parser.add_argument(
        "--config", default=os.path.join(config_dir, "config.json"),
        help="Path to config file (default: <config_dir>/config.json)",
    )
    parser.add_argument(
        "--watchlist", default=None,
        help="Path to watchlist file — overrides watchlist_file in config",
    )
    parser.add_argument(
        "--output",
        help="Override the output_dir from config",
    )
    parser.add_argument(
        "--db", default=os.path.join(data_dir, "watcher.db"),
        help="Path to the SQLite state database (default: <data_dir>/watcher.db)",
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="Run continuously, re-polling at poll_interval_minutes",
    )
    args = parser.parse_args()

    config = cfg.load_config(args.config)

    if args.output:
        config["output_dir"] = args.output

    # Resolve watchlist: CLI flag > config.json key > resolved config_dir default
    if args.watchlist:
        config["watchlist_file"] = args.watchlist
    elif not config.get("watchlist_file"):
        config["watchlist_file"] = os.path.join(config_dir, "watchlist.txt")

    db_path = args.db
    db.init_db(db_path)

    # Place the log file next to the database
    log_dir = os.path.dirname(os.path.abspath(db_path))
    logger.set_log_path(os.path.join(log_dir, "watcher.log"))

    # Load watchlist once for the startup summary; run_once/run_watch reload it.
    watchlist_path = config["watchlist_file"]
    watchlist_items = wl.load_watchlist(watchlist_path)

    logger.info(f"multporn-watcher v{VERSION}")
    logger.info(f"Output dir   : {config['output_dir']}")
    logger.info(f"Database     : {db_path}")
    logger.info(f"Full check   : every {config.get('full_check_interval_days', 28)} day(s)")
    logger.info(f"Config items : {len(config.get('watched_items', []))}")
    logger.info(f"Watchlist    : {watchlist_path} ({len(watchlist_items)} item(s))")

    if args.watch:
        run_watch(config, db_path)
    else:
        run_once(config, db_path)
        logger.info("One-shot run complete.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        logger.info("Interrupted by user.")
        sys.exit(0)
