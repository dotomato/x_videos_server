#!/usr/bin/env python3.11
"""
X (Twitter) 首页时间线获取 + 视频下载脚本
使用私有 GraphQL API，通过 Cookie 认证

用法:
  python3.11 x_timeline.py              # 获取 5 条推文
  python3.11 x_timeline.py 20           # 获取 20 条推文
  python3.11 x_timeline.py --download   # 获取推文并下载其中的视频
"""

import os
import re
import sys
import json
import httpx
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone
from wcwidth import wcswidth

# ─── 凭证配置（从环境变量读取，不可硬编码）────────────────────────────────────
def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"环境变量 {name!r} 未设置。\n"
            f"请在项目根目录创建 .env 文件并设置该变量，"
            f"或在 systemd service 中通过 EnvironmentFile= 加载。\n"
            f"参考 .env.example 文件。"
        )
    return value

AUTH_TOKEN = _require_env("X_AUTH_TOKEN")
CT0        = _require_env("X_CT0")

# X 内置的 Bearer Token（固定值，所有客户端通用）
BEARER_TOKEN = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"

# 视频下载目录（相对于脚本所在目录，本地和服务器均适用）
DOWNLOAD_DIR = Path(__file__).parent / "videos"


# ─── 获取 queryId ─────────────────────────────────────────────────────────────
CACHE_FILE = Path(__file__).parent / ".query_id_cache.json"
FALLBACK_QUERY_ID = "5HIFewm4IR4zjZoYSa1vBg"

BROWSER_HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# X GraphQL API 通用 features 参数（所有 timeline 接口共用）
TWEET_FEATURES: dict = {
    "rweb_tipjar_consumption_enabled":                                         True,
    "responsive_web_graphql_exclude_directive_enabled":                        True,
    "verified_phone_label_enabled":                                            False,
    "creator_subscriptions_tweet_preview_enabled":                             True,
    "responsive_web_graphql_timeline_navigation_enabled":                      True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled":       False,
    "communities_web_enable_tweet_community_results_fetch":                    True,
    "c9s_tweet_anatomy_moderator_badge_enabled":                               True,
    "articles_preview_enabled":                                                True,
    "responsive_web_edit_tweet_api_enabled":                                   True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled":              True,
    "view_counts_everywhere_api_enabled":                                      True,
    "longform_notetweets_consumption_enabled":                                 True,
    "responsive_web_twitter_article_tweet_consumption_enabled":                True,
    "tweet_awards_web_tipping_enabled":                                        False,
    "creator_subscriptions_quote_tweet_preview_enabled":                       False,
    "freedom_of_speech_not_reach_fetch_enabled":                               True,
    "standardized_nudges_misinfo":                                             True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled":                                           True,
    "longform_notetweets_rich_text_read_enabled":                              True,
    "longform_notetweets_inline_media_enabled":                                True,
    "responsive_web_enhance_cards_enabled":                                    False,
}


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_query_id() -> str:
    """直接返回缓存的 queryId；若无缓存则返回已知备用值。"""
    cache = _load_cache()
    if cache.get("query_id"):
        return cache["query_id"]
    return FALLBACK_QUERY_ID


def _fetch_query_id_for_operation(
    client: httpx.Client,
    operation_name: str,
    cache_key: str,
    fallback: str,
) -> str:
    """从 X JS bundle 中提取指定 operation 的 queryId 并写入缓存。
    仅在缓存失效（API 请求失败）时调用。
    """
    print(f"{operation_name} queryId 可能已失效，正在重新获取 ...")
    with httpx.Client(headers=BROWSER_HEADERS, cookies=client.cookies, timeout=30) as html_client:
        resp = html_client.get("https://x.com/home", follow_redirects=True)
        main_js_urls = re.findall(r'src="(https://abs\.twimg\.com/responsive-web/client-web/main\.[^"]+\.js)"', resp.text)
        all_js_urls  = re.findall(r'src="(https://abs\.twimg\.com[^"]+\.js)"', resp.text)
        search_urls  = main_js_urls + [u for u in all_js_urls if u not in main_js_urls]

        for url in search_urls[:6]:
            js_resp = html_client.get(url)
            match = re.search(rf'queryId:"([^"]+)",operationName:"{operation_name}"', js_resp.text)
            if match:
                qid = match.group(1)
                cache = _load_cache()
                cache[cache_key] = qid
                CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
                print(f"找到新 {operation_name} queryId: {qid}（已缓存）")
                return qid

    print(f"未找到 {operation_name} queryId，使用已知备用值")
    return fallback


