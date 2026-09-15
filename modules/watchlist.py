"""
Loads a plain-text watchlist file.

Format:
  - One URL per line
  - Lines starting with # are comments
  - Inline # comments are also stripped
  - Blank lines are ignored

The URL type (comic / artist) is detected automatically from the URL path.
"""

import os
from typing import Dict, List

from modules import logger
from modules.multporn import detect_url_type, normalise_url


def load_watchlist(path: str) -> List[Dict]:
    """
    Parse a watchlist text file and return a list of
    {"url": str, "type": "comic" | "artist"} dicts.

    Returns an empty list if the file does not exist.
    """
    if not os.path.exists(path):
        return []

    items: List[Dict] = []
    seen: set = set()

    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            # Strip inline comments
            url = line.split("#")[0].strip()
            if not url:
                continue

            if not url.startswith("http"):
                logger.warn(f"watchlist line {lineno}: not a URL, skipping: {url!r}")
                continue

            url = normalise_url(url)
            if url in seen:
                continue
            seen.add(url)

            try:
                url_type = detect_url_type(url)
            except ValueError as exc:
                logger.warn(f"watchlist line {lineno}: {exc} — skipping")
                continue

            items.append({"url": url, "type": url_type})

    return items
