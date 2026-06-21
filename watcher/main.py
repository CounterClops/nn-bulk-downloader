#!/usr/bin/env python3
"""
multporn-watcher — monitor comics/artists on multporn.net and keep local
CBZ archives up-to-date.

Usage:
  python main.py [--config PATH] [--output DIR] [--db PATH] [--watch]
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


def _cbz_path_for(output_dir: str, title: str) -> str:
    return os.path.join(output_dir, cbz.sanitize_filename(title) + ".cbz")


def _is_blacklisted(tags: list, blacklist: list) -> bool:
    tags_lower = {t.lower() for t in tags}
    return any(b.lower() in tags_lower for b in blacklist)


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
    image_urls = meta["image_urls"]
    remote_count = meta["page_count"]

    # Tag blacklist check
    if _is_blacklisted(tags, blacklist):
        logger.warn(f"  '{title}' matches blacklisted tag — skipping and marking.")
        db.upsert_comic(
            db_path, url,
            title=title, is_blacklisted=1, tags_json=json.dumps(tags),
        )
        return

    local_count = (existing or {}).get("page_count", 0)
    cbz_path = (
        (existing or {}).get("cbz_path") or _cbz_path_for(output_dir, title)
    )

    # Decide whether a sync is needed
    counts_changed = remote_count != local_count
    needs_sync = (local_count == 0) or counts_changed or in_updated_feed

    if not needs_sync:
        logger.info(f"  '{title}' is up to date ({local_count} pages).")
        db.update_comic_checked(
            db_path, url, local_count, json.dumps(tags), cbz_path, title, meta["node_id"]
        )
        return

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
    db.update_comic_checked(
        db_path, url, final_count, json.dumps(tags), cbz_path, title, meta["node_id"]
    )
    logger.info(f"  Done: '{title}' — {final_count} live page(s), {archived} archived.")


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------

def run_once(config: Dict, db_path: str):
    session = _make_session()
    output_dir = config["output_dir"]
    os.makedirs(output_dir, exist_ok=True)

    # Determine whether this cycle is a full check
    interval_days = config.get("full_check_interval_days", 28)
    last_full_check = float(db.get_setting(db_path, "last_full_check", "0"))
    elapsed_days = (time.time() - last_full_check) / 86400
    is_full_check = elapsed_days >= interval_days
    if is_full_check:
        logger.info(
            f"Full-check cycle triggered "
            f"(last was {elapsed_days:.1f} days ago, interval is {interval_days} days). "
            f"All tracked comics will be verified directly."
        )

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
    watchlist_file = config.get("watchlist_file", "./watchlist.txt")
    file_items = wl.load_watchlist(watchlist_file)
    if file_items:
        logger.info(f"Loaded {len(file_items)} URL(s) from watchlist file: {watchlist_file}")
    all_watched.extend(file_items)

    watched_comics: Set[str] = set()
    for item in all_watched:
        url = item["url"].rstrip("/")
        item_type = item.get("type", "")

        if item_type == "artist":
            logger.info(f"Fetching artist catalogue: {url}")
            try:
                artist_comics = mp.fetch_artist_comics(session, url)
                logger.info(f"  Found {len(artist_comics)} comic(s).")
                for comic_url in artist_comics:
                    watched_comics.add(comic_url)
                    db.upsert_comic(db_path, comic_url)
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

        # Only hit the site if there's a reason to:
        #   - never downloaded yet (first run for this comic), OR
        #   - the updated feed says it changed, OR
        #   - it's a scheduled full-check cycle
        if not (never_downloaded or in_updated_feed or is_full_check):
            continue

        _process_comic(session, comic_url, config, db_path, in_updated_feed=in_updated_feed)
        sleep(1)

    # Persist full-check timestamp after a successful full-check cycle
    if is_full_check:
        db.set_setting(db_path, "last_full_check", str(time.time()))
        logger.info("Full-check complete — timestamp saved to database.")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

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
    parser = argparse.ArgumentParser(
        description="Multporn Comic Watcher — keep local CBZ archives up-to-date.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                             # one-shot run with ./config.json
  python main.py --config ~/my_config.json  # custom config
  python main.py --output ~/comics          # override output directory
  python main.py --watch                    # run continuously as a daemon
""",
    )
    parser.add_argument(
        "--config", default="./config.json",
        help="Path to config file (default: ./config.json)",
    )
    parser.add_argument(
        "--output",
        help="Override the output_dir from config",
    )
    parser.add_argument(
        "--db", default="./watcher.db",
        help="Path to the SQLite state database (default: ./watcher.db)",
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="Run continuously, re-polling at poll_interval_minutes",
    )
    args = parser.parse_args()

    config = cfg.load_config(args.config)

    if args.output:
        config["output_dir"] = args.output

    db_path = args.db
    db.init_db(db_path)

    # Place the log file next to the database
    log_dir = os.path.dirname(os.path.abspath(db_path))
    logger.set_log_path(os.path.join(log_dir, "watcher.log"))

    logger.info(f"multporn-watcher v{VERSION}")
    logger.info(f"Output dir : {config['output_dir']}")
    logger.info(f"Database   : {db_path}")
    logger.info(f"Full check : every {config.get('full_check_interval_days', 28)} day(s)")
    logger.info(f"Config items: {len(config.get('watched_items', []))} | Watchlist: {config.get('watchlist_file', './watchlist.txt')}")

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
