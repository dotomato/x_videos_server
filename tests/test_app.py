"""
tests/test_app.py

测试 app.py 中的工具函数和 Flask 路由：
  - _safe_segment()
  - _safe_filename()
  - mark_downloaded()
  - 路由：/play, /delete, /videos, /author, /user, /timeline/more
  - 安全：速率限制、open redirect 修复、HTTP 安全响应头
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# conftest.py 已设置环境变量，可直接导入
import app as flask_app
from app import _safe_segment, _safe_filename, mark_downloaded, _parse_tweet_url


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def app(tmp_path, monkeypatch):
    """返回配置好的 Flask 测试应用，VIDEOS_DIR 指向临时目录。"""
    monkeypatch.setattr(flask_app, "VIDEOS_DIR", tmp_path / "videos")
    flask_app.app.config["TESTING"] = True
    flask_app.app.config["SECRET_KEY"] = "test-secret"
    flask_app.app.secret_key = "test-secret"
    return flask_app.app


@pytest.fixture()
def client(app):
    """已登录状态的 Flask test client。"""
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["logged_in"] = True
            sess["username"] = "testuser"
        yield c


@pytest.fixture()
def videos_dir(app, tmp_path):
    """在临时目录中创建 videos 目录并返回其 Path。"""
    d = tmp_path / "videos"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─────────────────────────────────────────────────────────────────────────────
# _safe_segment()
# ─────────────────────────────────────────────────────────────────────────────

class TestSafeSegment:
    @pytest.mark.parametrize("value", [
        "alice",
        "user123",
        "my_name",
        "my-name",
        "A" * 128,             # 最大长度
        "abc_123-XYZ",
    ])
    def test_valid_values(self, value):
        assert _safe_segment(value) is True

    @pytest.mark.parametrize("value", [
        "",                    # 空字符串
        "..",                  # 点
        "../etc",              # 路径穿越
        "hello world",        # 含空格
        "hello/world",        # 含斜杠
        "hello\\world",       # 含反斜杠
        "用户名",              # 中文
        "A" * 129,             # 超过最大长度
        "name;rm -rf /",       # 注入字符
    ])
    def test_invalid_values(self, value):
        assert _safe_segment(value) is False


# ─────────────────────────────────────────────────────────────────────────────
# _safe_filename()
# ─────────────────────────────────────────────────────────────────────────────

class TestSafeFilename:
    @pytest.mark.parametrize("value", [
        "1234567890_0.mp4",
        "abc-def_0.jpg",
        "valid_name.mp4",
        "tweet_123.jpg",
        "favicon.ico",
    ])
    def test_valid_filenames(self, value):
        assert _safe_filename(value) is True

    @pytest.mark.parametrize("value", [
        "../etc/passwd",        # 路径穿越
        "../../etc.mp4",        # 路径穿越
        "noextension",          # 无扩展名
        "shell.exe",            # 非法扩展名
        "file.php",             # 非法扩展名
        "file.sh",              # 非法扩展名
        "",                     # 空字符串
        ".mp4",                 # stem 为空
        "hello world.mp4",      # stem 含空格
        "name/path.mp4",        # stem 含斜杠
    ])
    def test_invalid_filenames(self, value):
        assert _safe_filename(value) is False


# ─────────────────────────────────────────────────────────────────────────────
# mark_downloaded()
# ─────────────────────────────────────────────────────────────────────────────

class TestMarkDownloaded:
    def test_file_not_exists_marks_false(self, app, tmp_path, monkeypatch):
        videos_dir = tmp_path / "videos"
        monkeypatch.setattr(flask_app, "VIDEOS_DIR", videos_dir)

        tweets = [{"id": "1", "user": "alice", "videos": [{"variants": [{"url": "x"}]}]}]
        mark_downloaded(tweets)
        assert tweets[0]["videos"][0]["downloaded"] is False

    def test_file_exists_marks_true(self, app, tmp_path, monkeypatch):
        videos_dir = tmp_path / "videos"
        user_dir = videos_dir / "alice"
        user_dir.mkdir(parents=True)
        (user_dir / "1_0.mp4").write_bytes(b"fake")
        monkeypatch.setattr(flask_app, "VIDEOS_DIR", videos_dir)

        tweets = [{"id": "1", "user": "alice", "videos": [{"variants": []}]}]
        mark_downloaded(tweets)
        assert tweets[0]["videos"][0]["downloaded"] is True

    def test_no_videos_field_does_not_crash(self, app, tmp_path, monkeypatch):
        monkeypatch.setattr(flask_app, "VIDEOS_DIR", tmp_path / "videos")
        tweets = [{"id": "1", "user": "alice"}]
        result = mark_downloaded(tweets)
        assert result == tweets  # 不崩溃，原样返回

    def test_multiple_videos_per_tweet(self, app, tmp_path, monkeypatch):
        videos_dir = tmp_path / "videos"
        user_dir = videos_dir / "bob"
        user_dir.mkdir(parents=True)
        (user_dir / "99_0.mp4").write_bytes(b"fake")
        # 99_1.mp4 不创建
        monkeypatch.setattr(flask_app, "VIDEOS_DIR", videos_dir)

        tweets = [{
            "id": "99",
            "user": "bob",
            "videos": [{"variants": []}, {"variants": []}],
        }]
        mark_downloaded(tweets)
        assert tweets[0]["videos"][0]["downloaded"] is True
        assert tweets[0]["videos"][1]["downloaded"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 路由：/play/<author>/<filename>
# ─────────────────────────────────────────────────────────────────────────────

class TestPlayRoute:
    def test_invalid_author_returns_400(self, client):
        resp = client.get("/play/../etc/test_0.mp4")
        assert resp.status_code in (400, 404)  # Flask 路由可能先 404

    def test_invalid_filename_returns_400(self, client, videos_dir):
        author_dir = videos_dir / "alice"
        author_dir.mkdir()
        resp = client.get("/play/alice/../../etc.mp4")
        assert resp.status_code in (400, 404)

    def test_file_not_found_returns_404(self, client, videos_dir):
        resp = client.get("/play/alice/nonexistent_0.mp4")
        assert resp.status_code == 404

    def test_valid_file_returns_200(self, client, videos_dir):
        author_dir = videos_dir / "alice"
        author_dir.mkdir()
        (author_dir / "tweet123_0.mp4").write_bytes(b"fake video")
        resp = client.get("/play/alice/tweet123_0.mp4")
        assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# 路由：/delete/<author>/<filename>
# ─────────────────────────────────────────────────────────────────────────────

class TestDeleteRoute:
    def test_invalid_author_returns_400(self, client):
        resp = client.post("/delete/../evil/file_0.mp4")
        assert resp.status_code in (400, 404)

    def test_invalid_filename_returns_400(self, client, videos_dir):
        author_dir = videos_dir / "alice"
        author_dir.mkdir()
        resp = client.post("/delete/alice/../../evil.mp4")
        assert resp.status_code in (400, 404)

    def test_file_not_found_returns_404(self, client, videos_dir):
        resp = client.post("/delete/alice/ghost_0.mp4")
        assert resp.status_code == 404

    def test_deletes_mp4_and_jpg(self, client, videos_dir):
        author_dir = videos_dir / "alice"
        author_dir.mkdir()
        mp4 = author_dir / "tweet_0.mp4"
        jpg = author_dir / "tweet_0.jpg"
        mp4.write_bytes(b"video")
        jpg.write_bytes(b"thumb")

        resp = client.post("/delete/alice/tweet_0.mp4")
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}
        assert not mp4.exists()
        assert not jpg.exists()

    def test_deletes_mp4_when_no_jpg(self, client, videos_dir):
        author_dir = videos_dir / "alice"
        author_dir.mkdir()
        mp4 = author_dir / "tweet_0.mp4"
        mp4.write_bytes(b"video")

        resp = client.post("/delete/alice/tweet_0.mp4")
        assert resp.status_code == 200
        assert not mp4.exists()


# ─────────────────────────────────────────────────────────────────────────────
# 路由：/videos/<author>/<filename>
# ─────────────────────────────────────────────────────────────────────────────

class TestServeVideoRoute:
    def test_invalid_author_returns_400(self, client):
        resp = client.get("/videos/../evil/file_0.mp4")
        assert resp.status_code in (400, 404)

    def test_invalid_filename_returns_400(self, client, videos_dir):
        author_dir = videos_dir / "alice"
        author_dir.mkdir()
        resp = client.get("/videos/alice/../../evil.mp4")
        assert resp.status_code in (400, 404)

    def test_missing_author_dir_returns_404(self, client, videos_dir):
        resp = client.get("/videos/noone/file_0.mp4")
        assert resp.status_code == 404

    def test_valid_video_served(self, client, videos_dir):
        author_dir = videos_dir / "alice"
        author_dir.mkdir()
        (author_dir / "vid_0.mp4").write_bytes(b"fake mp4 content")
        resp = client.get("/videos/alice/vid_0.mp4")
        assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# 路由：/author/<name>
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthorRoute:
    def test_invalid_name_returns_400(self, client):
        resp = client.get("/author/../evil")
        assert resp.status_code in (400, 404)

    def test_valid_name_no_videos_returns_404(self, client, videos_dir):
        resp = client.get("/author/nobody")
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 路由：/user/<screen_name>
# ─────────────────────────────────────────────────────────────────────────────

class TestUserTimelineRoute:
    def test_invalid_screen_name_returns_400(self, client):
        resp = client.get("/user/../evil")
        assert resp.status_code in (400, 404)

    def test_valid_name_with_mock_api(self, client, monkeypatch):
        """mock get_user_id 和 get_user_timeline_with_cursor，验证路由不崩溃。"""
        monkeypatch.setattr(flask_app, "get_user_id", lambda sn: None)
        resp = client.get("/user/validuser")
        # get_user_id 返回 None → 404
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 路由：POST /timeline/more
# ─────────────────────────────────────────────────────────────────────────────

class TestTimelineMoreRoute:
    def test_missing_cursor_returns_400(self, client):
        resp = client.post(
            "/timeline/more",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "missing cursor" in resp.get_json().get("error", "")

    def test_empty_cursor_returns_400(self, client):
        resp = client.post(
            "/timeline/more",
            data=json.dumps({"cursor": ""}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_valid_cursor_calls_api(self, client, monkeypatch):
        monkeypatch.setattr(
            flask_app, "get_home_timeline_with_cursor",
            lambda count, cursor: ([], None)
        )
        resp = client.post(
            "/timeline/more",
            data=json.dumps({"cursor": "some_cursor_value"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "tweets" in data
        assert "next_cursor" in data


# ─────────────────────────────────────────────────────────────────────────────
# 未登录访问受保护路由 → 重定向到 /login
# ─────────────────────────────────────────────────────────────────────────────

class TestLoginRequired:
    def test_unauthenticated_redirects_to_login(self, app):
        with app.test_client() as c:
            resp = c.get("/")
            assert resp.status_code == 302
            assert "/login" in resp.headers["Location"]

    def test_unauthenticated_timeline_redirects(self, app):
        with app.test_client() as c:
            resp = c.get("/timeline")
            assert resp.status_code == 302


# ─────────────────────────────────────────────────────────────────────────────
# 登录速率限制
# ─────────────────────────────────────────────────────────────────────────────

class TestLoginRateLimit:
    def _post_login(self, client, username="nobody", password="wrong"):
        return client.post(
            "/login",
            data={"username": username, "password": password},
            environ_base={"REMOTE_ADDR": "10.0.0.1"},
        )

    def test_too_many_failures_returns_429(self, app, monkeypatch):
        """连续失败超过阈值后应返回 429。"""
        # 清除旧的失败记录，保证测试隔离
        monkeypatch.setattr(flask_app, "_login_attempts", {})
        # check_password 总返回 False
        monkeypatch.setattr(flask_app, "check_password", lambda u, p: False)
        with app.test_client() as c:
            for _ in range(flask_app._MAX_ATTEMPTS):
                c.post("/login", data={"username": "x", "password": "x"},
                       environ_base={"REMOTE_ADDR": "10.0.0.2"})
            resp = c.post("/login", data={"username": "x", "password": "x"},
                          environ_base={"REMOTE_ADDR": "10.0.0.2"})
        assert resp.status_code == 429

    def test_successful_login_clears_failures(self, app, monkeypatch):
        """成功登录后失败计数应清零，再次失败仍可继续计数。"""
        monkeypatch.setattr(flask_app, "_login_attempts", {})
        call_count = {"n": 0}

        def fake_check(u, p):
            call_count["n"] += 1
            return call_count["n"] >= 2  # 第二次才成功

        monkeypatch.setattr(flask_app, "check_password", fake_check)
        with app.test_client() as c:
            # 第一次失败
            c.post("/login", data={"username": "u", "password": "p"},
                   environ_base={"REMOTE_ADDR": "10.0.0.3"})
            # 第二次成功
            resp = c.post("/login", data={"username": "u", "password": "p"},
                          environ_base={"REMOTE_ADDR": "10.0.0.3"})
        assert resp.status_code == 303
        assert flask_app._login_attempts.get("10.0.0.3") is None

    def test_empty_credentials_not_checked(self, app, monkeypatch):
        """空用户名或密码不应调用 check_password，直接报错。"""
        monkeypatch.setattr(flask_app, "_login_attempts", {})
        called = {"v": False}
        def fake_check(u, p):
            called["v"] = True
            return False
        monkeypatch.setattr(flask_app, "check_password", fake_check)
        with app.test_client() as c:
            resp = c.post("/login", data={"username": "", "password": ""},
                          environ_base={"REMOTE_ADDR": "10.0.0.4"})
        assert resp.status_code == 200
        assert called["v"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 开放重定向修复（next 参数）
# ─────────────────────────────────────────────────────────────────────────────

class TestOpenRedirectFix:
    def _login_with_next(self, app, monkeypatch, next_param):
        monkeypatch.setattr(flask_app, "_login_attempts", {})
        monkeypatch.setattr(flask_app, "check_password", lambda u, p: True)
        with app.test_client() as c:
            resp = c.post(
                f"/login?next={next_param}",
                data={"username": "u", "password": "p"},
            )
        return resp

    def test_valid_relative_path_is_redirected(self, app, monkeypatch):
        resp = self._login_with_next(app, monkeypatch, "/timeline")
        assert resp.status_code == 303
        assert resp.headers["Location"].endswith("/timeline")

    def test_external_url_redirects_to_index(self, app, monkeypatch):
        """next=https://evil.com 应重定向到首页，而非外部站点。"""
        resp = self._login_with_next(app, monkeypatch, "https://evil.com")
        assert resp.status_code == 303
        loc = resp.headers["Location"]
        assert "evil.com" not in loc

    def test_protocol_relative_url_redirects_to_index(self, app, monkeypatch):
        """next=//evil.com 应重定向到首页。"""
        resp = self._login_with_next(app, monkeypatch, "//evil.com/path")
        assert resp.status_code == 303
        loc = resp.headers["Location"]
        assert "evil.com" not in loc

    def test_empty_next_redirects_to_index(self, app, monkeypatch):
        resp = self._login_with_next(app, monkeypatch, "")
        assert resp.status_code == 303
        assert resp.headers["Location"].endswith("/")


