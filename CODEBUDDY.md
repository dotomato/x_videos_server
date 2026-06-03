# CODEBUDDY.md This file provides guidance to CodeBuddy when working with code in this repository.

## Project Overview

Two-component Python project for scraping X (Twitter) timeline videos and serving them via a password-protected web UI:

1. **`x_timeline.py`** — CLI + library that scrapes X's private GraphQL API using cookie-based auth, and downloads videos from tweets.
2. **`app.py`** — Flask web server that serves the downloaded videos through a session-authenticated web UI with ratings, thumbnails, and in-browser downloading.

- **Runtime**: Python 3.11
- **Key dependencies**: `httpx`, `flask`, `bcrypt`, `opencv-python-headless` (`cv2`), `wcwidth`, `pytest`, `pytest-mock`
- **Credentials**: injected via environment variables `X_AUTH_TOKEN` and `X_CT0` (see `.env.example`)

## Commands

```bash
# Run all tests (must pass before deployment)
python3.11 -m pytest tests/ -v

# Run a single test file
python3.11 -m pytest tests/test_app.py -v

# Run a specific test by name
python3.11 -m pytest tests/test_app.py::test_login_rate_limit -v

# Quick mode (minimal output)
python3.11 -m pytest tests/ -q

# Fetch 5 tweets (default) from CLI
python3.11 x_timeline.py

# Fetch N tweets, optionally with --download
python3.11 x_timeline.py 20 --download

# Run Flask web server (0.0.0.0:5000)
python3.11 app.py

# User management
python3.11 manage_users.py list
python3.11 manage_users.py add <username>
python3.11 manage_users.py passwd <username>
python3.11 manage_users.py del <username>

# Deploy (tests → push → remote pull → restart)
./deploy.sh
./deploy.sh "commit message"   # also commits locally first
```

## Architecture

### Data Flow

```
x_timeline.py  ──(downloads)──▶  videos/{screen_name}/{tweet_id}_{index}.mp4
                                           │
                                           ▼
                                       app.py (Flask)
                                  ─────────────────────────────────
                                  /                → index (latest + by-author)
                                  /timeline        → fetch 20 tweets + download UI
                                  /bookmarks       → fetch 20 bookmarks + download UI
                                  /user/<name>     → user-specific timeline
                                  /downloader      → single-tweet URL download
                                  /liked           → rated videos (sorted by score)
                                  /author/<n>      → all videos by author
                                  /play/<a>/<f>    → video player + rating + delete
                                  /videos/<a>/<f>  → raw file serving
```

### `x_timeline.py` Pipeline

```
CLI args → get_home_timeline() → parse_timeline() → print_tweets()
                 │                                         │
          get_query_id()                           download_video()
          make_headers()

Shared parsing: _parse_instructions() is reused by parse_timeline(),
                parse_bookmarks(), and parse_user_tweets()
```

Four separate GraphQL operations, each with its own queryId and fallback:
- **HomeTimeline** (`get_home_timeline` / `get_home_timeline_with_cursor`) — POST
- **UserTweets** (`get_user_timeline_with_cursor`) — GET, requires `get_user_id()` first
- **Bookmarks** (`get_bookmarks_with_cursor`) — POST
- **TweetDetail** (`get_tweet_by_id`) — GET, used by downloader page

All share the same `TWEET_FEATURES` dict and the same auto-refresh pattern: on 400/403, call `_fetch_query_id_for_operation()` to scrape x.com's JS bundle for the new queryId, then retry once.

### `app.py` Structure

- **Authentication**: Session-based login (`/login`, `/logout`) using bcrypt via `users.json`; all routes protected by `@login_required`. Login rate-limited: 5 failures → 5-min lockout per IP.
- **Security headers**: `add_security_headers()` adds CSP, X-Frame-Options, X-Content-Type-Options etc. to all non-passthrough responses.
- **Path safety**: `_safe_segment()` and `_safe_filename()` validate all URL parameters to prevent path traversal.
- **Thumbnail generation**: `ensure_thumbnail()` uses OpenCV to extract first frame of each `.mp4` as `.jpg` on-demand; thumbnails are cached alongside the video file.
- **Video scanning**: `get_all_videos()` globs `videos/*/*.mp4`, parses filenames into `{author}/{tweet_id}_{index}`, sorts by `mtime` descending.
- **Timeline/Bookmarks/User timeline**: Each has an initial page route + a `/more` POST endpoint for infinite scroll (JSON `{cursor}`); `mark_downloaded()` annotates which videos already exist locally.
- **Downloader**: `/downloader` page + `/api/tweet` POST endpoint; `_parse_tweet_url()` extracts tweet ID from x.com URL, `get_tweet_by_id()` fetches details.
- **Downloads**: `/timeline/download` starts a background thread via `_do_download()`; `/timeline/progress/<task_id>` streams SSE progress events (polls `_download_tasks` dict every 300ms).
- **Ratings**: `/rate/<author>/<filename>` POST (1–5 stars, 0 = unset); `/liked` shows rated videos sorted by score desc. `ratings.json` stores `author/filename → score`; `load_ratings()` / `save_ratings()` protected by `_ratings_lock`.
- **Deletion**: `/delete/<author>/<filename>` POST; removes mp4 + jpg + rating entry.
- **User ID cache**: `_user_id_cache` dict avoids repeated `get_user_id()` calls for `/user/<screen_name>` routes.

