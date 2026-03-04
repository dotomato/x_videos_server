"""
tests/test_x_timeline.py

测试 x_timeline.py 中的纯逻辑函数（不发起真实网络请求）：
  - extract_videos()
  - _parse_instructions()
  - parse_timeline()
  - parse_user_tweets()
  - get_query_id()
  - get_user_tweets_query_id()
"""
import json
import sys
import os
from pathlib import Path

import pytest

# conftest.py 中已设置环境变量，此处直接导入
sys.path.insert(0, str(Path(__file__).parent.parent))
import x_timeline as xt


# ─────────────────────────────────────────────────────────────────────────────
# 辅助函数：构造最小化的推文 entry
# ─────────────────────────────────────────────────────────────────────────────

def _make_tweet_entry(
    tweet_id="1001",
    screen_name="testuser",
    name="Test User",
    text="Hello world",
    created_at="Mon Jan 01 12:00:00 +0000 2024",
    likes=10,
    retweets=2,
    replies=1,
    videos_legacy=None,
    typename="Tweet",
):
    """构造一个标准的时间线 entry dict。"""
    legacy = {
        "id_str":         tweet_id,
        "full_text":      text,
        "created_at":     created_at,
        "favorite_count": likes,
        "retweet_count":  retweets,
        "reply_count":    replies,
    }
    if videos_legacy:
        legacy.update(videos_legacy)

    tweet_result = {
        "__typename": typename,
        "legacy": legacy,
        "core": {
            "user_results": {
                "result": {
                    "core": {
                        "screen_name": screen_name,
                        "name": name,
                    },
                    "legacy": {},
                }
            }
        },
    }

    return {
        "content": {
            "itemContent": {
                "itemType": "TimelineTweet",
                "tweet_results": {"result": tweet_result},
            }
        }
    }


def _make_cursor_entry(value="next_cursor_value", cursor_type="Bottom"):
    return {
        "content": {
            "entryType": "TimelineTimelineCursor",
            "cursorType": cursor_type,
            "value": value,
        }
    }


def _make_instructions(entries):
    return [{"type": "TimelineAddEntries", "entries": entries}]


