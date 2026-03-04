#!/usr/bin/env python3.11
"""
X Videos - Flask 视频展示网站
"""

import json
import re
import time
import uuid
import logging
import threading
import bcrypt
import httpx
from pathlib import Path
from functools import wraps
from flask import (
    Flask, render_template, send_from_directory, abort,
    session, redirect, url_for, request, Response, jsonify
)
import cv2
from x_timeline import get_home_timeline, get_home_timeline_with_cursor, get_user_id, get_user_timeline_with_cursor, DOWNLOAD_DIR

# ─── 日志 ──────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BASE_DIR = Path(__file__).parent
VIDEOS_DIR = BASE_DIR / "videos"
USERS_FILE = BASE_DIR / "users.json"


# ─── 配置加载 ─────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(USERS_FILE, encoding="utf-8") as f:
        return json.load(f)

config = load_config()
app.secret_key = config["secret_key"]


# ─── 认证 ─────────────────────────────────────────────────────────────────────

def check_password(username: str, password: str) -> bool:
    users = load_config()["users"]
    if username not in users:
        return False
    return bcrypt.checkpw(password.encode(), users[username].encode())


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if check_password(username, password):
            session["logged_in"] = True
            session["username"] = username
            next_url = request.args.get("next") or url_for("index")
            return redirect(next_url, 303)
        error = "用户名或密码错误"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─── 路径参数校验 ─────────────────────────────────────────────────────────────

# 只允许字母、数字、下划线、连字符，1-128 个字符
_SAFE_SEGMENT_RE = re.compile(r'^[A-Za-z0-9_\-]{1,128}$')


def _safe_segment(value: str) -> bool:
    """校验路径段是否安全，防止路径穿越攻击。"""
    return bool(_SAFE_SEGMENT_RE.match(value))


def _safe_filename(value: str) -> bool:
    """校验文件名（含扩展名）是否安全。
    允许格式：{stem}.{ext}，stem 只含安全字符，ext 限 mp4/jpg/ico。
    """
    if "." not in value:
        return False
    stem, _, ext = value.rpartition(".")
    return _safe_segment(stem) and ext.lower() in ("mp4", "jpg", "ico")


# ─── 缩略图 ───────────────────────────────────────────────────────────────────

def ensure_thumbnail(mp4_path: Path) -> Path | None:
    """若缩略图不存在，用 OpenCV 截取第一帧生成 jpg"""
    jpg_path = mp4_path.with_suffix(".jpg")
    if not jpg_path.exists():
        cap = cv2.VideoCapture(str(mp4_path))
        ret, frame = cap.read()
        cap.release()
        if ret:
            cv2.imwrite(str(jpg_path), frame)
        else:
            return None
    return jpg_path


# ─── 数据扫描 ─────────────────────────────────────────────────────────────────

def get_all_videos() -> list[dict]:
    """扫描 videos/*/*.mp4，确保缩略图存在，按 mtime 降序返回"""
    videos = []
    if not VIDEOS_DIR.exists():
        return videos

    for mp4 in VIDEOS_DIR.glob("*/*.mp4"):
        author = mp4.parent.name
        stem = mp4.stem  # e.g. "2029001949665001594_0"
        parts = stem.rsplit("_", 1)
        tweet_id = parts[0] if len(parts) == 2 else stem
        index = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0

        jpg = ensure_thumbnail(mp4)
        has_thumb = jpg is not None and jpg.exists()

        videos.append({
            "author": author,
            "tweet_id": tweet_id,
            "index": index,
            "mp4": mp4.name,
            "jpg": mp4.stem + ".jpg" if has_thumb else None,
            "mtime": mp4.stat().st_mtime,
        })

    videos.sort(key=lambda v: v["mtime"], reverse=True)
    return videos


def get_latest_videos(n: int = 10) -> list[dict]:
    return get_all_videos()[:n]


def get_latest_by_author() -> list[dict]:
    """每位作者取最新的一个视频，按作者名字母排序"""
    seen: dict[str, dict] = {}
    for v in get_all_videos():
        if v["author"] not in seen:
            seen[v["author"]] = v
    return sorted(seen.values(), key=lambda v: v["author"].lower())