# ─────────────────────────────────────────────────────────────────────────────
# HTTP 安全响应头
# ─────────────────────────────────────────────────────────────────────────────

class TestSecurityHeaders:
    def test_login_page_has_security_headers(self, app):
        with app.test_client() as c:
            resp = c.get("/login")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    def test_protected_page_has_security_headers(self, client):
        resp = client.get("/")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"

    def test_csp_header_present(self, app):
        with app.test_client() as c:
            resp = c.get("/login")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "default-src" in csp
        assert "frame-ancestors" in csp


# ─────────────────────────────────────────────────────────────────────────────
# load_config() / SECRET_KEY 初始化
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadConfig:
    def test_loads_users_json_successfully(self, tmp_path, monkeypatch):
        cfg = {"secret_key": "s3cr3t", "users": {"alice": "hash"}}
        cfg_file = tmp_path / "users.json"
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
        monkeypatch.setattr(flask_app, "USERS_FILE", cfg_file)
        result = flask_app.load_config()
        assert result["secret_key"] == "s3cr3t"
        assert "alice" in result["users"]

    def test_missing_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(flask_app, "USERS_FILE", tmp_path / "nonexistent.json")
        with pytest.raises(FileNotFoundError):
            flask_app.load_config()


# ─────────────────────────────────────────────────────────────────────────────
# check_password() — 实际 bcrypt 验证
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckPassword:
    def _make_users_file(self, tmp_path, username, raw_password):
        import bcrypt as _bcrypt
        hashed = _bcrypt.hashpw(raw_password.encode(), _bcrypt.gensalt()).decode()
        cfg = {"secret_key": "x", "users": {username: hashed}}
        f = tmp_path / "users.json"
        f.write_text(json.dumps(cfg), encoding="utf-8")
        return f

    def test_correct_password_returns_true(self, tmp_path, monkeypatch):
        f = self._make_users_file(tmp_path, "alice", "s3cr3t")
        monkeypatch.setattr(flask_app, "USERS_FILE", f)
        assert flask_app.check_password("alice", "s3cr3t") is True

    def test_wrong_password_returns_false(self, tmp_path, monkeypatch):
        f = self._make_users_file(tmp_path, "alice", "s3cr3t")
        monkeypatch.setattr(flask_app, "USERS_FILE", f)
        assert flask_app.check_password("alice", "wrong") is False

    def test_unknown_user_returns_false(self, tmp_path, monkeypatch):
        f = self._make_users_file(tmp_path, "alice", "s3cr3t")
        monkeypatch.setattr(flask_app, "USERS_FILE", f)
        assert flask_app.check_password("nobody", "s3cr3t") is False


