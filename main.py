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
from typing import Dict, Optional, Set

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


def _comic_info_metadata(meta: Dict, url: str) -> Dict:
    """Build the ComicInfo payload for a comic from its freshly-fetched metadata."""
    return {
        "title":      meta["title"],
        "author":     meta["author"],
        "sections":   meta.get("sections") or [],
        "characters": meta.get("characters") or [],
        "tags":       meta.get("tags") or [],
        "user_tags":  meta.get("user_tags") or [],
        "web":        url,
        "notes":      f"Downloaded by multporn-watcher v{VERSION}",
    }


def _refresh_comic_info(cbz_path: str, metadata: Dict, title: str) -> bool:
    """Bring an existing CBZ's ComicInfo.xml up to date without touching pages.

    Returns True when the file was rewritten. Site metadata changes long after a
    comic stops gaining pages — sections and characters get filled in by editors
    over time — so a check that finds no new pages still propagates what it read.
    """
    try:
        rewritten = cbz.update_comic_info(cbz_path, metadata)
    except Exception as exc:
        logger.error(f"  Failed to update ComicInfo.xml for '{title}': {exc}")
        return False
    if rewritten:
        logger.info(f"  Metadata changed on site — updated ComicInfo.xml in place for '{title}'.")
    return rewritten


def _is_blacklisted(tags: list, blacklist: list) -> bool:
    tags_lower = {t.lower() for t in tags}
    return any(b.lower() in tags_lower for b in blacklist)


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


def _mark_skipped(db_path: str, url: str, title: str, tags: list, reason: str):
    """Record a filter decision against a comic.

    last_checked is stamped so the comic re-enters the normal full-check
    rotation: skip decisions are re-derived from freshly-fetched metadata on
    each interval rather than being frozen at the moment of discovery.
    """
    db.upsert_comic(
        db_path, url,
        title=title,
        is_blacklisted=1,
        skip_reason=reason,
        tags_json=json.dumps(tags),
        last_checked=time.time(),
    )


# ---------------------------------------------------------------------------
# Per-comic processing
# ---------------------------------------------------------------------------

def _parse_interval_days(config: Dict) -> int:
    """Parse and validate full_check_interval_days from config.

    Returns a non-negative integer, defaulting to 28 on missing or invalid values.
    """
    try:
        return max(0, int(config.get("full_check_interval_days", 28)))
    except (TypeError, ValueError):
        logger.warning("Invalid full_check_interval_days in config — defaulting to 28 days.")
        return 28


def _needs_sync(
    local_count: int,
    remote_count: int,
    in_updated_feed: bool,
    last_synced: float,
    full_check_interval_s: float,
    now: float,
) -> bool:
    """Return True when a CBZ sync is required.

    Sync is needed when:
    - the comic has never been downloaded (local_count == 0)
    - the remote page count has changed
    - the comic appears in the updated feed AND has not been synced within the
      configured interval (avoids re-downloading every poll cycle when the feed
      entry persists but the content has not actually changed)
    """
    if local_count == 0:
        return True
    if remote_count != local_count:
        return True
    if in_updated_feed:
        recently_synced = last_synced > 0 and (now - last_synced) < full_check_interval_s
        return not recently_synced
    return False


def _describe_last_checked(last_checked: float, now: float) -> str:
    """Phrase a comic's last check for the log.

    An unset timestamp means the comic has never been checked (or was queued for
    a recheck), and measuring from the epoch would report it as decades old.
    """
    if not last_checked:
        return "not yet checked"
    return f"last checked {(now - last_checked) / 86400:.1f}d ago"


def _needs_check(
    is_skipped: bool,
    skip_reason: Optional[str],
    never_downloaded: bool,
    in_updated_feed: bool,
    due_for_full_check: bool,
) -> bool:
    """Return True when a comic is worth spending HTTP requests on this cycle.

    A tracked comic is checked when it has never been downloaded, when the
    updated feed reports a change, or when its full-check interval has elapsed.

    A skipped comic is checked on the same terms minus *never_downloaded*:
    every skipped comic has a page count of zero, so honouring it there would
    re-fetch the whole skip list on every poll. The remaining triggers still
    bring it round on each interval, so a filter decision tracks the site's
    current metadata instead of being frozen at the moment of discovery.

    A censored comic is the exception: that verdict comes from the artist
    listing's blur marker, which its own page does not carry, so fetching it
    would cost two requests and learn nothing. Artist discovery reconciles it.
    """
    if is_skipped:
        if skip_reason == db.CENSORED_SKIP_REASON:
            return False
        return due_for_full_check or in_updated_feed
    return never_downloaded or due_for_full_check or in_updated_feed