# ─────────────────────────────────────────────────────────────────────────────
# extract_videos()
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractVideos:
    def test_empty_legacy_returns_empty(self):
        assert xt.extract_videos({}) == []

    def test_image_media_is_filtered(self):
        legacy = {
            "extended_entities": {
                "media": [{"type": "photo", "video_info": {}}]
            }
        }
        assert xt.extract_videos(legacy) == []

    def test_extracts_video_fields(self):
        legacy = {
            "extended_entities": {
                "media": [{
                    "type": "video",
                    "media_url_https": "https://pbs.twimg.com/thumb.jpg",
                    "video_info": {
                        "duration_millis": 30000,
                        "variants": [
                            {"content_type": "video/mp4", "bitrate": 2176000, "url": "https://video.twimg.com/hi.mp4"},
                            {"content_type": "video/mp4", "bitrate": 832000,  "url": "https://video.twimg.com/lo.mp4"},
                            {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/playlist.m3u8"},
                        ],
                    },
                }]
            }
        }
        videos = xt.extract_videos(legacy)
        assert len(videos) == 1
        v = videos[0]
        assert v["type"] == "video"
        assert v["duration_ms"] == 30000
        assert v["thumb"] == "https://pbs.twimg.com/thumb.jpg"
        # m3u8 应被过滤，只剩 2 个 mp4
        assert len(v["variants"]) == 2
        # 按 bitrate 降序
        assert v["variants"][0]["bitrate"] == 2176000
        assert v["variants"][1]["bitrate"] == 832000

    def test_variants_sorted_by_bitrate_descending(self):
        legacy = {
            "extended_entities": {
                "media": [{
                    "type": "video",
                    "media_url_https": "",
                    "video_info": {
                        "duration_millis": 5000,
                        "variants": [
                            {"content_type": "video/mp4", "bitrate": 100, "url": "lo.mp4"},
                            {"content_type": "video/mp4", "bitrate": 500, "url": "hi.mp4"},
                            {"content_type": "video/mp4", "bitrate": 300, "url": "mid.mp4"},
                        ],
                    },
                }]
            }
        }
        variants = xt.extract_videos(legacy)[0]["variants"]
        bitrates = [v["bitrate"] for v in variants]
        assert bitrates == sorted(bitrates, reverse=True)

    def test_animated_gif_is_extracted(self):
        legacy = {
            "extended_entities": {
                "media": [{
                    "type": "animated_gif",
                    "media_url_https": "https://thumb.jpg",
                    "video_info": {
                        "duration_millis": 0,
                        "variants": [
                            {"content_type": "video/mp4", "bitrate": 0, "url": "https://gif.mp4"},
                        ],
                    },
                }]
            }
        }
        videos = xt.extract_videos(legacy)
        assert len(videos) == 1
        assert videos[0]["type"] == "animated_gif"

    def test_prefers_extended_entities_over_entities(self):
        legacy = {
            "extended_entities": {
                "media": [{"type": "video", "media_url_https": "ext.jpg",
                           "video_info": {"duration_millis": 1000, "variants": []}}]
            },
            "entities": {
                "media": [{"type": "photo", "video_info": {}}]
            }
        }
        videos = xt.extract_videos(legacy)
        # 应该使用 extended_entities 里的 video，不是 entities 里的 photo
        assert len(videos) == 1
        assert videos[0]["type"] == "video"

    def test_no_media_key_returns_empty(self):
        legacy = {"extended_entities": {}}
        assert xt.extract_videos(legacy) == []


# ─────────────────────────────────────────────────────────────────────────────
# _parse_instructions()
# ─────────────────────────────────────────────────────────────────────────────

class TestParseInstructions:
    def test_empty_instructions(self):
        tweets, cursor = xt._parse_instructions([])
        assert tweets == []
        assert cursor is None

    def test_extracts_bottom_cursor(self):
        entries = [_make_cursor_entry("abc123", "Bottom")]
        instructions = _make_instructions(entries)
        tweets, cursor = xt._parse_instructions(instructions)
        assert cursor == "abc123"
        assert tweets == []

    def test_top_cursor_is_ignored(self):
        entries = [_make_cursor_entry("top_val", "Top")]
        instructions = _make_instructions(entries)
        _, cursor = xt._parse_instructions(instructions)
        assert cursor is None

    def test_extracts_tweet_fields(self):
        entries = [_make_tweet_entry(
            tweet_id="9999",
            screen_name="alice",
            name="Alice",
            text="Test tweet",
            likes=42,
            retweets=7,
            replies=3,
        )]
        instructions = _make_instructions(entries)
        tweets, cursor = xt._parse_instructions(instructions)
        assert len(tweets) == 1
        t = tweets[0]
        assert t["id"] == "9999"
        assert t["user"] == "alice"
        assert t["name"] == "Alice"
        assert t["text"] == "Test tweet"
        assert t["likes"] == 42
        assert t["retweets"] == 7
        assert t["replies"] == 3
        assert t["url"] == "https://x.com/alice/status/9999"
        assert t["videos"] == []

    def test_formats_created_at(self):
        entries = [_make_tweet_entry(created_at="Mon Jan 01 12:00:00 +0000 2024")]
        tweets, _ = xt._parse_instructions(_make_instructions(entries))
        assert tweets[0]["created_at"] == "2024-01-01 12:00 UTC"

    def test_invalid_created_at_preserved(self):
        entries = [_make_tweet_entry(created_at="not-a-date")]
        tweets, _ = xt._parse_instructions(_make_instructions(entries))
        assert tweets[0]["created_at"] == "not-a-date"

    def test_skips_non_tweet_typename(self):
        entries = [_make_tweet_entry(typename="SomeOtherType")]
        tweets, _ = xt._parse_instructions(_make_instructions(entries))
        assert tweets == []

    def test_unwraps_tweet_with_visibility_results(self):
        """TweetWithVisibilityResults 包装应被自动解包。"""
        inner_tweet = {
            "__typename": "Tweet",
            "legacy": {
                "id_str": "7777",
                "full_text": "visibility wrapped",
                "created_at": "",
                "favorite_count": 0,
                "retweet_count": 0,
                "reply_count": 0,
            },
            "core": {
                "user_results": {
                    "result": {
                        "core": {"screen_name": "bob", "name": "Bob"},
                        "legacy": {},
                    }
                }
            },
        }
        wrapped = {
            "__typename": "TweetWithVisibilityResults",
            "tweet": inner_tweet,
        }
        entry = {
            "content": {
                "itemContent": {
                    "itemType": "TimelineTweet",
                    "tweet_results": {"result": wrapped},
                }
            }
        }
        tweets, _ = xt._parse_instructions(_make_instructions([entry]))
        assert len(tweets) == 1
        assert tweets[0]["id"] == "7777"
        assert tweets[0]["user"] == "bob"

    def test_skips_non_timeline_tweet_item_type(self):
        entry = {
            "content": {
                "itemContent": {
                    "itemType": "TimelineUser",
                }
            }
        }
        tweets, _ = xt._parse_instructions(_make_instructions([entry]))
        assert tweets == []

    def test_multiple_tweets_and_cursor(self):
        entries = [
            _make_tweet_entry(tweet_id="1"),
            _make_tweet_entry(tweet_id="2"),
            _make_cursor_entry("cursor_xyz"),
        ]
        tweets, cursor = xt._parse_instructions(_make_instructions(entries))
        assert len(tweets) == 2
        assert {t["id"] for t in tweets} == {"1", "2"}
        assert cursor == "cursor_xyz"

    def test_non_add_entries_instruction_is_skipped(self):
        instructions = [
            {"type": "TimelineClearCache"},
            {"type": "TimelineAddEntries", "entries": [_make_tweet_entry(tweet_id="5")]},
        ]
        tweets, _ = xt._parse_instructions(instructions)
        assert len(tweets) == 1
        assert tweets[0]["id"] == "5"


# ─────────────────────────────────────────────────────────────────────────────
# parse_timeline()
# ─────────────────────────────────────────────────────────────────────────────

class TestParseTimeline:
    def _wrap(self, entries):
        return {
            "data": {
                "home": {
                    "home_timeline_urt": {
                        "instructions": _make_instructions(entries)
                    }
                }
            }
        }

    def test_valid_data_returns_tweets(self):
        data = self._wrap([_make_tweet_entry(tweet_id="100")])
        tweets, cursor = xt.parse_timeline(data)
        assert len(tweets) == 1
        assert tweets[0]["id"] == "100"

    def test_missing_instructions_key_returns_empty(self):
        tweets, cursor = xt.parse_timeline({})
        assert tweets == []
        assert cursor is None

    def test_wrong_path_returns_empty(self):
        data = {"data": {"home": {}}}
        tweets, cursor = xt.parse_timeline(data)
        assert tweets == []
        assert cursor is None


# ─────────────────────────────────────────────────────────────────────────────
# parse_user_tweets()
# ─────────────────────────────────────────────────────────────────────────────

class TestParseUserTweets:
    def _wrap(self, entries):
        return {
            "data": {
                "user": {
                    "result": {
                        "timeline_v2": {
                            "timeline": {
                                "instructions": _make_instructions(entries)
                            }
                        }
                    }
                }
            }
        }

    def test_valid_data_returns_tweets(self):
        data = self._wrap([_make_tweet_entry(tweet_id="200")])
        tweets, cursor = xt.parse_user_tweets(data)
        assert len(tweets) == 1
        assert tweets[0]["id"] == "200"

    def test_missing_path_returns_empty(self):
        tweets, cursor = xt.parse_user_tweets({})
        assert tweets == []
        assert cursor is None

    def test_wrong_nested_path_returns_empty(self):
        data = {"data": {"user": {"result": {}}}}
        tweets, cursor = xt.parse_user_tweets(data)
        assert tweets == []
        assert cursor is None


# ─────────────────────────────────────────────────────────────────────────────
# get_query_id() / get_user_tweets_query_id()
# ─────────────────────────────────────────────────────────────────────────────

class TestGetQueryId:
    def test_no_cache_file_returns_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setattr(xt, "CACHE_FILE", tmp_path / "nonexistent.json")
        result = xt.get_query_id()
        assert result == xt.FALLBACK_QUERY_ID

    def test_cache_with_query_id_returns_cached(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps({"query_id": "cached_qid_123"}))
        monkeypatch.setattr(xt, "CACHE_FILE", cache_file)
        assert xt.get_query_id() == "cached_qid_123"

    def test_corrupted_cache_returns_fallback(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text("not valid json {{{{")
        monkeypatch.setattr(xt, "CACHE_FILE", cache_file)
        assert xt.get_query_id() == xt.FALLBACK_QUERY_ID

    def test_cache_missing_query_id_key_returns_fallback(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps({"other_key": "value"}))
        monkeypatch.setattr(xt, "CACHE_FILE", cache_file)
        assert xt.get_query_id() == xt.FALLBACK_QUERY_ID


class TestGetUserTweetsQueryId:
    def test_no_cache_returns_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setattr(xt, "CACHE_FILE", tmp_path / "nonexistent.json")
        assert xt.get_user_tweets_query_id() == xt.FALLBACK_USER_TWEETS_QUERY_ID

    def test_cache_with_user_tweets_query_id(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps({"user_tweets_query_id": "user_qid_abc"}))
        monkeypatch.setattr(xt, "CACHE_FILE", cache_file)
        assert xt.get_user_tweets_query_id() == "user_qid_abc"

    def test_cache_missing_user_tweets_key_returns_fallback(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps({"query_id": "home_qid"}))
        monkeypatch.setattr(xt, "CACHE_FILE", cache_file)
        assert xt.get_user_tweets_query_id() == xt.FALLBACK_USER_TWEETS_QUERY_ID
