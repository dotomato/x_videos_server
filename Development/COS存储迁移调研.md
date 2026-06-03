# 视频存储迁移调研：本地 → 腾讯云 COS

## 一、现状分析

### 当前本地存储涉及的文件和操作

| 位置 | 操作 | 说明 |
|------|------|------|
| `x_timeline.py` `download_video()` | 写入 | 流式下载视频到 `videos/{user}/{tweet_id}_{index}.mp4` |
| `app.py` `_do_download()` | 写入 | 后台线程流式下载视频到 `videos/{user}/{tweet_id}_{index}.mp4` |
| `app.py` `get_all_videos()` | 扫描 | `VIDEOS_DIR.glob("*/*.mp4")` 遍历所有视频文件 |
| `app.py` `ensure_thumbnail()` | 读写 | OpenCV 读取 mp4 第一帧，写入 jpg 缩略图 |
| `app.py` `serve_video()` | 读取 | `send_from_directory()` 提供视频/缩略图文件 |
| `app.py` `play()` | 读取 | 检查 mp4 是否存在、获取缩略图路径 |
| `app.py` `delete_video()` | 删除 | `unlink()` 删除 mp4 + jpg |
| `app.py` `mark_downloaded()` | 检查 | `path.is_file()` 检查视频是否已下载 |
| `app.py` `rate_video()` | 检查 | `mp4_path.is_file()` 确认视频存在 |
| `app.py` `liked()` | 读取+检查 | `ensure_thumbnail()` + `is_file()` |
| `app.py` `get_videos_size()` | 扫描 | `VIDEOS_DIR.glob("*/*.mp4")` 统计总大小 |
| `app.py` `index()` | 读取 | `os.statvfs()` 获取磁盘剩余空间 |

### 需要改动的核心函数（6个）

1. **上传（写入）**：`download_video()`、`_do_download()`
2. **扫描（列表）**：`get_all_videos()`、`get_videos_size()`
3. **读取（服务）**：`serve_video()`
4. **删除**：`delete_video()`

---

## 二、COS SDK 核心操作映射

SDK 包：`cos-python-sdk-v5`（`pip install cos-python-sdk-v5`）

### 2.1 初始化

```python
from qcloud_cos import CosConfig, CosS3Client

config = CosConfig(
    Region=os.environ["COS_REGION"],       # 如 "ap-beijing"
    SecretId=os.environ["COS_SECRET_ID"],
    SecretKey=os.environ["COS_SECRET_KEY"],
    Scheme="https",
)
cos_client = CosS3Client(config)
COS_BUCKET = os.environ["COS_BUCKET"]      # 如 "my-videos-1250000000"
COS_PREFIX = "videos/"                     # COS 对象键前缀
```

### 2.2 操作映射表

| 本地操作 | COS SDK 方法 | 说明 |
|----------|-------------|------|
| `open(path, "wb") + write` | `cos_client.put_object(Body=stream, Key=...)` | 小文件简单上传（<5GB） |
| `open(path, "wb") + stream write` | `cos_client.upload_file(LocalFilePath=..., Key=...)` | 大文件分块+多线程上传，支持断点续传 |
| `Path.glob("*/*.mp4")` | `cos_client.list_objects(Bucket=..., Prefix=...)` | 分页列举，需循环处理 `IsTruncated` |
| `send_from_directory()` | **预签名 URL** 或 `cos_client.get_object()` | 见下方方案选择 |
| `path.is_file()` | `cos_client.object_exists(Bucket=..., Key=...)` | 或 `head_object()` |
| `path.unlink()` | `cos_client.delete_object(Bucket=..., Key=...)` | |
| `path.stat().st_size` | `cos_client.head_object()` → `Content-Length` | |
| `os.statvfs()` | 无直接对应 | COS 无"磁盘空间"概念 |

### 2.3 预签名 URL（视频访问的关键）

```python
# 生成下载预签名 URL（默认有效1小时，可自定义）
url = cos_client.get_presigned_download_url(
    Bucket=COS_BUCKET,
    Key="videos/alice/123456_0.mp4",
    Expires=3600,  # 秒
)
# 返回: https://bucket.cos.region.myqcloud.com/videos/alice/123456_0.mp4?sign=...
```

