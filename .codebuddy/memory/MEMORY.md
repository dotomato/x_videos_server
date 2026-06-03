# MEMORY.md

## Project: x_videos_server
- Two-component Python project: `x_timeline.py` (CLI + X GraphQL scraper) + `app.py` (Flask video web UI)
- Runtime: Python 3.11, deployed on `ubuntu@h1.tomatochen.top:22` via systemd
- Tests are fully offline (mocked), must pass before deploy (`deploy.sh` enforces this)
- Four separate GraphQL operations (HomeTimeline, UserTweets, Bookmarks, TweetDetail) with independent queryId caching in `.query_id_cache.json`
- `_parse_instructions()` is the shared parser reused across all timeline types

## CODEBUDDY.md
- Updated 2026-06-03: refreshed to match current codebase (added /user, /downloader, /rate, /delete routes; separated queryId caching docs; updated key functions)