# ─────────────────────────────────────────────────────────────────────────────
# ensure_thumbnail() — mock OpenCV
# ─────────────────────────────────────────────────────────────────────────────

class TestEnsureThumbnail:
    def test_existing_jpg_returned_immediately(self, tmp_path, monkeypatch):
        mp4 = tmp_path / "vid_0.mp4"
        jpg = tmp_path / "vid_0.jpg"
        mp4.write_bytes(b"fake")
        jpg.write_bytes(b"thumb")
        called = {"v": False}
        class FakeCap:
            def read(self): called["v"] = True; return (True, None)
            def release(self): pass
        monkeypatch.setattr(flask_app.cv2, "VideoCapture", lambda p: FakeCap())
        result = flask_app.ensure_thumbnail(mp4)
        assert result == jpg
        assert called["v"] is False

    def test_generates_jpg_when_missing(self, tmp_path, monkeypatch):
        mp4 = tmp_path / "vid_0.mp4"
        mp4.write_bytes(b"fake")
        class FakeCap:
            def read(self): return (True, "fake_frame")
            def release(self): pass
        written_path = {}
        def fake_imwrite(path, frame):
            written_path["p"] = path
            Path(path).write_bytes(b"jpg")
            return True
        monkeypatch.setattr(flask_app.cv2, "VideoCapture", lambda p: FakeCap())
        monkeypatch.setattr(flask_app.cv2, "imwrite", fake_imwrite)
        result = flask_app.ensure_thumbnail(mp4)
        assert result is not None
        assert result.suffix == ".jpg"

    def test_returns_none_when_frame_read_fails(self, tmp_path, monkeypatch):
        mp4 = tmp_path / "vid_0.mp4"
        mp4.write_bytes(b"fake")
        class FakeCap:
            def read(self): return (False, None)
            def release(self): pass
        monkeypatch.setattr(flask_app.cv2, "VideoCapture", lambda p: FakeCap())
        result = flask_app.ensure_thumbnail(mp4)
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# get_all_videos() / get_latest_videos() / get_latest_by_author() / get_author_videos()
# ─────────────────────────────────────────────────────────────────────────────

