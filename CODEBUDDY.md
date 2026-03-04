# CODEBUDDY.md

This file provides guidance to CodeBuddy Code when working with code in this repository.

## Project Overview

This repository has two components:

1. **`x_timeline.py`** — CLI script that scrapes the X (Twitter) home timeline via X's **private GraphQL API** using cookie-based authentication, and optionally downloads videos from tweets.
2. **`app.py`** — Flask web server that serves the downloaded videos through a password-protected web UI.

- **Runtime**: Python 3.11
- **Key dependencies**: `httpx` (async-capable HTTP client), `flask`, `bcrypt`, `opencv-python` (`cv2`), `wcwidth`

## Running the Scripts

```bash
# Fetch 5 tweets (default)
python3.11 x_timeline.py

# Fetch N tweets
python3.11 x_timeline.py 20

# Fetch tweets and download all videos
python3.11 x_timeline.py --download

# Fetch N tweets and download videos
python3.11 x_timeline.py 20 --download

# Run the Flask web server (serves on 0.0.0.0:5000)
python3.11 app.py
```

## User Management (Flask Web App)

Users are stored with bcrypt-hashed passwords in `users.json`. Manage them with:

```bash
python3.11 manage_users.py list
python3.11 manage_users.py add <username>
python3.11 manage_users.py passwd <username>
python3.11 manage_users.py del <username>
```

## Credentials Configuration

Credentials are hardcoded at the top of `x_timeline.py` (lines 21–29):

| Variable | Purpose |
|---|---|
| `AUTH_TOKEN` | X user `auth_token` cookie |
| `CT0` | X CSRF token (`ct0` cookie) |
| `BEARER_TOKEN` | Universal X Bearer token (fixed, same for all clients) |
| `DOWNLOAD_DIR` | Output directory for downloaded videos (default: `<project_dir>/videos`, relative to script location) |

To use different credentials, update `AUTH_TOKEN` and `CT0`. These can be extracted from browser DevTools on x.com (Application > Cookies).

## Architecture

### System Overview

```
x_timeline.py  ──(downloads)──▶  videos/{screen_name}/{tweet_id}_{index}.mp4
                                           │
                                           ▼
                                       app.py (Flask)
                                  ─────────────────────
                                  /           → index (latest + by-author)
                                  /timeline   → fetch 20 tweets + download UI
                                  /author/<n> → all videos by author
                                  /play/<a>/<f> → video player page
                                  /videos/<a>/<f> → raw file serving
```

### `x_timeline.py` Pipeline

```
CLI args → get_home_timeline() → parse_timeline() → print_tweets()
                 │                                         │
          get_query_id()                           download_video()
          make_headers()
```

### `app.py` Structure

- **Authentication**: Session-based login (`/login`, `/logout`) using bcrypt via `users.json`; all routes protected by `@login_required`
- **Thumbnail generation**: `ensure_thumbnail()` uses OpenCV to extract first frame of each `.mp4` as `.jpg` on-demand; thumbnails are cached alongside the video file
- **Video scanning**: `get_all_videos()` globs `videos/*/*.mp4`, parses filenames into `{author}/{tweet_id}_{index}`, and sorts by `mtime` descending
- **Timeline**: `/timeline` fetches 20 tweets from X on page load (server-side render); `/timeline/download` starts a background thread; `/timeline/progress/<task_id>` streams SSE progress events
- **Templates**: `templates/index.html`, `templates/author.html`, `templates/play.html`, `templates/login.html`, `templates/timeline.html`

### Key Functions

| Function | File | Lines | Description |
|---|---|---|---|
| `get_query_id()` | `x_timeline.py` | 57–62 | Returns cached queryId from `.query_id_cache.json`; falls back to hardcoded `"5HIFewm4IR4zjZoYSa1vBg"` |
| `fetch_query_id(client)` | `x_timeline.py` | 65–88 | Re-extracts `HomeTimeline` queryId from X's JS bundle; called automatically on 400/403 |
| `make_headers()` | `x_timeline.py` | 92–104 | Builds GraphQL request headers; generates random `x-client-uuid` per request |
| `extract_videos(legacy)` | `x_timeline.py` | 108–133 | Parses `extended_entities.media`; returns videos sorted by bitrate descending |
| `download_video(tweet, video, index)` | `x_timeline.py` | 137–171 | Streams highest-bitrate MP4 to `DOWNLOAD_DIR/{user}/`; skips if file exists |
| `get_home_timeline(count, cursor)` | `x_timeline.py` | 175–241 | Posts to GraphQL endpoint; auto-refreshes queryId on 400/403 and retries |
| `parse_timeline(data)` | `x_timeline.py` | 245–301 | Traverses nested GraphQL JSON; handles `TweetWithVisibilityResults` wrapper |
| `print_tweets(tweets, download)` | `x_timeline.py` | 305–388 | Tabular output with interactive download prompt |
| `ensure_thumbnail(mp4_path)` | `app.py` | 74–85 | OpenCV first-frame extraction to `.jpg`; returns `None` if frame read fails |
| `get_all_videos()` | `app.py` | 90–116 | Scans `videos/*/*.mp4`; generates thumbnails; returns list sorted by mtime |
| `_do_download(task_id, ...)` | `app.py` | 201–228 | Background thread: streams video to disk, updates `_download_tasks` dict |
| `/timeline` route | `app.py` | 231–235 | Calls `get_home_timeline(20)`, server-side renders `timeline.html` |
| `/timeline/progress/<id>` | `app.py` | 259–287 | SSE endpoint; polls `_download_tasks` every 300ms, emits JSON events |

### X API GraphQL Response Structure

```
data.home.home_timeline_urt.instructions
  → [type="TimelineAddEntries"].entries
  → content.itemContent[itemType="TimelineTweet"]
  → tweet_results.result
  → (unwrap TweetWithVisibilityResults if needed)
  → legacy (tweet data) + core.user_results.result (user data)
```

User fields may be in either `result.core` or `result.legacy` depending on API version — the code checks both.

## Video File Layout

```
videos/
  {screen_name}/
    {tweet_id}_{video_index}.mp4
    {tweet_id}_{video_index}.jpg   ← auto-generated thumbnail
```

Example: `videos/Uaijie/2029001949665001594_0.mp4`

## queryId Caching

`.query_id_cache.json` stores the last known valid `HomeTimeline` queryId. If the API returns 400/403, `fetch_query_id()` re-scrapes x.com's JS bundle and updates the cache. Delete this file to force a fresh fetch.

## `users.json` Schema

```json
{
  "secret_key": "<flask session secret>",
  "users": {
    "<username>": "<bcrypt hash>"
  }
}
```

## Deployment

- **Server**: `ubuntu@h1.tomatochen.top:22`
- **Project path**: `~/x_videos_server`
- **Service**: `x_videos_server.service` (systemd)

### Deploy script (`deploy.sh`)

```bash
./deploy.sh              # push + remote pull + restart
./deploy.sh "msg"        # commit locally, then push + deploy
```

### Manual steps

```bash
git push origin main
ssh -p 22 ubuntu@h1.tomatochen.top \
  "cd ~/x_videos_server && git pull origin main && sudo systemctl restart x_videos_server"
```
