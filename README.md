# multporn-watcher

A background watcher that monitors [multporn.net](https://multporn.net) comics and artists,
downloads pages, and keeps local **CBZ archives** up-to-date — complete with a valid
`ComicInfo.xml` (Kavita / Komga compatible).

## Features

- Watch individual **comics** or entire **artist catalogues**
- Checks the site's [`/updated_comics`](https://multporn.net/updated_comics) feed to detect
  changes without polling every tracked comic individually
- Downloads only **new pages** — never re-downloads what you already have
- Stores each comic as a **CBZ** with a correctly formatted `ComicInfo.xml`
  (title, series, tags, page count, source URL, author)
- **Tag blacklisting** — skip comics that match unwanted tags
- **Daemon mode** (`--watch`) for continuous background monitoring
- Respects site bandwidth: uses the lightweight update feed + per-comic XML endpoint,
  with polite `sleep()` pauses between requests
- State stored in a local **SQLite database** — safe to interrupt and resume at any time

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create your config file
cp config.example.json config.json
# Edit config.json if needed (output_dir, poll interval, blacklisted tags…)

# 3. Add URLs to watch — just paste one URL per line
cp watchlist.example.txt watchlist.txt
# Edit watchlist.txt — add your comics and artist pages

# 4. Run once
python main.py

# 5. Or keep it running as a watcher daemon
python main.py --watch
```

## Adding comics / artists

The easiest way is the **watchlist file** — just paste URLs, one per line:

```
# watchlist.txt
https://multporn.net/comics/some-comic
https://multporn.net/comic_author/some-artist   # all their comics discovered automatically
https://multporn.net/hentai_manga/another-comic
```

The URL type (`comic` or `artist`) is **detected automatically** from the URL path.  
Comments (`#`) and blank lines are ignored.

You can still use `watched_items` in `config.json` if you prefer JSON, or use both — they are merged.

## Docker

```bash
# 1. Create local directories
mkdir -p config data media

# 2. Copy example files
cp config.example.json config/config.json
cp watchlist.example.txt config/watchlist.txt
# Edit config/watchlist.txt — add your URLs

# 3. Start the watcher
docker compose up -d

# 4. View logs
docker compose logs -f
```

The container uses three mounted volumes:

| Host path | Container path | Purpose |
|---|---|---|
| `./config/` | `/config/` | `config.json` + `watchlist.txt` |
| `./data/` | `/data/` | `watcher.db` + `watcher.log` |
| `./media/` | `/media/` | Downloaded `.cbz` files |

## Configuration

`config.json` (copy from `config.example.json`):

```json
{
  "output_dir": "./media",
  "poll_interval_minutes": 60,
  "blacklisted_tags": ["example_tag"],
  "check_updated_feed": true,
  "updated_feed_pages": 2,
  "watched_items": [
    {"type": "comic",  "url": "https://multporn.net/comics/my-favourite-comic"},
    {"type": "artist", "url": "https://multporn.net/comic_author/some-artist"}
  ]
}
```

| Key | Default | Description |
|---|---|---|
| `output_dir` | `./media` | Where CBZ files are saved |
| `watchlist_file` | `./watchlist.txt` | Path to the plain-text URL list |
| `poll_interval_minutes` | `60` | Interval between polls in `--watch` mode |
| `blacklisted_tags` | `[]` | Comics whose tags match any entry here are skipped |
| `check_updated_feed` | `true` | Fetch `/updated_comics` each cycle to avoid checking every comic |
| `updated_feed_pages` | `2` | How many feed pages to scan per cycle |
| `watched_items` | `[]` | Optional JSON list of `{"type": "comic"/"artist", "url": "..."}` entries |

### Adding items

**Preferred:** edit `watchlist.txt` — one URL per line, type auto-detected.  
**Alternative:** add to `watched_items` in `config.json` with explicit `type` field.

| Type | Example URL |
|---|---|
| Single comic | `https://multporn.net/comics/some-title` |
| Artist page | `https://multporn.net/comic_author/artist-name` |

For `type: artist`, **all comics** on that artist's page are discovered automatically and
added to the tracking database.

## CLI options

```
python main.py [--config PATH] [--output DIR] [--db PATH] [--watch]

  --config PATH   Config file to use          (default: ./config.json)
  --output DIR    Override output_dir          (overrides config value)
  --db PATH       SQLite state database path   (default: ./watcher.db)
  --watch         Run continuously as a daemon
```

## CBZ structure

Each comic is stored as `<output_dir>/<sanitized_title>.cbz` containing:

```
001.jpg
002.png
...
ComicInfo.xml
```

`ComicInfo.xml` includes: `Title`, `Series` (artist), `Web` (source URL), `Tags`,
`PageCount`, `Writer`, `Penciller`, and `Notes`.

## How bandwidth is kept low

1. **`/updated_comics` feed** — only 1–2 lightweight pages scraped per cycle.
2. **Per-comic XML** (`/juicebox/xml/…`) — fetched **only** for comics that appear in
   the updated feed or have never been downloaded. Most tracked comics generate **zero
   extra requests** per poll.
3. **Polite delays** — 1 s between image downloads, 2 s between page scrapes.

## State database

`watcher.db` (SQLite) tracks each comic's URL, page count, CBZ path, tags, and whether
it has been blacklisted.  Safe to delete and start fresh — the watcher will re-discover
and re-download everything.
