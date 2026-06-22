# CODEBUDDY.md This file provides guidance to CodeBuddy when working with code in this repository.

## Project Overview

Two-component Python project for scraping X (Twitter) timeline videos and serving them via a password-protected web UI:

1. **`x_timeline.py`** — CLI + library that scrapes X's private GraphQL API using cookie-based auth, and downloads videos from tweets.
2. **`app.py`** — Flask web server that serves the downloaded videos through a session-authenticated web UI with ratings, thumbnails, and in-browser downloading.
3. **`storage.py`** — Storage abstraction layer supporting local filesystem and Tencent Cloud COS backends.

- **Runtime**: Python 3.11
- **Key dependencies**: `httpx`, `flask`, `bcrypt`, `opencv-python-headless` (`cv2`), `wcwidth`, `cos-python-sdk-v5`
- **Credentials**: injected via environment variables `X_AUTH_TOKEN` and `X_CT0` (see `.env.example`)

## Commands

```bash
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

# Deploy (push → remote pull → restart)
./deploy.sh
./deploy.sh "commit message"   # also commits locally first
```

## Architecture

### Storage Layer

`storage.py` provides a `StorageBackend` abstract class with two implementations:

- **`LocalStorage`** — Original local filesystem approach. Files stored under `videos/{author}/{filename}`. URLs are `/videos/{author}/{filename}` paths served by Flask.
- **`CosStorage`** — Tencent Cloud COS object storage. Files stored with `videos/` prefix in a private bucket. Access via presigned URLs (1h for videos, 5min for thumbnails).

Selected via `STORAGE_BACKEND` env var (`"local"` default, `"cos"` for COS). Singleton via `get_storage()`.

| Method | Local | COS |
|---|---|---|
| `upload_file(key, path, content_type)` | `shutil.copy2` to `videos/{key}` | `client.upload_file()` with multipart |
| `upload_bytes(key, data, content_type)` | `Path.write_bytes()` | `client.put_object()` |
| `exists(key)` | `Path.is_file()` | `client.object_exists()` |
| `delete(key)` | `Path.unlink()` | `client.delete_object()` |
| `list_objects(prefix)` | `glob(*/*)` | `client.list_objects()` with pagination |
| `get_url(key, expires)` | `/videos/{key}` | `client.get_presigned_download_url()` |
| `get_size(key)` | `Path.stat().st_size` | `client.head_object()` → Content-Length |
| `total_size(prefix)` | Sum mp4 file sizes | Sum mp4 objects from list |
| `get_disk_free()` | `shutil.disk_usage()` | `"∞"` |

### Data Flow