class TestVideoScanning:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        self.vdir = tmp_path / "videos"
        self.vdir.mkdir()
        monkeypatch.setattr(flask_app, "VIDEOS_DIR", self.vdir)
        monkeypatch.setattr(flask_app, "ensure_thumbnail",
                            lambda p: p.with_suffix(".jpg") if p.with_suffix(".jpg").exists() else None)

    def _make_video(self, author, stem, with_thumb=True):
        d = self.vdir / author
        d.mkdir(exist_ok=True)
        mp4 = d / f"{stem}.mp4"
        mp4.write_bytes(b"fake")
        if with_thumb:
            (d / f"{stem}.jpg").write_bytes(b"thumb")
        return mp4

    def test_empty_dir_returns_empty(self):
        assert flask_app.get_all_videos() == []

    def test_nonexistent_dir_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(flask_app, "VIDEOS_DIR", tmp_path / "no_such_dir")
        assert flask_app.get_all_videos() == []

    def test_single_video_returned(self):
        self._make_video("alice", "tweet_0")
        videos = flask_app.get_all_videos()
        assert len(videos) == 1
        assert videos[0]["author"] == "alice"
        assert videos[0]["mp4"] == "tweet_0.mp4"
        assert videos[0]["tweet_id"] == "tweet"
        assert videos[0]["index"] == 0

    def test_multiple_authors_all_returned(self):
        self._make_video("alice", "t1_0")
        self._make_video("bob", "t2_1")
        videos = flask_app.get_all_videos()
        authors = {v["author"] for v in videos}
        assert authors == {"alice", "bob"}

    def test_sorted_by_mtime_descending(self):
        import time
        self._make_video("alice", "old_0")
        time.sleep(0.05)
        self._make_video("bob", "new_0")
        videos = flask_app.get_all_videos()
        assert videos[0]["author"] == "bob"
        assert videos[1]["author"] == "alice"

    def test_no_thumb_sets_jpg_none(self):
        self._make_video("alice", "novid_0", with_thumb=False)
        videos = flask_app.get_all_videos()
        assert videos[0]["jpg"] is None

    def test_get_latest_videos_limits_count(self):
        import time
        for i in range(5):
            self._make_video("alice", f"t{i}_0")
            time.sleep(0.01)
        result = flask_app.get_latest_videos(3)
        assert len(result) == 3

    def test_get_latest_by_author_one_per_author(self):
        self._make_video("alice", "t1_0")
        self._make_video("alice", "t2_0")
        self._make_video("bob", "t3_0")
        by_author = flask_app.get_latest_by_author()
        authors = [v["author"] for v in by_author]
        assert authors.count("alice") == 1
        assert authors.count("bob") == 1

    def test_get_latest_by_author_sorted_alphabetically(self):
        self._make_video("zebra", "z_0")
        self._make_video("apple", "a_0")
        by_author = flask_app.get_latest_by_author()
        assert by_author[0]["author"] == "apple"
        assert by_author[1]["author"] == "zebra"

    def test_get_author_videos_filters_correctly(self):
        self._make_video("alice", "ta_0")
        self._make_video("bob", "tb_0")
        result = flask_app.get_author_videos("alice")
        assert all(v["author"] == "alice" for v in result)
        assert len(result) == 1

    def test_get_author_videos_empty_for_unknown(self):
        self._make_video("alice", "ta_0")
        assert flask_app.get_author_videos("nobody") == []