def _process_comic(
    session: requests.Session,
    url: str,
    config: Dict,
    db_path: str,
    full_check_interval_s: float,
    in_updated_feed: bool = False,
    is_direct_watch: bool = False,
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
    user_tag_blacklist = config.get("blacklisted_user_tags", [])
    allowed_languages = config.get("allowed_languages", ["en"])
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
    user_tags = meta.get("user_tags") or []
    comic_info = _comic_info_metadata(meta, url)

    # Language filter check
    if not _is_language_allowed(language, allowed_languages):
        logger.warning(
            f"  '{title}' is language '{language}' — not in allowed_languages {allowed_languages}. Skipping."
        )
        _mark_skipped(db_path, url, title, tags, "language")
        return

    # Tag blacklist check — scoped to bulk-discovered comics only; comics the
    # user explicitly watches directly (a single-comic watchlist/config entry)
    # are exempt, regardless of their tags.
    if not is_direct_watch and _is_blacklisted(tags, blacklist):
        logger.warning(f"  '{title}' matches blacklisted tag — skipping and marking.")
        _mark_skipped(db_path, url, title, tags, db.TAG_SKIP_REASON)
        return

    # User tags are community-editable, so they get their own list rather than
    # sharing blacklisted_tags: the same word can be a deliberate filter in the
    # curated vocabulary and noise in the community one.
    if not is_direct_watch and _is_blacklisted(user_tags, user_tag_blacklist):
        logger.warning(f"  '{title}' matches blacklisted user tag — skipping and marking.")
        _mark_skipped(db_path, url, title, tags, db.USER_TAG_SKIP_REASON)
        return

    # Every filter passed. If an earlier run skipped this comic, that decision
    # no longer holds — the site's metadata or the config has changed since —
    # so lift it and let the download proceed.
    # A censored skip is not ours to lift: nothing on this page carries the
    # site's blur marker, so only artist discovery can revisit that verdict.
    previous_skip_reason = (existing or {}).get("skip_reason")
    if (existing or {}).get("is_blacklisted") and previous_skip_reason != db.CENSORED_SKIP_REASON:
        logger.info(
            f"  '{title}' no longer matches the '{previous_skip_reason}' filter "
            f"— resuming downloads."
        )
        db.clear_comic_skip(db_path, url)

    local_count = (existing or {}).get("page_count", 0)
    raw_path = (existing or {}).get("cbz_path") or ""
    if raw_path:
        cbz_path = _resolve_abs_cbz(output_dir, raw_path)
    else:
        cbz_path = _cbz_path_for(output_dir, title, author)

    # Decide whether a sync is needed
    now = time.time()
    last_synced = (existing or {}).get("last_synced") or 0
    needs_sync = _needs_sync(
        local_count=local_count,
        remote_count=remote_count,
        in_updated_feed=in_updated_feed,
        last_synced=last_synced,
        full_check_interval_s=full_check_interval_s,
        now=now,
    )

    if not needs_sync:
        if in_updated_feed:
            logger.info(
                f"  '{title}' in updated feed — page count unchanged, "
                f"last synced {(now - last_synced) / 86400:.1f}d ago — skipping re-download."
            )
        else:
            logger.info(f"  '{title}' is up to date ({local_count} pages).")
        refreshed = _refresh_comic_info(cbz_path, comic_info, title)
        db.update_comic_checked(
            db_path, url, local_count, json.dumps(tags),
            _make_rel_cbz(output_dir, cbz_path), title, meta["node_id"],
            cbz_hash=cbz.hash_cbz(cbz_path) if refreshed else None,
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
            _refresh_comic_info(cbz_path, comic_info, title)
            cbz_hash = cbz.hash_cbz(cbz_path)
            db.update_comic_checked(
                db_path, url, disk_count, json.dumps(tags),
                _make_rel_cbz(output_dir, cbz_path), title, meta["node_id"],
                cbz_hash=cbz_hash,
                last_synced=time.time(),
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

    # If a previous run recorded a page failure, probe that page first using
    # the freshly-fetched URL (which may have been fixed upstream).
    failed_page = (existing or {}).get("failed_page")
    if failed_page and 1 <= failed_page <= remote_count:
        probe_url = image_urls[failed_page - 1]
        logger.info(f"  Probing previously-failed page {failed_page}: {probe_url}")
        try:
            mp.download_image(session, probe_url)
        except Exception as exc:
            logger.error(
                f"  Page {failed_page} still failing ({exc}) — skipping download until it is fixed."
            )
            db.set_comic_failed_page(db_path, url, failed_page)
            return
        logger.info(f"  Page {failed_page} now OK — proceeding with full download.")
        db.set_comic_failed_page(db_path, url, None)

    # Download ALL remote pages to a temporary directory
    with tempfile.TemporaryDirectory(prefix="multporn_dl_") as tmpdir:
        remote_image_paths: list = []
        failed = False

        for idx, img_url in enumerate(image_urls, start=1):
            logger.info(f"    [{idx}/{remote_count}] {img_url}")
            try:
                img_bytes = mp.download_image(session, img_url)
                # get_image_extension() already validates against an allowlist and
                # never returns a dot-prefixed string; lstrip is a defensive no-op.
                ext = mp.get_image_extension(img_url).lstrip(".")
                tmp_file = os.path.join(tmpdir, f"{idx:04d}.{ext}")
                with open(tmp_file, "wb") as f:
                    f.write(img_bytes)
                # Build an arcname that preserves both the position (for correct
                # sort order in any CBZ reader) and the original filename.
                orig_stem = img_url.split("?")[0].rpartition("/")[-1].rpartition(".")[0]
                safe_stem = cbz.sanitize_filename(orig_stem) if orig_stem else f"page{idx}"
                arcname = f"{idx:04d}-{safe_stem}.{ext}"
                remote_image_paths.append((arcname, tmp_file))
                sleep(1)
            except Exception as exc:
                logger.error(f"    Download failed for page {idx}: {exc}")
                db.set_comic_failed_page(db_path, url, idx)
                failed = True
                break

        if failed or not remote_image_paths:
            logger.warn(f"  Download incomplete for '{title}' — CBZ not modified.")
            return

        try:
            if local_count == 0:
                logger.info(f"  Creating CBZ: {cbz_path}")
                cbz.create_cbz(cbz_path, remote_image_paths, comic_info)
                stats = {"live_pages": len(remote_image_paths), "pages_archived": 0, "pages_unchanged": 0}
            else:
                logger.info(f"  Syncing CBZ: {cbz_path}")
                stats = cbz.sync_cbz(cbz_path, remote_image_paths, comic_info)
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
        last_synced=time.time(),
    )
    # Clear the failure marker only after all persistence steps have succeeded.
    db.set_comic_failed_page(db_path, url, None)
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
    interval_days = _parse_interval_days(config)
    full_check_interval_s = interval_days * 86400
    now = time.time()

    # Censored exclusion is applied at discovery time only: artist listings hide
    # comics whose preview thumbnail carries the site's blur marker.
    exclude_censored = config.get("exclude_censored", False)

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
    direct_comic_urls: Set[str] = set()
    for item in all_watched:
        url = mp.normalise_url(item["url"])
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
                listing = mp.fetch_artist_comics(session, url)
                logger.info(
                    f"  Found {len(listing.comic_urls)} comic(s), "
                    f"{len(listing.censored_urls)} marked censored by the site."
                )
                for comic_url in listing.comic_urls:
                    watched_comics.add(comic_url)
                    db.upsert_comic(db_path, comic_url)
                # The listing is the only place the blur marker appears, so the
                # censored skip is re-derived here on every artist refresh.
                # With the setting off, an empty set releases anything it held.
                db.sync_censored_skips(
                    db_path,
                    listing.comic_urls,
                    listing.censored_urls if exclude_censored else set(),
                )
                db.upsert_artist_checked(db_path, url)
            except Exception as exc:
                logger.error(f"  Failed to fetch artist page: {exc}")

        elif item_type == "comic":
            watched_comics.add(url)
            direct_comic_urls.add(url)
            db.upsert_comic(db_path, url)

    # A comic previously discovered (and tag-blacklisted) via a bulk artist
    # listing may now be directly watched — the user's explicit intent
    # overrides the earlier bulk-scoped blacklist decision, so unblock it
    # for re-evaluation this run.
    if direct_comic_urls:
        db.clear_tag_blacklist_for_urls(db_path, list(direct_comic_urls))

    # 3. Process every tracked comic that needs attention
    all_comics = db.get_all_comics(db_path)
    for comic in all_comics:
        comic_url = comic["url"]

        is_skipped = bool(comic["is_blacklisted"])
        never_downloaded = comic["page_count"] == 0
        in_updated_feed  = comic_url in updated_set
        is_direct_watch  = comic_url in direct_comic_urls
        last_checked = comic.get("last_checked") or 0
        due_for_full_check = (now - last_checked) >= full_check_interval_s

        if not _needs_check(
            is_skipped=is_skipped,
            skip_reason=comic["skip_reason"],
            never_downloaded=never_downloaded,
            in_updated_feed=in_updated_feed,
            due_for_full_check=due_for_full_check,
        ):
            continue

        if is_skipped:
            logger.info(
                f"Re-checking skipped comic ({comic['skip_reason']}, "
                f"{_describe_last_checked(last_checked, now)}): {comic_url}"
            )
        elif due_for_full_check and not never_downloaded and not in_updated_feed:
            logger.info(
                f"Full-check due for: {comic_url} ({_describe_last_checked(last_checked, now)})"
            )

        _process_comic(
            session, comic_url, config, db_path, full_check_interval_s,
            in_updated_feed=in_updated_feed, is_direct_watch=is_direct_watch,
        )
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
