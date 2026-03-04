# X Videos Server

A self-hosted tool for fetching videos from your X (Twitter) home timeline and browsing them through a password-protected web UI.

## Features

- Fetch your X home timeline via X's GraphQL API (cookie-based auth)
- Download videos at the highest available bitrate
- Browse downloaded videos by author or recency
- Auto-generated video thumbnails (first-frame via OpenCV)
- Password-protected web interface with session-based login
- In-browser download with real-time progress (Server-Sent Events)
- Multi-user support with bcrypt-hashed passwords

## Project Structure

```
x_videos_server/
├── app.py              # Flask web server
├── x_timeline.py       # CLI script for timeline fetching & video download
├── manage_users.py     # User management utility
├── templates/          # HTML templates
│   ├── index.html
│   ├── author.html
│   ├── play.html
│   ├── login.html
│   └── timeline.html
├── static/
│   └── style.css
├── videos/             # Downloaded videos (auto-created)
│   └── {screen_name}/
│       ├── {tweet_id}_{index}.mp4
│       └── {tweet_id}_{index}.jpg  # auto-generated thumbnail
└── users.json          # User credentials (not in repo)
```

## Requirements

- Python 3.11+
- Dependencies: `flask`, `httpx`, `bcrypt`, `opencv-python`, `wcwidth`

Install dependencies:

```bash
pip install flask httpx bcrypt opencv-python wcwidth
```

## Configuration

### X Credentials

Edit the top of `x_timeline.py` (lines 21–26) and set your X account cookies:

```python
AUTH_TOKEN = "your_auth_token_here"
CT0        = "your_ct0_here"
```

To get these values:
1. Open x.com in your browser and log in
2. Open DevTools → Application → Cookies → `https://x.com`
3. Copy the values of `auth_token` and `ct0`

### Web App Users

Create `users.json` before running the web server:

```bash
python3.11 manage_users.py add <username>
```

This will prompt for a password and create the file if it doesn't exist.

## Usage

### CLI — Fetch Timeline

```bash
# Fetch 5 tweets (default)
python3.11 x_timeline.py

# Fetch N tweets
python3.11 x_timeline.py 20

# Fetch tweets and interactively download videos
python3.11 x_timeline.py --download

# Fetch N tweets and download videos
python3.11 x_timeline.py 20 --download
```

### Web Server

```bash
python3.11 app.py
```

Then open `http://localhost:5000` in your browser. All routes require login.

| Route | Description |
|---|---|
| `/` | Home: latest 10 videos + all authors |
| `/timeline` | Fetch 20 tweets from X, download videos in-browser |
| `/author/<name>` | All videos by a specific author |
| `/play/<author>/<file>` | Video player page |

### User Management

```bash
python3.11 manage_users.py list               # List all users
python3.11 manage_users.py add <username>     # Add a new user
python3.11 manage_users.py passwd <username>  # Change password
python3.11 manage_users.py del <username>     # Delete a user
```

## Notes

- `users.json` and `videos/` are excluded from the repository via `.gitignore`
- The X GraphQL `queryId` is cached in `.query_id_cache.json` and auto-refreshed when the API returns 400/403
- Thumbnails are generated on first access and cached alongside the video file