# ─────────────────────────────────────────────────────────────────────────────
# _set_task() — 线程安全任务状态管理
# ─────────────────────────────────────────────────────────────────────────────

class TestSetTask:
    def test_creates_new_task(self, monkeypatch):
        tasks = {}
        monkeypatch.setattr(flask_app, "_download_tasks", tasks)
        flask_app._set_task("t1", status="pending", progress=0)
        assert tasks["t1"] == {"status": "pending", "progress": 0}

    def test_updates_existing_task(self, monkeypatch):
        tasks = {"t1": {"status": "pending", "progress": 0}}
        monkeypatch.setattr(flask_app, "_download_tasks", tasks)
        flask_app._set_task("t1", progress=50)
        assert tasks["t1"]["progress"] == 50
        assert tasks["t1"]["status"] == "pending"

    def test_concurrent_updates_safe(self, monkeypatch):
        import threading
        tasks = {}
        monkeypatch.setattr(flask_app, "_download_tasks", tasks)
        errors = []

        def worker(i):
            try:
                flask_app._set_task(f"t{i}", val=i)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors
        assert len(tasks) == 20


# ─────────────────────────────────────────────────────────────────────────────
# GET /login, POST /login 补充场景
# ─────────────────────────────────────────────────────────────────────────────

class TestLoginRoute:
    def test_get_login_renders_form(self, app):
        with app.test_client() as c:
            resp = c.get("/login")
        assert resp.status_code == 200

    def test_post_login_sets_session(self, app, monkeypatch):
        monkeypatch.setattr(flask_app, "_login_attempts", {})
        monkeypatch.setattr(flask_app, "check_password", lambda u, p: True)
        with app.test_client() as c:
            resp = c.post("/login", data={"username": "alice", "password": "pw"})
            assert resp.status_code == 303
            with c.session_transaction() as sess:
                assert sess.get("logged_in") is True
                assert sess.get("username") == "alice"

    def test_post_login_wrong_credentials_shows_error(self, app, monkeypatch):
        monkeypatch.setattr(flask_app, "_login_attempts", {})
        monkeypatch.setattr(flask_app, "check_password", lambda u, p: False)
        with app.test_client() as c:
            resp = c.post("/login", data={"username": "alice", "password": "bad"})
        assert resp.status_code == 200
        assert "错误" in resp.data.decode("utf-8")

    def test_get_logout_clears_session(self, client):
        resp = client.get("/logout", follow_redirects=False)
        assert resp.status_code == 302
        with client.session_transaction() as sess:
            assert not sess.get("logged_in")