def fetch_query_id(client: httpx.Client) -> str:
    """刷新 HomeTimeline queryId。"""
    return _fetch_query_id_for_operation(client, "HomeTimeline", "query_id", FALLBACK_QUERY_ID)


# ─── 构建请求头 ───────────────────────────────────────────────────────────────
def make_headers() -> dict:
    return {
        "authority":              "x.com",
        "origin":                 "https://x.com",
        "referer":                "https://x.com/home",
        "content-type":           "application/json",
        "authorization":          f"Bearer {BEARER_TOKEN}",
        "x-csrf-token":           CT0,
        "x-twitter-active-user":  "yes",
        "x-twitter-auth-type":    "OAuth2Session",
        "x-client-uuid":          str(uuid4()),
        "user-agent":             "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    }


# ─── 提取视频信息 ─────────────────────────────────────────────────────────────
def extract_videos(legacy: dict) -> list[dict]:
    """
    从推文 legacy 数据中提取视频信息。
    返回列表，每项包含 type、duration_ms、variants（按码率降序）。
    """
    entities = legacy.get("extended_entities") or legacy.get("entities") or {}
    media_list = entities.get("media", [])
    videos = []
    for m in media_list:
        media_type = m.get("type", "")
        if media_type not in ("video", "animated_gif"):
            continue
        video_info = m.get("video_info", {})
        variants = [
            v for v in video_info.get("variants", [])
            if v.get("content_type") == "video/mp4"
        ]
        # 按码率降序排列，取最高画质
        variants.sort(key=lambda v: v.get("bitrate", 0), reverse=True)
        videos.append({
            "type":        media_type,
            "duration_ms": video_info.get("duration_millis", 0),
            "thumb":       m.get("media_url_https", ""),
            "variants":    variants,
        })
    return videos


# ─── 下载视频 ─────────────────────────────────────────────────────────────────
def download_video(tweet: dict, video: dict, index: int = 0) -> Path | None:
    """下载单个视频，保存到 DOWNLOAD_DIR，返回文件路径"""
    if not video["variants"]:
        print(f"  [!] 推文 {tweet['id']} 没有可用的 mp4 链接")
        return None

    best = video["variants"][0]
    video_url = best["url"]
    bitrate   = best.get("bitrate", 0)

    user_dir = DOWNLOAD_DIR / tweet['user']
    user_dir.mkdir(parents=True, exist_ok=True)
    filename = user_dir / f"{tweet['id']}_{index}.mp4"

    if filename.exists():
        print(f"  已存在: {filename.name}")
        return filename

    print(f"  下载中 ({bitrate//1000} kbps): {filename.name}")
    with httpx.Client(timeout=120, follow_redirects=True) as dl_client:
        with dl_client.stream("GET", video_url) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            downloaded = 0
            with open(filename, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=1024 * 64):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded * 100 // total
                        print(f"\r  进度: {pct:3d}% ({downloaded//1024}KB/{total//1024}KB)", end="", flush=True)
            print()

    print(f"  保存至: {filename}")
    return filename


# ─── 请求首页时间线 ───────────────────────────────────────────────────────────
def get_home_timeline(count: int = 5, cursor: str = None) -> list[dict]:
    cookies = {"auth_token": AUTH_TOKEN, "ct0": CT0}

    with httpx.Client(cookies=cookies, headers=make_headers(), timeout=30) as client:
        query_id = get_query_id()

        variables = {
            "count":                  count,
            "includePromotedContent": True,
            "latestControlAvailable": True,
            "requestContext":         "launch",
            "withCommunity":          True,
            "seenTweetIds":           [],
        }
        if cursor:
            variables["cursor"] = cursor

        features = TWEET_FEATURES

        url = f"https://x.com/i/api/graphql/{query_id}/HomeTimeline"
        payload = {
            "queryId":   query_id,
            "variables": variables,
            "features":  features,
        }

        print(f"正在请求首页时间线 (count={count}) ...")
        resp = client.post(url, json=payload)

        # queryId 失效（400/403）时自动刷新并重试一次
        if resp.status_code in (400, 403):
            print(f"请求失败: HTTP {resp.status_code}，尝试刷新 queryId ...")
            query_id = fetch_query_id(client)
            url = f"https://x.com/i/api/graphql/{query_id}/HomeTimeline"
            payload["queryId"] = query_id
            resp = client.post(url, json=payload)

        if resp.status_code != 200:
            print(f"请求失败: HTTP {resp.status_code}")
            print(resp.text[:500])
            return []

        tweets, _ = parse_timeline(resp.json())
        return tweets


def get_home_timeline_with_cursor(count: int = 20, cursor: str = None) -> tuple[list[dict], str | None]:
    """返回 (tweets, next_cursor)，供分页加载使用"""
    cookies = {"auth_token": AUTH_TOKEN, "ct0": CT0}

    with httpx.Client(cookies=cookies, headers=make_headers(), timeout=30) as client:
        query_id = get_query_id()

        variables = {
            "count":                  count,
            "includePromotedContent": True,
            "latestControlAvailable": True,
            "requestContext":         "launch",
            "withCommunity":          True,
            "seenTweetIds":           [],
        }
        if cursor:
            variables["cursor"] = cursor

        features = TWEET_FEATURES

        url = f"https://x.com/i/api/graphql/{query_id}/HomeTimeline"
        payload = {
            "queryId":   query_id,
            "variables": variables,
            "features":  features,
        }

        resp = client.post(url, json=payload)

        if resp.status_code in (400, 403):
            query_id = fetch_query_id(client)
            url = f"https://x.com/i/api/graphql/{query_id}/HomeTimeline"
            payload["queryId"] = query_id
            resp = client.post(url, json=payload)

        if resp.status_code != 200:
            return [], None

        return parse_timeline(resp.json())


# ─── 解析返回数据 ─────────────────────────────────────────────────────────────
def _parse_instructions(instructions: list) -> tuple[list[dict], str | None]:
    """从 GraphQL instructions 列表中提取推文列表和底部 cursor。
    供 parse_timeline / parse_user_tweets 复用。
    """
    tweets = []
    bottom_cursor = None

    for instruction in instructions:
        if instruction.get("type") != "TimelineAddEntries":
            continue
        for entry in instruction.get("entries", []):
            content = entry.get("content", {})
            # 提取底部 cursor
            if content.get("entryType") == "TimelineTimelineCursor" and content.get("cursorType") == "Bottom":
                bottom_cursor = content.get("value")
                continue
            item_content = content.get("itemContent", {})
            if item_content.get("itemType") != "TimelineTweet":
                continue
            tweet_result = item_content.get("tweet_results", {}).get("result", {})
            if tweet_result.get("__typename") == "TweetWithVisibilityResults":
                tweet_result = tweet_result.get("tweet", {})
            if tweet_result.get("__typename") != "Tweet":
                continue

            legacy      = tweet_result.get("legacy", {})
            user_result = tweet_result.get("core", {}).get("user_results", {}).get("result", {})
            user_core   = user_result.get("core", {})
            user_legacy = user_result.get("legacy", {})
            user = {
                "screen_name": user_core.get("screen_name") or user_legacy.get("screen_name", ""),
                "name":        user_core.get("name")        or user_legacy.get("name", ""),
            }

            # 格式化时间
            created_at = legacy.get("created_at", "")
            try:
                dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S +0000 %Y")
                dt = dt.replace(tzinfo=timezone.utc)
                created_at = dt.strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                pass

            tweets.append({
                "id":         legacy.get("id_str", ""),
                "user":       user["screen_name"],
                "name":       user["name"],
                "text":       legacy.get("full_text", legacy.get("text", "")),
                "created_at": created_at,
                "likes":      legacy.get("favorite_count", 0),
                "retweets":   legacy.get("retweet_count", 0),
                "replies":    legacy.get("reply_count", 0),
                "url":        f"https://x.com/{user['screen_name']}/status/{legacy.get('id_str','')}",
                "videos":     extract_videos(legacy),
            })

    return tweets, bottom_cursor


def parse_timeline(data: dict) -> tuple[list[dict], str | None]:
    """解析 HomeTimeline GraphQL 响应，返回 (tweets, bottom_cursor)。"""
    try:
        instructions = data["data"]["home"]["home_timeline_urt"]["instructions"]
    except KeyError:
        print("解析失败，原始响应:")
        print(json.dumps(data, indent=2, ensure_ascii=False)[:1000])
        return [], None
    return _parse_instructions(instructions)


def parse_user_tweets(data: dict) -> tuple[list[dict], str | None]:
    """解析 UserTweets GraphQL 响应，返回 (tweets, bottom_cursor)。"""
    try:
        instructions = data["data"]["user"]["result"]["timeline_v2"]["timeline"]["instructions"]
    except KeyError:
        return [], None
    return _parse_instructions(instructions)


# ─── 展示推文 ─────────────────────────────────────────────────────────────────
def print_tweets(tweets: list[dict], download: bool = False):
    if not tweets:
        print("没有获取到推文")
        return

    video_count = sum(1 for t in tweets if t["videos"])

    # 记录含视频推文的序号（1-based），供后续交互使用
    video_tweet_nums: list[int] = []
    for i, t in enumerate(tweets, 1):
        if t["videos"]:
            video_tweet_nums.append(i)

    def pad(s: str, width: int) -> str:
        """按显示宽度补齐空格（正确处理中文等宽字符）"""
        w = wcswidth(s)
        if w < 0:
            w = len(s)
        return s + " " * max(0, width - w)

    def truncate(s: str, max_width: int) -> str:
        """按显示宽度截断字符串，超出部分用…替代"""
        s = s.replace("\n", " ")
        cur = 0
        for idx, ch in enumerate(s):
            from wcwidth import wcwidth as _wcw
            cw = _wcw(ch)
            if cw < 0:
                cw = 1
            if cur + cw > max_width - 1:
                return s[:idx] + "…"
            cur += cw
        return s

    # 列宽定义（显示宽度）
    W_NUM     = 4
    W_AUTHOR  = 22
    W_SNIPPET = 54
    W_VIDEO   = 6
    total_w   = W_NUM + W_AUTHOR + W_SNIPPET + W_VIDEO + 10

    # 汇总表格
    print(f"\n共获取到 {len(tweets)} 条推文（其中 {video_count} 条含视频）:\n")
    print("─" * total_w)
    print(pad("#", W_NUM) + pad("作者", W_AUTHOR) + pad("正文摘要", W_SNIPPET) + pad("视频", W_VIDEO) + "时长")
    print("─" * total_w)
    for i, t in enumerate(tweets, 1):
        has_video = bool(t["videos"])
        snippet   = truncate(t["text"], W_SNIPPET)
        if has_video:
            dur_str    = "  ".join(f"{v['duration_ms']//1000}s" for v in t["videos"])
            video_mark = "✅"
        else:
            dur_str    = "—"
            video_mark = "❌"
        print(pad(str(i), W_NUM) + pad("@" + t['user'], W_AUTHOR) + pad(snippet, W_SNIPPET) + pad(video_mark, W_VIDEO) + dur_str)
    print("─" * total_w)

    # 交互式下载
    if download and video_tweet_nums:
        nums_str = " ".join(str(n) for n in video_tweet_nums)
        print(f"\n含视频推文编号: {nums_str}")
        raw = input("请输入要下载的推文编号（逗号分隔，如 1,3 或 all，直接回车跳过）: ").strip()

        if not raw:
            return

        if raw.lower() == "all":
            selected = set(video_tweet_nums)
        else:
            selected = set()
            for part in raw.split(","):
                part = part.strip()
                if part.isdigit():
                    n = int(part)
                    if n in video_tweet_nums:
                        selected.add(n)
                    else:
                        print(f"  [!] 编号 {n} 不含视频，跳过")

        for i, t in enumerate(tweets, 1):
            if i in selected:
                for vi, v in enumerate(t["videos"]):
                    download_video(t, v, vi)


# ─── 用户时间线 ───────────────────────────────────────────────────────────────

# 备用 queryId（从 X JS bundle 中提取，会过期，届时自动刷新）
FALLBACK_USER_TWEETS_QUERY_ID = "V1ze5q3ijDS1VeLwLY0m7g"
_USER_QUERY_ID_CACHE_KEY = "user_tweets_query_id"


def get_user_tweets_query_id() -> str:
    cache = _load_cache()
    return cache.get(_USER_QUERY_ID_CACHE_KEY) or FALLBACK_USER_TWEETS_QUERY_ID


def _fetch_user_tweets_query_id(client: httpx.Client) -> str:
    """刷新 UserTweets queryId。"""
    return _fetch_query_id_for_operation(
        client, "UserTweets", _USER_QUERY_ID_CACHE_KEY, FALLBACK_USER_TWEETS_QUERY_ID
    )


def get_user_id(screen_name: str) -> str | None:
    """通过 X GraphQL UserByScreenName 接口获取用户数字 ID。"""
    cookies = {"auth_token": AUTH_TOKEN, "ct0": CT0}
    # UserByScreenName queryId 较稳定，直接用已知值
    query_id = "xmU6X_CKVnQ5lSrCbAmJsg"
    url = f"https://x.com/i/api/graphql/{query_id}/UserByScreenName"
    params = {
        "variables": json.dumps({
            "screen_name": screen_name,
            "withSafetyModeUserFields": True,
        }),
        "features": json.dumps({
            "hidden_profile_subscriptions_enabled": True,
            "rweb_tipjar_consumption_enabled": True,
            "responsive_web_graphql_exclude_directive_enabled": True,
            "verified_phone_label_enabled": False,
            "subscriptions_verification_info_is_identity_verified_enabled": True,
            "subscriptions_verification_info_verified_since_enabled": True,
            "highlights_tweets_tab_ui_enabled": True,
            "responsive_web_twitter_article_notes_tab_enabled": False,
            "creator_subscriptions_tweet_preview_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "responsive_web_graphql_timeline_navigation_enabled": True,
        }),
    }
    with httpx.Client(cookies=cookies, headers=make_headers(), timeout=15) as client:
        try:
            resp = client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                return data["data"]["user"]["result"]["rest_id"]
        except Exception as e:
            print(f"获取用户 ID 失败: {e}")
    return None


def get_user_timeline_with_cursor(user_id: str, count: int = 20, cursor: str = None) -> tuple[list[dict], str | None]:
    """获取指定用户的推文时间线，返回 (tweets, next_cursor)。"""
    cookies = {"auth_token": AUTH_TOKEN, "ct0": CT0}

    with httpx.Client(cookies=cookies, headers=make_headers(), timeout=30) as client:
        query_id = get_user_tweets_query_id()

        variables = {
            "userId":                 user_id,
            "count":                  count,
            "includePromotedContent": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice":              True,
            "withV2Timeline":         True,
        }
        if cursor:
            variables["cursor"] = cursor

        features = TWEET_FEATURES

        url = f"https://x.com/i/api/graphql/{query_id}/UserTweets"
        params = {
            "variables": json.dumps(variables),
            "features":  json.dumps(features),
        }

        resp = client.get(url, params=params)

        if resp.status_code in (400, 403):
            query_id = _fetch_user_tweets_query_id(client)
            params["variables"] = json.dumps({**variables})
            url = f"https://x.com/i/api/graphql/{query_id}/UserTweets"
            resp = client.get(url, params=params)

        if resp.status_code != 200:
            print(f"UserTweets 请求失败: HTTP {resp.status_code}")
            return [], None

        return parse_user_tweets(resp.json())


# ─── 单条推文详情 ──────────────────────────────────────────────────────────────

# 备用 queryId（可能随 X 更新失效，届时需从 JS bundle 重新提取）
FALLBACK_TWEET_DETAIL_QUERY_ID = "BbCrSoXIR7z93lLCVFlQ2Q"
_TWEET_DETAIL_QUERY_ID_CACHE_KEY = "tweet_detail_query_id"


def get_tweet_detail_query_id() -> str:
    cache = _load_cache()
    return cache.get(_TWEET_DETAIL_QUERY_ID_CACHE_KEY) or FALLBACK_TWEET_DETAIL_QUERY_ID


def get_tweet_by_id(tweet_id: str) -> dict | None:
    """通过推文 ID 获取单条推文详情（含视频信息）。
    使用 TweetDetail GraphQL 接口，返回与 parse_timeline 格式一致的 tweet dict。
    失败时返回 None。
    """
    cookies = {"auth_token": AUTH_TOKEN, "ct0": CT0}
    query_id = get_tweet_detail_query_id()
    url = f"https://x.com/i/api/graphql/{query_id}/TweetDetail"
    variables = {
        "focalTweetId":                          tweet_id,
        "with_rux_injections":                   False,
        "includePromotedContent":                True,
        "withCommunity":                         True,
        "withQuickPromoteEligibilityTweetFields": True,
        "withBirdwatchNotes":                    True,
        "withVoice":                             True,
        "withV2Timeline":                        True,
    }
    params = {
        "variables": json.dumps(variables),
        "features":  json.dumps(TWEET_FEATURES),
    }
    with httpx.Client(cookies=cookies, headers=make_headers(), timeout=15) as client:
        try:
            resp = client.get(url, params=params)
            if resp.status_code != 200:
                print(f"TweetDetail 请求失败: HTTP {resp.status_code}")
                return None
            data = resp.json()
            instructions = (
                data.get("data", {})
                    .get("threaded_conversation_with_injections_v2", {})
                    .get("instructions", [])
            )
            tweets, _ = _parse_instructions(instructions)
            # 优先返回 focalTweetId 对应的推文
            for t in tweets:
                if t["id"] == tweet_id:
                    return t
            # 若结构不同则返回解析到的第一条
            if tweets:
                return tweets[0]
        except Exception as e:
            print(f"获取推文详情失败: {e}")
    return None


# ─── 入口 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args     = sys.argv[1:]
    download = "--download" in args
    nums     = [a for a in args if a.isdigit()]
    count    = int(nums[0]) if nums else 5

    tweets = get_home_timeline(count=count)[:count]
    print_tweets(tweets, download=download)