```
x_timeline.py  ──(downloads)──▶  storage backend (local or COS)
                                           │
                                           ▼
                                       app.py (Flask)
                                  ─────────────────────────────────
                                  /                → index (infinite scroll, all videos)
                                  /authors         → browse by author
                                  /author/<n>      → all videos by author
                                  /timeline        → fetch 20 tweets + download UI
                                  /bookmarks       → fetch 20 bookmarks + download UI
                                  /user/<name>     → user-specific timeline
                                  /downloader      → single-tweet URL download
                                  /liked           → rated videos (sorted by score)
                                  /play/<a>/<f>    → video player + rating + delete
                                  /videos/<a>/<f>  → raw file serving (local) / redirect to presigned URL (COS)
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
- **Security headers**: `add_security_headers()` adds CSP, X-Frame-Options, X-Content-Type-Options etc. to all non-passthrough responses. CSP dynamically includes COS domain when using CosStorage.
- **Path safety**: `_safe_segment()` and `_safe_filename()` validate all URL parameters to prevent path traversal.
- **Thumbnail generation**: `ensure_thumbnail(author, filename)` uses OpenCV to extract first frame; for COS, downloads mp4 to temp → extracts frame → uploads jpg → cleans temp.
- **Video scanning**: `get_all_videos()` uses `store.list_objects()` to enumerate mp4/jpg files; parses keys into `{author}/{tweet_id}_{index}`.
- **URL generation**: `_video_url()`, `_thumb_url()`, `_annotate_urls()` generate presigned URLs (COS) or local paths (local).
- **Timeline/Bookmarks/User timeline**: Each has an initial page route + a `/more` POST endpoint for infinite scroll (JSON `{cursor}`); `mark_downloaded()` annotates which videos already exist in storage.
- **Downloads**: `/timeline/download` starts a background thread via `_do_download()` which downloads to temp file → uploads to storage → generates thumbnail → cleans temp; `/timeline/progress/<task_id>` streams SSE progress events.
- **Ratings**: `/rate/<author>/<filename>` POST (1–5 stars, 0 = unset); `/liked` shows rated videos sorted by score desc. `ratings.json` stores `author/filename → score`; `load_ratings()` / `save_ratings()` protected by `_ratings_lock`.
- **Deletion**: `/delete/<author>/<filename>` POST; removes mp4 + jpg from storage + rating entry.
- **User ID cache**: `_user_id_cache` dict avoids repeated `get_user_id()` calls for `/user/<screen_name>` routes.

### Key Functions

| Function | File | Description |
|---|---|---|
| `get_storage()` | `storage.py` | Returns singleton StorageBackend (local or COS) |
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
| `download_video(tweet, video, index)` | `x_timeline.py` | Streams highest-bitrate MP4 to `DOWNLOAD_DIR/{user}/`; skips if exists (CLI only) |
| `ensure_thumbnail(author, filename)` | `app.py` | OpenCV first-frame extraction; COS mode: download→extract→upload→clean |
| `get_all_videos()` | `app.py` | Lists objects from storage; returns sorted by mtime |
| `mark_downloaded(tweets)` | `app.py` | Annotates each tweet's videos with `downloaded: bool` |
| `_do_download(task_id, ...)` | `app.py` | Background thread: download→upload to storage→thumbnail→cleanup |
| `_annotate_urls(videos)` | `app.py` | Adds `thumb_url` (presigned or local) to video dicts |
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

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `X_AUTH_TOKEN` | Yes | — | X auth cookie |
| `X_CT0` | Yes | — | X CSRF token |
| `SECRET_KEY` | Yes* | users.json | Flask session secret key |
| `STORAGE_BACKEND` | No | `local` | `"local"` or `"cos"` |
| `COS_REGION` | COS only | — | e.g. `ap-beijing` |
| `COS_SECRET_ID` | COS only | — | Tencent Cloud SecretId |
| `COS_SECRET_KEY` | COS only | — | Tencent Cloud SecretKey |
| `COS_BUCKET` | COS only | — | Bucket name with appid |
| `COS_SCHEME` | No | `https` | COS connection scheme |
| `FLASK_DEBUG` | No | `false` | Enable debug mode |

## Data Files

| File | Schema | Purpose |
|---|---|---|
| `users.json` | `{"secret_key": "...", "users": {"<name>": "<bcrypt hash>"}}` | Auth credentials (gitignored) |
| `ratings.json` | `{"author/filename": score}` | Video ratings 1–5 (gitignored) |
| `.query_id_cache.json` | `{"query_id": "...", "user_tweets_query_id": "...", "bookmarks_query_id": "...", "tweet_detail_query_id": "..."}` | Cached GraphQL queryIds per operation |
| `videos/{user}/{tweet_id}_{index}.mp4` | — | Downloaded videos (local mode, gitignored) |
| `videos/{user}/{tweet_id}_{index}.jpg` | — | Auto-generated thumbnails (local mode) |

## Deployment

- **Server**: `ubuntu@h1.tomatochen.top:22`
- **Project path**: `~/x_videos_server`
- **Service**: `x_videos_server.service` (systemd)
- `deploy.sh` runs push → remote pull → restart.

## Agent skills

### Issue tracker

Issues live in GitHub Issues (`github.com/dotomato/x_videos_server`). See `docs/agents/issue-tracker.md`.

### Triage labels

Uses default label vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context repo — one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