### Key Functions

| Function | File | Description |
|---|---|---|
| `get_query_id()` | `x_timeline.py` | Returns cached HomeTimeline queryId or fallback |
| `_fetch_query_id_for_operation()` | `x_timeline.py` | Generic queryId refresh: scrapes X JS bundle, writes to `.query_id_cache.json` |
| `make_headers()` | `x_timeline.py` | Builds GraphQL request headers with random `x-client-uuid` |
| `extract_videos(legacy)` | `x_timeline.py` | Parses `extended_entities.media`; returns videos sorted by bitrate desc |
| `_parse_instructions(instructions)` | `x_timeline.py` | Shared parser for all GraphQL instruction lists; handles `TweetWithVisibilityResults` wrapper |
| `parse_timeline(data)` | `x_timeline.py` | HomeTimeline response → `_parse_instructions()` |
| `parse_user_tweets(data)` | `x_timeline.py` | UserTweets response → `_parse_instructions()` |
| `parse_bookmarks(data)` | `x_timeline.py` | Bookmarks response → `_parse_instructions()` |
| `get_user_id(screen_name)` | `x_timeline.py` | UserByScreenName GraphQL lookup → numeric rest_id |
| `get_tweet_by_id(tweet_id)` | `x_timeline.py` | TweetDetail GraphQL → single tweet dict |
| `download_video(tweet, video, index)` | `x_timeline.py` | Streams highest-bitrate MP4 to `DOWNLOAD_DIR/{user}/`; skips if exists |
| `ensure_thumbnail(mp4_path)` | `app.py` | OpenCV first-frame extraction to `.jpg` |
| `get_all_videos()` | `app.py` | Scans `videos/*/*.mp4`; generates thumbnails; returns sorted by mtime |
| `mark_downloaded(tweets)` | `app.py` | Annotates each tweet's videos with `downloaded: bool` |
| `_do_download(task_id, ...)` | `app.py` | Background thread: streams video to disk, updates `_download_tasks` dict |
| `_parse_tweet_url(url)` | `app.py` | Extracts (screen_name, tweet_id) from x.com URL |

### X API GraphQL Response Structure

```
HomeTimeline:
  data.home.home_timeline_urt.instructions → _parse_instructions()

UserTweets:
  data.user.result.timeline_v2.timeline.instructions → _parse_instructions()

Bookmarks:
  data.bookmark_timeline_v2.timeline.instructions → _parse_instructions()

TweetDetail:
  data.threaded_conversation_with_injections_v2.instructions → _parse_instructions()

All share the same entry traversal:
  [type="TimelineAddEntries"].entries
  → content.itemContent[itemType="TimelineTweet"]
  → tweet_results.result
  → (unwrap TweetWithVisibilityResults if needed)
  → legacy (tweet data) + core.user_results.result (user data)
```

User fields may be in either `result.core` or `result.legacy` — the code checks both.

## Data Files

| File | Schema | Purpose |
|---|---|---|
| `users.json` | `{"secret_key": "...", "users": {"<name>": "<bcrypt hash>"}}` | Auth credentials (gitignored) |
| `ratings.json` | `{"author/filename": score}` | Video ratings 1–5 (gitignored) |
| `.query_id_cache.json` | `{"query_id": "...", "user_tweets_query_id": "...", "bookmarks_query_id": "...", "tweet_detail_query_id": "..."}` | Cached GraphQL queryIds per operation |
| `videos/{user}/{tweet_id}_{index}.mp4` | — | Downloaded videos (gitignored) |
| `videos/{user}/{tweet_id}_{index}.jpg` | — | Auto-generated thumbnails |

## Testing

Tests are fully offline — no network, X API, or real video files required. All external dependencies are mocked via `monkeypatch`.

| Test file | Coverage |
|---|---|
| `tests/test_x_timeline.py` | `extract_videos`, `_parse_instructions`, `parse_timeline`, `parse_bookmarks`, `parse_user_tweets`, queryId cache for all 4 operations, `make_headers`, `get_user_id`, `get_tweet_by_id`, HTTP error handling |
| `tests/test_app.py` | Path safety (`_safe_segment`, `_safe_filename`, `_parse_tweet_url`), all routes (400/404/200), login rate limiting, open-redirect fix, HTTP security headers, bcrypt password check, OpenCV thumbnail mock, video scanning/sorting, SSE progress stream, rating/delete endpoints |

**Tests must pass before deployment** — `deploy.sh` runs `pytest` automatically and aborts on failure.

## Deployment

- **Server**: `ubuntu@h1.tomatochen.top:22`
- **Project path**: `~/x_videos_server`
- **Service**: `x_videos_server.service` (systemd)
- `deploy.sh` runs tests → push → remote pull → restart. Aborts immediately if pytest fails.