# ─────────────────────────────────────────────────────────────────────────────
# GET / (index)
# ─────────────────────────────────────────────────────────────────────────────

class TestIndexRoute:
    def test_renders_with_empty_videos_dir(self, client, videos_dir):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_renders_with_videos(self, client, videos_dir, monkeypatch):
        author_dir = videos_dir / "alice"
        author_dir.mkdir()
        (author_dir / "tweet_0.mp4").write_bytes(b"fake")
        (author_dir / "tweet_0.jpg").write_bytes(b"thumb")
        monkeypatch.setattr(flask_app, "ensure_thumbnail",
                            lambda p: p.with_suffix(".jpg"))
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"alice" in resp.data


# ─────────────────────────────────────────────────────────────────────────────
# GET /author/<name> — 有视频的成功场景
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthorRouteWithVideos:
    def test_valid_author_with_videos_returns_200(self, client, videos_dir, monkeypatch):
        author_dir = videos_dir / "alice"
        author_dir.mkdir()
        (author_dir / "tweet_0.mp4").write_bytes(b"fake")
        (author_dir / "tweet_0.jpg").write_bytes(b"thumb")
        monkeypatch.setattr(flask_app, "ensure_thumbnail",
                            lambda p: p.with_suffix(".jpg"))
        resp = client.get("/author/alice")
        assert resp.status_code == 200
        assert b"alice" in resp.data


# ─────────────────────────────────────────────────────────────────────────────
# GET /play — referrer 场景
# ─────────────────────────────────────────────────────────────────────────────

class TestPlayRouteReferrer:
    def test_play_uses_referrer_as_back(self, client, videos_dir):
        author_dir = videos_dir / "alice"
        author_dir.mkdir()
        (author_dir / "tweet_0.mp4").write_bytes(b"fake")
        resp = client.get("/play/alice/tweet_0.mp4",
                          headers={"Referer": "http://localhost/author/alice"})
        assert resp.status_code == 200
        assert b"alice" in resp.data

    def test_play_falls_back_to_author_url_without_referrer(self, client, videos_dir):
        author_dir = videos_dir / "alice"
        author_dir.mkdir()
        (author_dir / "tweet_0.mp4").write_bytes(b"fake")
        resp = client.get("/play/alice/tweet_0.mp4")
        assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# GET /timeline (mock API)
# ─────────────────────────────────────────────────────────────────────────────

class TestTimelineRoute:
    def test_timeline_renders(self, client, monkeypatch):
        monkeypatch.setattr(flask_app, "get_home_timeline_with_cursor",
                            lambda count: ([], None))
        resp = client.get("/timeline")
        assert resp.status_code == 200

    def test_timeline_passes_tweets_to_template(self, client, monkeypatch):
        tweet = {
            "id": "1", "user": "alice", "name": "Alice",
            "text": "hello", "created_at": "2024-01-01",
            "likes": 0, "retweets": 0, "replies": 0,
            "url": "https://x.com/alice/status/1",
            "videos": [],
        }
        monkeypatch.setattr(flask_app, "get_home_timeline_with_cursor",
                            lambda count: ([tweet], "next_cur"))
        resp = client.get("/timeline")
        assert resp.status_code == 200
        assert b"alice" in resp.data