def get_author_videos(author: str) -> list[dict]:
    """返回指定作者的所有视频（mtime 降序）"""
    return [v for v in get_all_videos() if v["author"] == author]


# ─── 路由 ─────────────────────────────────────────────────────────────────────

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(BASE_DIR / "static", "favicon.ico", mimetype="image/x-icon")


@app.route("/")
@login_required
def index():
    latest = get_latest_videos(10)
    by_author = get_latest_by_author()
    return render_template("index.html", latest=latest, by_author=by_author)


@app.route("/author/<name>")
@login_required
def author(name: str):
    if not _safe_segment(name):
        abort(400)
    videos = get_author_videos(name)
    if not videos:
        abort(404)
    return render_template("author.html", author=name, videos=videos)


@app.route("/play/<author>/<filename>")
@login_required
def play(author: str, filename: str):
    if not _safe_segment(author) or not _safe_filename(filename):
        abort(400)
    mp4_path = VIDEOS_DIR / author / filename
    if not mp4_path.is_file():
        abort(404)
    jpg = filename.rsplit(".", 1)[0] + ".jpg"
    back = request.referrer or url_for("author", name=author)
    return render_template("play.html", author=author, filename=filename,
                           src=f"/videos/{author}/{filename}",
                           thumb=f"/videos/{author}/{jpg}",
                           back=back)


@app.route("/delete/<author>/<filename>", methods=["POST"])
@login_required
def delete_video(author: str, filename: str):
    if not _safe_segment(author) or not _safe_filename(filename):
        abort(400)
    mp4_path = VIDEOS_DIR / author / filename
    if not mp4_path.is_file():
        abort(404)
    jpg_path = mp4_path.with_suffix(".jpg")
    mp4_path.unlink()
    if jpg_path.exists():
        jpg_path.unlink()
    logger.info("已删除视频: %s/%s", author, filename)
    return jsonify({"ok": True})


@app.route("/videos/<author>/<filename>")
@login_required
def serve_video(author: str, filename: str):
    if not _safe_segment(author) or not _safe_filename(filename):
        abort(400)
    author_dir = VIDEOS_DIR / author
    if not author_dir.is_dir():
        abort(404)
    return send_from_directory(author_dir, filename)


# ─── 时间线路由 ───────────────────────────────────────────────────────────────

# 全局下载任务字典 { task_id: {status, progress, total, done} }
_task_lock = threading.Lock()
_download_tasks: dict[str, dict] = {}


def _set_task(task_id: str, **kwargs):
    with _task_lock:
        if task_id not in _download_tasks:
            _download_tasks[task_id] = {}
        _download_tasks[task_id].update(kwargs)


def _do_download(task_id: str, user: str, tweet_id: str, video_url: str, video_index: int):
    """在后台线程中流式下载单个视频，实时更新进度。"""
    user_dir = DOWNLOAD_DIR / user
    user_dir.mkdir(parents=True, exist_ok=True)
    filename = user_dir / f"{tweet_id}_{video_index}.mp4"
    logger.info("[%s] 开始下载 user=%s tweet_id=%s index=%d", task_id[:8], user, tweet_id, video_index)
    logger.info("[%s] 目标路径: %s", task_id[:8], filename)
    logger.info("[%s] 视频 URL: %s", task_id[:8], video_url)

    if filename.exists():
        logger.info("[%s] 文件已存在，跳过", task_id[:8])
        _set_task(task_id, status="skipped", progress=1, total=1, done=True)
        return

    try:
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            with client.stream("GET", video_url) as r:
                logger.info("[%s] HTTP %d  Content-Length: %s", task_id[:8], r.status_code, r.headers.get("content-length", "unknown"))
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                _set_task(task_id, status="downloading", progress=0, total=total, done=False)
                downloaded = 0
                with open(filename, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=1024 * 64):
                        f.write(chunk)
                        downloaded += len(chunk)
                        _set_task(task_id, progress=downloaded, total=total)
        logger.info("[%s] 下载完成，共 %d 字节，保存至 %s", task_id[:8], downloaded, filename)
        _set_task(task_id, status="done", progress=total or 1, total=total or 1, done=True)
    except Exception as e:
        logger.error("[%s] 下载失败: %s", task_id[:8], e, exc_info=True)
        # 清理不完整文件
        if filename.exists():
            filename.unlink(missing_ok=True)
        _set_task(task_id, status="error", message=str(e), done=True)


