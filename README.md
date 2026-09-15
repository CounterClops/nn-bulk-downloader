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
  (title, series, characters, curated and user tags, page count, source URL, author)
- **Metadata refresh** — when the site's tags, sections or characters change, the
  existing `ComicInfo.xml` is rewritten in place without re-downloading any pages
- **Tag blacklisting** — skip comics that match unwanted tags, with separate
  lists for the site's curated tags and its community-editable user tags
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
| `unwatched_retention_days` | `7` | Days a comic with no source left on the watchlist is kept before its database row is removed |
| `exclude_censored` | `false` | Never download a comic whose thumbnail is blurred on a watched artist's page |
| `blacklisted_tags` | `[]` | Comics whose **curated** tags match any entry here are skipped |
| `blacklisted_user_tags` | `[]` | Comics whose **user** tags match any entry here are skipped |
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

`ComicInfo.xml` includes: `Title`, `Series`, `Notes`, `Writer`, `Penciller`, `Tags`,
`Web` (source URL), `PageCount`, `Characters`, and `SeriesGroup`, emitted in the order
`ComicInfo.xsd` declares them.

The site's fields map on as follows:

| Site field | ComicInfo field |
| --- | --- |
| `Author:` / `Artist:` | `Writer` and `Penciller` |
| `Section:` | `Series` (first value), `SeriesGroup` (all values), `Tags` as `parody:` |
| `Characters:` | `Characters` |
| `Tags:` | `Tags` as `tag:` |
| `User tags:` | `Tags` as `other:` |

`Series` takes a single string, so a comic listed under several sections keeps the first
as its series and carries the full list in `SeriesGroup`. A comic with no section falls
back to its own title, so it stands alone in a library rather than being filed under its
artist.

ComicInfo offers a single `Tags` field, so the vocabularies that share it are marked with
a namespace prefix, the way other providers' ComicInfo files do:

```xml
<Tags>tag: Titfuck, tag: Oral, other: Ebony, other: AI Generated, parody: Others</Tags>
```

This keeps the site's curated `Tags:` distinguishable from the community-editable
`User tags:` once both are in the same field. Every field except the title and page count
is optional — a comic missing sections, characters or either tag list simply has those
entries left out, and the element is omitted entirely when nothing fills it.

Both kinds of comic page are read: site comics name their fields `field-author` and
`field-com-group`, while user content (`/mp<nodeid>`) uses `field-artist-term` and
`field-section-term`.

### Updating metadata in place

When a check finds no new pages but the site's metadata has changed, only the archive's
`ComicInfo.xml` is rewritten — the pages are left byte-for-byte untouched and nothing is
re-downloaded. The replacement entry and a fresh central directory are written over the
old central directory, so a metadata refresh moves a few hundred bytes rather than
repacking an archive that may be hundreds of megabytes. The previous entry is left behind
as an unreferenced hole, which the next full sync reclaims.

Refreshes ride the normal check schedule, so a comic picks up changed metadata the next
time it appears in the updated feed or its `full_check_interval_days` elapses.

## How bandwidth is kept low

1. **`/updated_comics` feed** — only 1–2 lightweight pages scraped per cycle.
2. **Per-comic XML** (`/juicebox/xml/…`) — fetched **only** for comics that appear in
   the updated feed or have never been downloaded. Most tracked comics generate **zero
   extra requests** per poll.
3. **Polite delays** — 1 s between image downloads, 2 s between page scrapes.

## Censored comics

The site marks a censored comic by blurring its thumbnail on artist listing pages. That
blur is the only signal used: there is no tag-based censoring, and a comic's own page
carries no trace of it. With `exclude_censored` on, a comic blurred on any watched
artist's page is never downloaded.

Because the blur only appears on listings, a verdict is only as fresh as the last time
that artist's page was read, and artist pages are normally read once per
`full_check_interval_days`. A comic falls due on its own schedule, so it can need
downloading long after its artist was last read. Before any page is downloaded, every
watched artist listing that comic is therefore read again, unless it was already read
this cycle:

- **Blurred on any of those pages** → recorded as censored and not downloaded, whichever
  artist is read first.
- **An artist page cannot be read** → the download is refused, and the comic stays due so
  the next cycle tries again. Not being able to confirm the verdict is not treated as
  permission.

Each artist page is read at most once per cycle, and only for comics that actually need
pages downloaded; a metadata-only check never triggers one. A comic that becomes censored
after it was downloaded stops being updated, but its CBZ is left on disk.

A directly watched comic that no watched artist lists has no listing to judge it by, so it
downloads as normal.

## Removing entries from the watchlist

Every comic is linked to the watchlist entries that provide it: an artist entry links
every comic its page lists, and a direct comic entry links itself. A comic can have any
number of sources — if two watched artists both list it, removing one leaves it linked to
the other, and it carries on being monitored.

Once a comic has **no** source left on the watchlist:

1. **It stops being checked** from the next poll cycle.
2. **Its row is kept for `unwatched_retention_days`** (default 7). Re-adding a source in
   that window picks it straight back up — a re-added artist's page is fetched on the
   next cycle regardless of the full-check interval.
3. **The row is then deleted** by the cleanup phase at the end of a poll cycle. The CBZ
   file is never touched.

Links are only added as sources are read, never removed because a listing came back
shorter. A comic ends its link only when its source leaves the watchlist, so an artist
page served incompletely cannot make comics look abandoned.

Cleanup is skipped for a cycle, leaving every row as it is, when:

- **a watched source failed to refresh** — it may still provide comics it has not linked
  yet. The skipped cycle's log names the sources that failed. An artist page answering
  404 or 410 no longer exists, so it counts as providing nothing rather than as a failure
  and does not hold cleanup back.
- **the watchlist yields no sources at all** — a missing watchlist file reads as empty,
  and would otherwise schedule the whole library for deletion.

## State database

`watcher.db` (SQLite) tracks each comic's URL, page count, CBZ path, tags, which
watchlist entries provide it, and whether it has been blacklisted. Every column is
derived from the site or the CBZ on disk, so it is safe to delete and start fresh: the
watcher re-discovers every comic, and one whose CBZ already holds all its pages is
matched to that file rather than re-downloaded. A comic whose title or author has changed
on the site since its CBZ was written maps to a new filename, and is downloaded again
under it.
