import json
import os
import sys
from typing import Dict

DEFAULTS: Dict = {
    "output_dir": "./media",
    "poll_interval_minutes": 60,
    "blacklisted_tags": [],
    "check_updated_feed": True,
    "updated_feed_pages": 2,
    "watchlist_file": "./watchlist.txt",
    "full_check_interval_days": 28,
    "watched_items": [],
}


def load_config(path: str) -> Dict:
    if not os.path.exists(path):
        print(f"Config file not found: {path}")
        print(f"Copy config.example.json to {path} and edit it.")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"Invalid JSON in config file: {e}")
            sys.exit(1)

    config = {**DEFAULTS, **data}
    _validate(config)
    return config


def _validate(config: Dict):
    errors = []

    if not isinstance(config.get("output_dir"), str):
        errors.append("'output_dir' must be a string path")

    interval = config.get("poll_interval_minutes")
    if not isinstance(interval, (int, float)) or interval < 1:
        errors.append("'poll_interval_minutes' must be a positive number")

    if not isinstance(config.get("blacklisted_tags"), list):
        errors.append("'blacklisted_tags' must be a list of strings")

    watched = config.get("watched_items")
    if not isinstance(watched, list):
        errors.append("'watched_items' must be a list")
    else:
        for i, item in enumerate(watched):
            if not isinstance(item, dict):
                errors.append(f"'watched_items[{i}]' must be an object")
                continue
            if item.get("type") not in ("comic", "artist"):
                errors.append(f"'watched_items[{i}].type' must be 'comic' or 'artist'")
            url = item.get("url", "")
            if not isinstance(url, str) or not url.startswith("http"):
                errors.append(f"'watched_items[{i}].url' must be a valid http(s) URL")

    if errors:
        print("Config validation errors:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)