# ─────────────────────────────────────────────────────────────────────────────
# POST /user/<screen_name>/more
# ─────────────────────────────────────────────────────────────────────────────

class TestUserTimelineMoreRoute:
    def test_invalid_screen_name_returns_400(self, client):
        resp = client.post("/user/../evil/more",
                           data=json.dumps({"cursor": "abc"}),
                           content_type="application/json")
        assert resp.status_code in (400, 404)

    def test_missing_cursor_returns_400(self, client, monkeypatch):
        monkeypatch.setattr(flask_app, "_user_id_cache", {"validuser": "123"})
        resp = client.post("/user/validuser/more",
                           data=json.dumps({}),
                           content_type="application/json")
        assert resp.status_code == 400

    def test_unknown_user_returns_404(self, client, monkeypatch):
        monkeypatch.setattr(flask_app, "_user_id_cache", {})
        monkeypatch.setattr(flask_app, "get_user_id", lambda sn: None)
        resp = client.post("/user/nobody/more",
                           data=json.dumps({"cursor": "abc"}),
                           content_type="application/json")
        assert resp.status_code == 404

    def test_valid_request_returns_tweets(self, client, monkeypatch):
        monkeypatch.setattr(flask_app, "_user_id_cache", {"alice": "999"})
        monkeypatch.setattr(flask_app, "get_user_timeline_with_cursor",
                            lambda uid, count, cursor: ([], None))
        resp = client.post("/user/alice/more",
                           data=json.dumps({"cursor": "some_cursor"}),
                           content_type="application/json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "tweets" in data
        assert "next_cursor" in data

    def test_user_id_cached_after_first_call(self, client, monkeypatch):
        cache = {}
        monkeypatch.setattr(flask_app, "_user_id_cache", cache)
        monkeypatch.setattr(flask_app, "get_user_id", lambda sn: "uid_42")
        monkeypatch.setattr(flask_app, "get_user_timeline_with_cursor",
                            lambda uid, count, cursor: ([], None))
        client.post("/user/newuser/more",
                    data=json.dumps({"cursor": "c"}),
                    content_type="application/json")
        assert cache.get("newuser") == "uid_42"


# ─────────────────────────────────────────────────────────────────────────────
# POST /timeline/download
# ─────────────────────────────────────────────────────────────────────────────

class TestTimelineDownloadRoute:
    def test_returns_task_id(self, client, monkeypatch):
        started = {"v": False}
        class FakeThread:
            def __init__(self, target, args, daemon): pass
            def start(self): started["v"] = True
        monkeypatch.setattr(flask_app.threading, "Thread", FakeThread)
        resp = client.post(
            "/timeline/download",
            data=json.dumps({
                "user": "alice", "tweet_id": "123",
                "video_url": "http://fake.url/v.mp4", "video_index": 0,
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "task_id" in data
        assert len(data["task_id"]) == 36
        assert started["v"] is True

    def test_task_created_in_pending_state(self, client, monkeypatch):
        tasks = {}
        monkeypatch.setattr(flask_app, "_download_tasks", tasks)
        class FakeThread:
            def __init__(self, target, args, daemon): pass
            def start(self): pass
        monkeypatch.setattr(flask_app.threading, "Thread", FakeThread)
        resp = client.post(
            "/timeline/download",
            data=json.dumps({
                "user": "bob", "tweet_id": "456",
                "video_url": "http://fake.url/v.mp4", "video_index": 0,
            }),
            content_type="application/json",
        )
        task_id = resp.get_json()["task_id"]
        assert task_id in tasks
        assert tasks[task_id]["status"] == "pending"


# ─────────────────────────────────────────────────────────────────────────────
# GET /timeline/progress/<task_id> — SSE
# ─────────────────────────────────────────────────────────────────────────────

class TestTimelineProgressRoute:
    def test_unknown_task_returns_done_event(self, client):
        resp = client.get("/timeline/progress/nonexistent-task-id")
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/event-stream")
        body = resp.data.decode("utf-8")
        assert "data:" in body
        assert '"status"' in body

    def test_completed_task_streams_done(self, client, monkeypatch):
        tasks = {"task99": {"status": "done", "progress": 100, "total": 100, "done": True}}
        monkeypatch.setattr(flask_app, "_download_tasks", tasks)
        resp = client.get("/timeline/progress/task99")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "done" in body

    def test_response_has_no_cache_header(self, client):
        resp = client.get("/timeline/progress/any-id")
        assert resp.headers.get("Cache-Control") == "no-cache"


# ─────────────────────────────────────────────────────────────────────────────
# _parse_tweet_url()
# ─────────────────────────────────────────────────────────────────────────────

class TestParseTweetUrl:
    @pytest.mark.parametrize("url,expected_user,expected_id", [
        ("https://x.com/alice/status/1234567890",   "alice", "1234567890"),
        ("https://www.x.com/bob/status/9999",       "bob",   "9999"),
        # 带查询参数和片段
        ("https://x.com/carol/status/111?s=20",     "carol", "111"),
        ("https://x.com/dave/status/222#anchor",    "dave",  "222"),
        # 路径有更多段（如 /photo/1）
        ("https://x.com/eve/status/333/photo/1",    "eve",   "333"),
    ])
    def test_valid_urls(self, url, expected_user, expected_id):
        screen_name, tweet_id = _parse_tweet_url(url)
        assert screen_name == expected_user
        assert tweet_id == expected_id

    @pytest.mark.parametrize("url", [
        "",                                              # 空字符串
        "not-a-url",                                     # 不是 URL
        "https://twitter.com/alice/status/123",          # 非 x.com 域名
        "https://evil.com/alice/status/123",             # 非 x.com 域名
        "https://x.com/alice",                           # 缺少 /status/
        "https://x.com/alice/status",                    # 缺少 ID
        "https://x.com/alice/status/notanumber",         # ID 不是数字
        "https://x.com/alice/follow",                    # 非 status 路径
    ])
    def test_invalid_urls_return_none_none(self, url):
        screen_name, tweet_id = _parse_tweet_url(url)
        assert screen_name is None
        assert tweet_id is None


# ─────────────────────────────────────────────────────────────────────────────
# GET /downloader
# ─────────────────────────────────────────────────────────────────────────────

class TestDownloaderRoute:
    def test_requires_login(self, app):
        with app.test_client() as c:
            resp = c.get("/downloader")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_renders_page_when_logged_in(self, client):
        resp = client.get("/downloader")
        assert resp.status_code == 200
        # 页面应包含"下载器"字样（UTF-8 编码）
        assert "下载器".encode("utf-8") in resp.data


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/tweet
# ─────────────────────────────────────────────────────────────────────────────

class TestApiTweetRoute:
    def _post(self, client, url=""):
        return client.post(
            "/api/tweet",
            data=__import__("json").dumps({"url": url}),
            content_type="application/json",
        )

    def test_requires_login(self, app):
        with app.test_client() as c:
            resp = c.post("/api/tweet",
                          data=__import__("json").dumps({"url": "https://x.com/u/status/1"}),
                          content_type="application/json")
        assert resp.status_code == 302

    def test_missing_url_returns_400(self, client):
        resp = client.post("/api/tweet",
                           data=__import__("json").dumps({}),
                           content_type="application/json")
        assert resp.status_code == 400
        assert "missing url" in resp.get_json().get("error", "")

    def test_empty_url_returns_400(self, client):
        resp = self._post(client, url="")
        assert resp.status_code == 400

    def test_invalid_url_returns_400(self, client):
        resp = self._post(client, url="https://twitter.com/alice/status/123")
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data

    def test_api_failure_returns_404(self, client, monkeypatch):
        """get_tweet_by_id 返回 None 时应响应 404。"""
        monkeypatch.setattr(flask_app, "get_tweet_by_id", lambda tid: None)
        resp = self._post(client, url="https://x.com/alice/status/9999")
        assert resp.status_code == 404
        data = resp.get_json()
        assert "error" in data

    def test_success_returns_tweet(self, client, monkeypatch):
        """get_tweet_by_id 返回推文时响应 200 并包含 tweet 字段。"""
        fake_tweet = {
            "id": "9999", "user": "alice", "name": "Alice",
            "text": "hello", "created_at": "2024-01-01",
            "likes": 0, "retweets": 0, "replies": 0,
            "url": "https://x.com/alice/status/9999",
            "videos": [],
        }
        monkeypatch.setattr(flask_app, "get_tweet_by_id", lambda tid: fake_tweet)
        resp = self._post(client, url="https://x.com/alice/status/9999")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "tweet" in data
        assert data["tweet"]["id"] == "9999"
        assert data["tweet"]["user"] == "alice"

    def test_success_calls_mark_downloaded(self, client, monkeypatch):
        """成功后应调用 mark_downloaded 标记已下载状态。"""
        fake_tweet = {
            "id": "8888", "user": "bob", "name": "Bob",
            "text": "test", "created_at": "2024-01-01",
            "likes": 0, "retweets": 0, "replies": 0,
            "url": "https://x.com/bob/status/8888",
            "videos": [{"variants": []}],
        }
        monkeypatch.setattr(flask_app, "get_tweet_by_id", lambda tid: fake_tweet)
        called_with = []
        original_mark = flask_app.mark_downloaded
        monkeypatch.setattr(flask_app, "mark_downloaded",
                            lambda tweets: called_with.extend(tweets) or tweets)
        resp = self._post(client, url="https://x.com/bob/status/8888")
        assert resp.status_code == 200
        assert any(t["id"] == "8888" for t in called_with)

    def test_no_json_body_returns_400(self, client):
        """无请求体时应返回 400。"""
        resp = client.post("/api/tweet", data="", content_type="application/json")
        assert resp.status_code == 400