**关键特性**：
- 私有读桶 + 预签名 URL = 临时授权访问，安全性好
- 永久密钥可生成任意时长的预签名 URL
- 临时密钥最长 36 小时
- 前端 `<video>` 和 `<img>` 可直接使用预签名 URL

---

## 三、方案设计

### 3.1 存储层抽象

推荐引入一个 `storage.py` 模块，封装所有存储操作，提供统一接口。这样 `app.py` 和 `x_timeline.py` 无需关心底层是本地还是 COS。

```python
# storage.py — 统一存储接口
class StorageBackend:
    """存储后端抽象基类"""
    def upload(self, key: str, data_or_path, content_type: str = "") -> str: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def list_objects(self, prefix: str) -> list[dict]: ...
    def get_url(self, key: str, expires: int = 3600) -> str: ...
    def get_size(self, key: str) -> int: ...
    def total_size(self, prefix: str) -> int: ...

class LocalStorage(StorageBackend): ...   # 现有本地文件系统逻辑
class CosStorage(StorageBackend): ...     # 腾讯云 COS 实现
```

### 3.2 对象键（Key）设计

当前本地路径：`videos/alice/1234567890_0.mp4`

COS 对象键设计（保持一致）：
```
videos/alice/1234567890_0.mp4    # 视频
videos/alice/1234567890_0.jpg    # 缩略图
```

### 3.3 视频访问方案选择

| 方案 | 优点 | 缺点 |
|------|------|------|
| **A. 预签名 URL（推荐）** | 卸载带宽到 COS CDN；无需 Flask 代理流；播放体验好 | URL 有过期时间，需前端处理刷新 |
| B. Flask 代理转发 | 改动最小；URL 不变 | 所有流量经过服务器，浪费带宽；延迟高 |
| C. 公有读桶 | 最简单 | 安全风险：任何人都可访问视频 |

**推荐方案 A**：私有桶 + 预签名 URL。

视频播放页面 `/play/<author>/<filename>` 改为返回预签名 URL 作为 `<video src>`。
缩略图同理，使用短时效（5分钟）预签名 URL。
首页/列表页只显示缩略图，按需生成预签名 URL，开销可接受。

### 3.4 缩略图方案

| 方案 | 说明 |
|------|------|
| **A. 上传时生成（推荐）** | 下载视频后，本地用 OpenCV 截帧 → 上传 jpg 到 COS → 删除本地临时文件 |
| B. COS 数据万象 | 使用 COS 图片处理能力实时截帧，但只支持部分格式，且需额外费用 |
| C. 按需生成 + 缓存 | 首次访问时从 COS 下载到本地 → 截帧 → 上传 jpg → 返回 URL；复杂且慢 |

**推荐方案 A**：保持现有的 OpenCV 截帧逻辑，只是截帧后将 jpg 上传到 COS，本地不保留。

### 3.5 下载流程改造

```
当前流程：
  httpx.stream(video_url) → write local file → ensure_thumbnail()

COS 流程：
  httpx.stream(video_url) → write temp file → upload_file() to COS
                          → ensure_thumbnail() on temp → upload jpg to COS
                          → delete temp files
```

`_do_download()` 改造步骤：
1. 下载到临时文件（`tempfile.NamedTemporaryFile`）
2. 上传 mp4 到 COS：`cos_client.upload_file(LocalFilePath=temp_mp4, Key=...)`
3. 用 OpenCV 截帧生成 jpg 临时文件
4. 上传 jpg 到 COS：`cos_client.put_object_from_local_file(LocalFilePath=temp_jpg, Key=...)`
5. 清理临时文件
6. 更新进度状态

`x_timeline.py` 的 `download_video()` 同理。

### 3.6 视频列表获取

当前 `get_all_videos()` 通过 `glob` 遍历本地目录。改为 COS 后需要：

