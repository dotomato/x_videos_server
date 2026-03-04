"""
tests/test_app.py

测试 app.py 中的工具函数和 Flask 路由：
  - _safe_segment()
  - _safe_filename()
  - mark_downloaded()
  - 路由：/play, /delete, /videos, /author, /user, /timeline/more
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# conftest.py 已设置环境变量，可直接导入
import app as flask_app
from app import _safe_segment, _safe_filename, mark_downloaded


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