def mark_downloaded(tweets: list[dict]) -> list[dict]:
    """为每条推文的每个视频标注是否已下载"""
    for t in tweets:
        for i, v in enumerate(t.get("videos", [])):
            path = VIDEOS_DIR / t["user"] / f"{t['id']}_{i}.mp4"
            v["downloaded"] = path.is_file()
    return tweets


@app.route("/timeline")
@login_required
def timeline():
    tweets, next_cursor = get_home_timeline_with_cursor(count=20)
    return render_template("timeline.html", tweets=mark_downloaded(tweets), next_cursor=next_cursor or "")


@app.route("/timeline/more", methods=["POST"])
@login_required
def timeline_more():
    data = request.get_json(force=True)
    cursor = data.get("cursor", "")
    if not cursor:
        return jsonify({"error": "missing cursor"}), 400
    tweets, next_cursor = get_home_timeline_with_cursor(count=20, cursor=cursor)
    return jsonify({"tweets": mark_downloaded(tweets), "next_cursor": next_cursor or ""})


# ─── 用户时间线路由 ────────────────────────────────────────────────────────────

# 用户 ID 缓存，避免重复请求
_user_id_cache: dict[str, str] = {}


@app.route("/user/<screen_name>")
@login_required
def user_timeline(screen_name: str):
    if not _safe_segment(screen_name):
        abort(400)
    if screen_name not in _user_id_cache:
        user_id = get_user_id(screen_name)
        if not user_id:
            abort(404)
        _user_id_cache[screen_name] = user_id
    user_id = _user_id_cache[screen_name]
    tweets, next_cursor = get_user_timeline_with_cursor(user_id, count=20)
    return render_template(
        "user_timeline.html",
        screen_name=screen_name,
        tweets=mark_downloaded(tweets),
        next_cursor=next_cursor or "",
    )


@app.route("/user/<screen_name>/more", methods=["POST"])
@login_required
def user_timeline_more(screen_name: str):
    if not _safe_segment(screen_name):
        abort(400)
    if screen_name not in _user_id_cache:
        user_id = get_user_id(screen_name)
        if not user_id:
            return jsonify({"error": "user not found"}), 404
        _user_id_cache[screen_name] = user_id
    user_id = _user_id_cache[screen_name]
    data = request.get_json(force=True)
    cursor = data.get("cursor", "")
    if not cursor:
        return jsonify({"error": "missing cursor"}), 400
    tweets, next_cursor = get_user_timeline_with_cursor(user_id, count=20, cursor=cursor)
    return jsonify({"tweets": mark_downloaded(tweets), "next_cursor": next_cursor or ""})


@app.route("/timeline/download", methods=["POST"])
@login_required
def timeline_download():
    data = request.get_json(force=True)
    logger.info("收到下载请求: %s", data)
    task_id = str(uuid.uuid4())
    _set_task(task_id, status="pending", progress=0, total=0, done=False)
    t = threading.Thread(
        target=_do_download,
        args=(
            task_id,
            data["user"],
            data["tweet_id"],
            data["video_url"],
            int(data.get("video_index", 0)),
        ),
        daemon=True,
    )
    t.start()
    logger.info("[%s] 任务已创建，线程已启动", task_id[:8])
    return jsonify({"task_id": task_id})


@app.route("/timeline/progress/<task_id>")
@login_required
def timeline_progress(task_id: str):
    def generate():
        while True:
            with _task_lock:
                task = dict(_download_tasks.get(task_id, {"status": "unknown", "done": True}))
            payload = json.dumps({
                "status":   task.get("status", "unknown"),
                "progress": task.get("progress", 0),
                "total":    task.get("total", 0),
                "message":  task.get("message", ""),
            })
            yield f"data: {payload}\n\n"
            if task.get("done"):
                # 延迟清理，确保客户端收到最终事件
                with _task_lock:
                    _download_tasks.pop(task_id, None)
                break
            time.sleep(0.3)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ─── 启动 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