```python
def list_all_cos_objects(prefix="videos/") -> list[dict]:
    """列举 COS 中所有视频对象"""
    marker = ""
    all_objects = []
    while True:
        resp = cos_client.list_objects(
            Bucket=COS_BUCKET,
            Prefix=prefix,
            Marker=marker,
            MaxKeys=1000,
        )
        for obj in resp.get("Contents", []):
            all_objects.append({
                "key": obj["Key"],
                "size": int(obj["Size"]),
                "last_modified": obj["LastModified"],
            })
        if resp.get("IsTruncated") == "true":
            marker = resp["NextMarker"]
        else:
            break
    return all_objects
```

**性能考虑**：`list_objects` 是 HTTP 请求，比本地 glob 慢。建议：
- 引入内存缓存（TTL 60秒），避免每次请求都列举
- 或在数据库（如 SQLite）中维护视频索引，上传/删除时同步更新

### 3.7 环境变量新增

```bash
# .env 新增
COS_REGION=ap-beijing
COS_SECRET_ID=AKIDxxxxxxxxxxxx
COS_SECRET_KEY=xxxxxxxxxxxxxxxx
COS_BUCKET=my-videos-1250000000
STORAGE_BACKEND=cos   # 或 "local"，用于切换后端
```

---

## 四、需要改动的文件清单

| 文件 | 改动 | 难度 |
|------|------|------|
| **新增 `storage.py`** | 存储后端抽象 + COS 实现 + Local 实现 | 中 |
| `app.py` | 将所有 `VIDEOS_DIR` 操作替换为 `storage.xxx()` 调用 | 中 |
| `x_timeline.py` | `download_video()` 改为上传到 COS | 低 |
| `templates/play.html` | `<video src>` 改为预签名 URL | 低 |
| `templates/index.html` | 缩略图 `src` 改为预签名 URL | 低 |
| `templates/author.html` | 同上 | 低 |
| `templates/authors.html` | 同上 | 低 |
| `templates/liked.html` | 同上 | 低 |
| `.env.example` | 新增 COS 配置项 | 低 |
| `requirements.txt` | 新增 `cos-python-sdk-v5` | 低 |
| `tests/test_app.py` | 适配 storage mock | 中 |

---

## 五、COS 费用估算（参考）

| 项目 | 单价（北京地域） | 说明 |
|------|----------------|------|
| 标准存储 | ¥0.118/GB/月 | 视频存储 |
| 下行流量 | ¥0.50/GB | 外网访问视频 |
| CDN 回源 | ¥0.15/GB | 如开启 CDN |
| 请求次数 | ¥0.01/万次 | PUT/GET/HEAD |
| **免费额度** | 50GB 存储 + 10GB 流量/月 | 6个月免费 |

假设 100GB 视频 + 50GB 月流量 → **约 ¥12/月存储 + ¥25/月流量 ≈ ¥37/月**。

---

## 六、推荐实施步骤

1. **创建 `storage.py`**：实现 `LocalStorage`（封装现有逻辑）和 `CosStorage`
2. **配置驱动**：通过 `STORAGE_BACKEND` 环境变量切换，默认 `local`
3. **改造 `app.py`**：将所有直接文件操作替换为 storage 接口调用
4. **改造下载流程**：`_do_download()` 和 `download_video()` 先下到临时文件再上传 COS
5. **预签名 URL**：`play()` 和所有模板中 `src` 改为预签名 URL
6. **删除 `serve_video` 路由**：不再需要 Flask 代理视频流
7. **迁移已有数据**：编写一次性脚本，将现有 `videos/` 目录上传到 COS
8. **测试**：在 `STORAGE_BACKEND=local` 模式下验证兼容性，然后切换到 `cos`
9. **部署**：在服务器添加 COS 环境变量，重启服务

---

## 七、关键风险与注意事项

1. **预签名 URL 过期**：视频播放中途 URL 可能过期（设 1 小时一般够用），前端需监听错误并刷新
2. **list_objects 性能**：对象数多时列举慢，建议缓存 + 数据库索引
3. **缩略图批量生成**：已有视频迁移后，需批量生成缩略图并上传
4. **COS SDK 依赖**：`cos-python-sdk-v5` 依赖 `requests`，与现有 `httpx` 无冲突
5. **临时文件清理**：上传后必须清理临时文件，避免服务器磁盘被占满
6. **安全**：COS 密钥与 X 认证密钥一样通过环境变量管理，不要硬编码
