#!/usr/bin/env python3.11
"""
X Videos - Flask 视频展示网站
"""

import json
import os
import re
import time
import uuid
import logging
import threading
import bcrypt
import httpx
import tempfile
from pathlib import Path
from functools import wraps
from flask import (
    Flask, render_template, send_from_directory, abort,
    session, redirect, url_for, request, Response, jsonify
)
import cv2
from x_timeline import (
    get_home_timeline, get_home_timeline_with_cursor,
    get_user_id, get_user_timeline_with_cursor,
    get_tweet_by_id, get_bookmarks_with_cursor,
    DOWNLOAD_DIR,
)
from storage import get_storage, StorageBackend, CosStorage

# ─── 日志 ──────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

import datetime

app = Flask(__name__)

@app.template_filter("datetimeformat")
def datetimeformat(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

BASE_DIR = Path(__file__).parent
VIDEOS_DIR = BASE_DIR / "videos"
USERS_FILE = BASE_DIR / "users.json"
RATINGS_FILE = BASE_DIR / "ratings.json"


# ─── 评分数据 ─────────────────────────────────────────────────────────────────

_ratings_lock = threading.Lock()


def load_ratings() -> dict:
    """读取 ratings.json，不存在时返回空字典。"""
    if not RATINGS_FILE.exists():
        return {}
    with open(RATINGS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_ratings(data: dict) -> None:
    """将评分数据写入 ratings.json（需在 _ratings_lock 内调用）。"""
    with open(RATINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ─── 配置加载 ─────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(USERS_FILE, encoding="utf-8") as f:
        return json.load(f)

_config = load_config()
# SECRET_KEY 优先读取环境变量，回退到 users.json（兼容旧部署）
app.secret_key = os.environ.get("SECRET_KEY") or _config.get("secret_key")
if not app.secret_key:
    raise RuntimeError("未设置 SECRET_KEY 环境变量，也未在 users.json 中找到 secret_key")


# ─── 认证 ─────────────────────────────────────────────────────────────────────

# 登录失败计数器：{ ip: (失败次数, 首次失败时间戳) }
_login_attempts: dict[str, tuple[int, float]] = {}
_LOGIN_LOCK = threading.Lock()
_MAX_ATTEMPTS = 5       # 最多连续失败次数
_LOCKOUT_SECS = 300     # 锁定时长（秒）


def _check_rate_limit(ip: str) -> bool:
    """返回 True 表示允许登录，False 表示已被锁定。"""
    with _LOGIN_LOCK:
        if ip not in _login_attempts:
            return True
        count, first_ts = _login_attempts[ip]
        if count < _MAX_ATTEMPTS:
            return True
        # 锁定期过后自动解除
        if time.time() - first_ts > _LOCKOUT_SECS:
            del _login_attempts[ip]
            return True
        return False


def _record_failure(ip: str) -> None:
    with _LOGIN_LOCK:
        if ip in _login_attempts:
            count, first_ts = _login_attempts[ip]
            _login_attempts[ip] = (count + 1, first_ts)
        else:
            _login_attempts[ip] = (1, time.time())


def _clear_failures(ip: str) -> None:
    with _LOGIN_LOCK:
        _login_attempts.pop(ip, None)


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
        ip = request.remote_addr or "unknown"
        if not _check_rate_limit(ip):
            error = f"登录尝试过多，请 {_LOCKOUT_SECS // 60} 分钟后再试"
            logger.warning("登录被限速: ip=%s", ip)
            return render_template("login.html", error=error), 429
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            error = "用户名和密码不能为空"
        elif check_password(username, password):
            _clear_failures(ip)
            session["logged_in"] = True
            session["username"] = username
            raw_next = request.args.get("next", "")
            # 只允许本站相对路径，防止开放重定向（open redirect）
            next_url = raw_next if raw_next.startswith("/") and not raw_next.startswith("//") else url_for("index")
            return redirect(next_url, 303)
        else:
            _record_failure(ip)
            logger.warning("登录失败: ip=%s username=%s", ip, username)
            error = "用户名或密码错误"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─── 安全响应头 ────────────────────────────────────────────────────────────────

@app.after_request
def add_security_headers(response):
    """为所有响应添加安全头，防止常见 Web 攻击。"""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "0"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if not response.direct_passthrough:
        store = get_storage()
        cos_domain = ""
        if isinstance(store, CosStorage):
            # 从 COS bucket 名称提取域名（格式: bucket-appid.cos.region.myqcloud.com）
            import os as _os
            cos_domain = f"{_os.environ.get('COS_BUCKET', '')}.cos.{_os.environ.get('COS_REGION', '')}.myqcloud.com"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            f"img-src 'self' data: pbs.twimg.com video.twimg.com {cos_domain}; "
            f"media-src 'self' blob: {cos_domain}; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        )
    return response


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


def _parse_tweet_url(url: str) -> tuple[str | None, str | None]:
    """从 x.com 推文 URL 中提取 (screen_name, tweet_id)。
    支持 https://x.com/{user}/status/{id} 格式，仅允许 x.com 域名。
    返回 (screen_name, tweet_id) 或 (None, None)。
    """
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        if parsed.netloc not in ("x.com", "www.x.com"):
            return None, None
        parts = [p for p in parsed.path.split("/") if p]
        # 路径格式: /{screen_name}/status/{tweet_id}
        if len(parts) >= 3 and parts[1] == "status" and parts[2].isdigit():
            return parts[0], parts[2]
    except Exception:
        pass
    return None, None


# ─── 缩略图 ───────────────────────────────────────────────────────────────────

def ensure_thumbnail(author: str, filename: str) -> str | None:
    """确保缩略图存在。返回缩略图的 storage key（不含前缀）或 None。
    对于 Local: OpenCV 截帧保存到本地。
    对于 COS: 下载到临时文件 → OpenCV 截帧 → 上传 jpg → 清理。
    """
    store = get_storage()
    mp4_key = f"{author}/{filename}"
    jpg_key = f"{author}/{Path(filename).stem}.jpg"

    # 缩略图已存在
    if store.exists(jpg_key):
        return jpg_key

    if isinstance(store, CosStorage):
        # COS: 下载到临时文件，截帧后上传
        try:
            tmp_dir = Path(tempfile.mkdtemp())
            tmp_mp4 = tmp_dir / filename
            tmp_jpg = tmp_dir / (Path(filename).stem + ".jpg")

            # 下载 mp4
            cos_key = f"videos/{mp4_key}"
            resp = store.client.get_object(
                Bucket=store.bucket,
                Key=cos_key,
            )
            tmp_mp4.write_bytes(resp["Body"].getvalue())

            # 截帧
            cap = cv2.VideoCapture(str(tmp_mp4))
            ret, frame = cap.read()
            cap.release()
            if ret:
                cv2.imwrite(str(tmp_jpg), frame)
                # 上传 jpg
                store.upload_file(jpg_key, tmp_jpg, content_type="image/jpeg")
                # 清理
                tmp_mp4.unlink(missing_ok=True)
                tmp_jpg.unlink(missing_ok=True)
                tmp_dir.rmdir()
                return jpg_key
            else:
                tmp_mp4.unlink(missing_ok=True)
                tmp_dir.rmdir()
                return None
        except Exception as e:
            logger.error("COS 缩略图生成失败: %s", e, exc_info=True)
            return None
    else:
        # Local: 原有逻辑
        mp4_path = VIDEOS_DIR / mp4_key
        jpg_path = VIDEOS_DIR / jpg_key
        if not mp4_path.is_file():
            return None
        cap = cv2.VideoCapture(str(mp4_path))
        ret, frame = cap.read()
        cap.release()
        if ret:
            cv2.imwrite(str(jpg_path), frame)
            return jpg_key
        return None


# ─── 数据扫描 ─────────────────────────────────────────────────────────────────

def get_all_videos() -> list[dict]:
    """扫描所有视频，按 mtime 降序返回"""
    store = get_storage()
    objects = store.list_objects()

    # 筛选 mp4 文件
    mp4_objects = {o["key"]: o for o in objects if o["key"].endswith(".mp4")}
    jpg_objects = {o["key"]: o for o in objects if o["key"].endswith(".jpg")}

    videos = []
    for key, obj in mp4_objects.items():
        # key 格式: author/tweet_id_index.mp4
        parts_path = key.split("/")
        if len(parts_path) != 2:
            continue
        author = parts_path[0]
        filename = parts_path[1]
        stem = Path(filename).stem
        parts = stem.rsplit("_", 1)
        tweet_id = parts[0] if len(parts) == 2 else stem
        index = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0

        jpg_key = f"{author}/{stem}.jpg"
        has_thumb = jpg_key in jpg_objects

        # 解析 mtime
        mtime = obj["last_modified"]
        if isinstance(mtime, str):
            # COS 返回 ISO 格式时间字符串
            try:
                dt = datetime.datetime.fromisoformat(mtime.replace("Z", "+00:00"))
                mtime = dt.timestamp()
            except Exception:
                mtime = 0

        videos.append({
            "author": author,
            "tweet_id": tweet_id,
            "index": index,
            "mp4": filename,
            "jpg": Path(filename).stem + ".jpg" if has_thumb else None,
            "mtime": mtime,
        })

    videos.sort(key=lambda v: v["mtime"], reverse=True)
    return videos


def get_latest_videos(n: int = 10) -> list[dict]:
    return get_all_videos()[:n]


def get_latest_by_author() -> list[dict]:
    """每位作者取最新的一个视频，附带视频总数，按作者名字母排序"""
    seen: dict[str, dict] = {}
    count: dict[str, int] = {}
    for v in get_all_videos():
        author = v["author"]
        count[author] = count.get(author, 0) + 1
        if author not in seen:
            seen[author] = v
    for author, v in seen.items():
        v["video_count"] = count[author]
    return sorted(seen.values(), key=lambda v: v["author"].lower())


def get_author_videos(author: str) -> list[dict]:
    """返回指定作者的所有视频（mtime 降序）"""
    return [v for v in get_all_videos() if v["author"] == author]


# ─── 辅助 ─────────────────────────────────────────────────────────────────────

def _video_url(author: str, filename: str, expires: int = 3600) -> str:
    """获取视频的访问 URL（预签名或本地路径）。"""
    store = get_storage()
    key = f"{author}/{filename}"
    return store.get_url(key, expires=expires)


def _thumb_url(author: str, jpg_filename: str, expires: int = 300) -> str:
    """获取缩略图的访问 URL（预签名或本地路径）。"""
    store = get_storage()
    key = f"{author}/{jpg_filename}"
    return store.get_url(key, expires=expires)


def _annotate_urls(videos: list[dict], thumb_expires: int = 300) -> list[dict]:
    """为视频列表添加 thumb_url 字段。"""
    store = get_storage()
    for v in videos:
        if v.get("jpg"):
            v["thumb_url"] = store.get_url(f"{v['author']}/{v['jpg']}", expires=thumb_expires)
        else:
            v["thumb_url"] = None
    return videos


# ─── 路由 ─────────────────────────────────────────────────────────────────────

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(BASE_DIR / "static", "favicon.ico", mimetype="image/x-icon")


def _fmt_size(size_bytes: int) -> str:
    """将字节数格式化为人类可读的字符串（GiB / MiB / KiB）。"""
    if size_bytes >= 1 << 30:
        return f"{size_bytes / (1 << 30):.2f} GiB"
    if size_bytes >= 1 << 20:
        return f"{size_bytes / (1 << 20):.1f} MiB"
    if size_bytes >= 1 << 10:
        return f"{size_bytes / (1 << 10):.0f} KiB"
    return f"{size_bytes} B"


def get_videos_size() -> int:
    """返回所有 .mp4 文件的字节总和。"""
    store = get_storage()
    return store.total_size()


@app.route("/")
@login_required
def index():
    all_videos = get_all_videos()
    page_size = 30
    first_page = all_videos[:page_size]
    total = len(all_videos)
    has_more = total > page_size
    videos_size = _fmt_size(get_videos_size())
    store = get_storage()
    disk_free = store.get_disk_free()
    _annotate_urls(first_page)
    return render_template("index.html", videos=first_page, total=total,
                           has_more=has_more, page_size=page_size,
                           videos_size=videos_size, disk_free=disk_free,
                           storage_type=os.environ.get("STORAGE_BACKEND", "local"))


@app.route("/videos/more", methods=["POST"])
@login_required
def videos_more():
    """首页无限滚动：按偏移量返回下一批视频。"""
    data = request.get_json(force=True)
    offset = int(data.get("offset", 0))
    page_size = 30
    all_videos = get_all_videos()
    batch = all_videos[offset:offset + page_size]
    has_more = offset + page_size < len(all_videos)
    _annotate_urls(batch)
    return jsonify({
        "videos": batch,
        "has_more": has_more,
    })


@app.route("/authors")
@login_required
def authors():
    """按作者浏览页面。"""
    by_author = get_latest_by_author()
    videos_size = _fmt_size(get_videos_size())
    _annotate_urls(by_author)
    return render_template("authors.html", by_author=by_author,
                           videos_size=videos_size)


@app.route("/author/<name>")
@login_required
def author(name: str):
    if not _safe_segment(name):
        abort(400)
    videos = get_author_videos(name)
    if not videos:
        abort(404)
    _annotate_urls(videos)
    return render_template("author.html", author=name, videos=videos)


@app.route("/play/<author>/<filename>")
@login_required
def play(author: str, filename: str):
    if not _safe_segment(author) or not _safe_filename(filename):
        abort(400)
    store = get_storage()
    mp4_key = f"{author}/{filename}"
    if not store.exists(mp4_key):
        abort(404)
    jpg = filename.rsplit(".", 1)[0] + ".jpg"
    # 确保缩略图存在
    ensure_thumbnail(author, filename)
    # 生成预签名 URL（视频 1 小时，缩略图 5 分钟）
    src_url = store.get_url(mp4_key, expires=3600)
    thumb_url = store.get_url(f"{author}/{jpg}", expires=300) if store.exists(f"{author}/{jpg}") else ""
    back = request.referrer or url_for("author", name=author)
    key = f"{author}/{filename}"
    current_score = load_ratings().get(key, 0)
    return render_template("play.html", author=author, filename=filename,
                           src=src_url,
                           thumb=thumb_url,
                           back=back, current_score=current_score)


@app.route("/delete/<author>/<filename>", methods=["POST"])
@login_required
def delete_video(author: str, filename: str):
    if not _safe_segment(author) or not _safe_filename(filename):
        abort(400)
    store = get_storage()
    mp4_key = f"{author}/{filename}"
    if not store.exists(mp4_key):
        abort(404)
    jpg_key = f"{author}/{Path(filename).stem}.jpg"
    store.delete(mp4_key)
    if store.exists(jpg_key):
        store.delete(jpg_key)
    # 同步删除评分
    key = f"{author}/{filename}"
    with _ratings_lock:
        ratings = load_ratings()
        if key in ratings:
            del ratings[key]
            save_ratings(ratings)
    logger.info("已删除视频: %s/%s", author, filename)
    return jsonify({"ok": True})


@app.route("/rate/<author>/<filename>", methods=["POST"])
@login_required
def rate_video(author: str, filename: str):
    """给视频打分（1-5 星），score=0 表示取消评分。"""
    if not _safe_segment(author) or not _safe_filename(filename):
        abort(400)
    store = get_storage()
    mp4_key = f"{author}/{filename}"
    if not store.exists(mp4_key):
        abort(404)
    data = request.get_json(silent=True) or {}
    try:
        score = int(data.get("score", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "invalid score"}), 400
    if score not in range(0, 6):
        return jsonify({"error": "score must be 0-5"}), 400
    key = f"{author}/{filename}"
    with _ratings_lock:
        ratings = load_ratings()
        if score == 0:
            ratings.pop(key, None)
        else:
            ratings[key] = score
        save_ratings(ratings)
    return jsonify({"ok": True, "score": score})


@app.route("/liked")
@login_required
def liked():
    """喜欢页：按评分降序显示已评分视频（评分相同则按 mtime 降序）。"""
    ratings = load_ratings()
    if not ratings:
        return render_template("liked.html", videos=[])
    store = get_storage()
    # 一次 list_objects 拿到所有对象，避免逐个 API 调用
    all_objects = {o["key"]: o for o in store.list_objects()}

    videos = []
    for key, score in ratings.items():
        # key 格式: author/filename
        parts = key.split("/", 1)
        if len(parts) != 2:
            continue
        author, filename = parts
        if not _safe_segment(author) or not _safe_filename(filename):
            continue
        mp4_key = f"{author}/{filename}"
        if mp4_key not in all_objects:
            continue
        jpg_key = f"{author}/{Path(filename).stem}.jpg"
        has_thumb = jpg_key in all_objects
        stem = Path(filename).stem
        p = stem.rsplit("_", 1)
        tweet_id = p[0] if len(p) == 2 else stem

        # 从缓存的对象信息中获取 mtime
        obj_info = all_objects[mp4_key]
        mtime = obj_info.get("last_modified", 0)
        if isinstance(mtime, str):
            try:
                dt = datetime.datetime.fromisoformat(mtime.replace("Z", "+00:00"))
                mtime = dt.timestamp()
            except Exception:
                mtime = 0

        thumb_url = store.get_url(jpg_key, expires=300) if has_thumb else None

        videos.append({
            "author": author,
            "filename": filename,
            "mp4": filename,
            "jpg": Path(filename).stem + ".jpg" if has_thumb else None,
            "thumb_url": thumb_url,
            "tweet_id": tweet_id,
            "score": score,
            "mtime": mtime,
        })
    videos.sort(key=lambda v: (-v["score"], -v["mtime"]))
    return render_template("liked.html", videos=videos)


@app.route("/videos/<author>/<filename>")
@login_required
def serve_video(author: str, filename: str):
    """本地存储模式下提供视频/缩略图文件服务；COS 模式下重定向到预签名 URL。"""
    if not _safe_segment(author) or not _safe_filename(filename):
        abort(400)
    store = get_storage()
    key = f"{author}/{filename}"
    if isinstance(store, CosStorage):
        # COS 模式：重定向到预签名 URL
        url = store.get_url(key, expires=3600)
        return redirect(url)
    else:
        # Local 模式：原有 send_from_directory
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
    """在后台线程中流式下载单个视频，实时更新进度，上传到存储后端。"""
    store = get_storage()
    filename = f"{tweet_id}_{video_index}.mp4"
    mp4_key = f"{user}/{filename}"
    logger.info("[%s] 开始下载 user=%s tweet_id=%s index=%d", task_id[:8], user, tweet_id, video_index)

    if store.exists(mp4_key):
        logger.info("[%s] 文件已存在，跳过", task_id[:8])
        _set_task(task_id, status="skipped", progress=1, total=1, done=True)
        return

    # 下载到临时文件
    tmp_dir = Path(tempfile.mkdtemp())
    tmp_mp4 = tmp_dir / filename

    try:
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            with client.stream("GET", video_url) as r:
                logger.info("[%s] HTTP %d  Content-Length: %s", task_id[:8], r.status_code, r.headers.get("content-length", "unknown"))
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                _set_task(task_id, status="downloading", progress=0, total=total, done=False)
                downloaded = 0
                with open(tmp_mp4, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=1024 * 64):
                        f.write(chunk)
                        downloaded += len(chunk)
                        _set_task(task_id, progress=downloaded, total=total)

        logger.info("[%s] 下载完成，共 %d 字节，开始上传到存储后端", task_id[:8], downloaded)

        # 上传 mp4 到存储后端
        _set_task(task_id, status="uploading", progress=downloaded, total=total)
        store.upload_file(mp4_key, tmp_mp4, content_type="video/mp4")

        # 生成缩略图并上传
        cap = cv2.VideoCapture(str(tmp_mp4))
        ret, frame = cap.read()
        cap.release()
        if ret:
            tmp_jpg = tmp_dir / f"{Path(filename).stem}.jpg"
            cv2.imwrite(str(tmp_jpg), frame)
            jpg_key = f"{user}/{Path(filename).stem}.jpg"
            store.upload_file(jpg_key, tmp_jpg, content_type="image/jpeg")
            tmp_jpg.unlink(missing_ok=True)

        # 清理临时文件
        tmp_mp4.unlink(missing_ok=True)
        tmp_dir.rmdir()

        logger.info("[%s] 上传完成: %s", task_id[:8], mp4_key)
        _set_task(task_id, status="done", progress=total or 1, total=total or 1, done=True)
    except Exception as e:
        logger.error("[%s] 下载失败: %s", task_id[:8], e, exc_info=True)
        # 清理临时文件
        if tmp_mp4.exists():
            tmp_mp4.unlink(missing_ok=True)
        tmp_dir.rmdir(missing_ok=True)
        _set_task(task_id, status="error", message=str(e), done=True)


def mark_downloaded(tweets: list[dict]) -> list[dict]:
    """为每条推文的每个视频标注是否已下载"""
    store = get_storage()
    for t in tweets:
        for i, v in enumerate(t.get("videos", [])):
            key = f"{t['user']}/{t['id']}_{i}.mp4"
            v["downloaded"] = store.exists(key)
    return tweets


@app.route("/bookmarks")
@login_required
def bookmarks():
    tweets, next_cursor = get_bookmarks_with_cursor(count=20)
    return render_template("bookmarks.html", tweets=mark_downloaded(tweets), next_cursor=next_cursor or "")


@app.route("/bookmarks/more", methods=["POST"])
@login_required
def bookmarks_more():
    data = request.get_json(force=True)
    cursor = data.get("cursor", "")
    if not cursor:
        return jsonify({"error": "missing cursor"}), 400
    tweets, next_cursor = get_bookmarks_with_cursor(count=20, cursor=cursor)
    return jsonify({"tweets": mark_downloaded(tweets), "next_cursor": next_cursor or ""})


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


# ─── 下载器 ───────────────────────────────────────────────────────────────────

@app.route("/downloader")
@login_required
def downloader():
    """单条推文视频下载器页面。"""
    return render_template("downloader.html")


@app.route("/api/tweet", methods=["POST"])
@login_required
def api_get_tweet():
    """接收推文 URL，返回推文数据（含视频列表和已下载标记）。
    请求体：{"url": "https://x.com/user/status/..."}
    响应：{"tweet": {...}} 或 {"error": "..."}
    """
    data = request.get_json(silent=True) or {}
    tweet_url = data.get("url", "").strip()
    if not tweet_url:
        return jsonify({"error": "missing url"}), 400

    _, tweet_id = _parse_tweet_url(tweet_url)
    if not tweet_id:
        return jsonify({"error": "无效的推文 URL，仅支持 https://x.com/user/status/ID 格式"}), 400

    tweet = get_tweet_by_id(tweet_id)
    if not tweet:
        return jsonify({"error": "无法获取推文，请确认 URL 是否正确或推文是否公开可见"}), 404

    mark_downloaded([tweet])
    return jsonify({"tweet": tweet})


# ─── 启动 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=5000, debug=_debug)
