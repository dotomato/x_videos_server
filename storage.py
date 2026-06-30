#!/usr/bin/env python3.11
"""
存储后端抽象层

通过 STORAGE_BACKEND 环境变量选择后端：
  - "local"（默认）：使用本地文件系统（原有逻辑）
  - "cos"：使用腾讯云 COS 对象存储

使用方式：
  from storage import get_storage
  store = get_storage()
  store.upload_file(key, local_path)
  url = store.get_url(key)
"""

import os
import logging
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)

# 对象键前缀（COS 中所有视频和缩略图的公共前缀）
COS_PREFIX = "videos/"


# ─── 抽象基类 ────────────────────────────────────────────────────────────────

class StorageBackend(ABC):
    """存储后端抽象基类。"""

    @abstractmethod
    def upload_file(self, key: str, local_path: str | Path,
                    content_type: str = "") -> None:
        """上传本地文件到存储后端。"""

    @abstractmethod
    def upload_bytes(self, key: str, data: bytes,
                     content_type: str = "") -> None:
        """上传字节数据到存储后端。"""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """检查对象是否存在。"""

    @abstractmethod
    def delete(self, key: str) -> None:
        """删除对象。"""

    @abstractmethod
    def list_objects(self, prefix: str = "") -> list[dict]:
        """列举指定前缀下的对象。
        返回 list[dict]，每项包含 key, size, last_modified。
        """

    @abstractmethod
    def get_url(self, key: str, expires: int = 3600) -> str:
        """获取对象的访问 URL。
        对于 COS：返回预签名 URL（默认 1 小时有效）。
        对于 Local：返回 /videos/{author}/{filename} 格式的路径。
        """

    @abstractmethod
    def get_size(self, key: str) -> int:
        """获取单个对象的大小（字节）。"""

    @abstractmethod
    def total_size(self, prefix: str = "") -> int:
        """获取指定前缀下所有对象的总大小（字节）。"""

    @abstractmethod
    def get_file(self, key: str) -> bytes:
        """下载对象内容为字节，供缩略图生成等场景使用。"""

    @abstractmethod
    def put_file(self, key: str, data: bytes, content_type: str = "") -> None:
        """将字节数据写入对象（内部使用，等同于 upload_bytes）。"""

    @abstractmethod
    def get_disk_free(self) -> str:
        """获取存储剩余空间的可读字符串。COS 返回 '∞'。"""

    def url_requires_redirect(self) -> bool:
        """返回 True 表示 get_url() 产生外部 URL（需要 HTTP redirect）。
        Local 返回 False（Flask 直接 serve），COS 返回 True（预签名 URL）。
        """
        return False

    def csp_media_domain(self) -> str:
        """返回需要添加到 CSP img-src/media-src 的域名，不需要则返回空字符串。"""
        return ""


# ─── 本地文件系统实现 ────────────────────────────────────────────────────────

class LocalStorage(StorageBackend):
    """基于本地文件系统的存储实现（原有逻辑）。"""

    def __init__(self, videos_dir: Path):
        self.videos_dir = videos_dir

    def _full_path(self, key: str) -> Path:
        return self.videos_dir / key

    def upload_file(self, key: str, local_path: str | Path,
                    content_type: str = "") -> None:
        dest = self._full_path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(str(local_path), str(dest))

    def upload_bytes(self, key: str, data: bytes,
                     content_type: str = "") -> None:
        dest = self._full_path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    def exists(self, key: str) -> bool:
        return self._full_path(key).is_file()

    def delete(self, key: str) -> None:
        p = self._full_path(key)
        if p.exists():
            p.unlink()

    def list_objects(self, prefix: str = "") -> list[dict]:
        if not self.videos_dir.exists():
            return []
        search_dir = self.videos_dir / prefix if prefix else self.videos_dir
        results = []
        # 匹配 videos/*/*.mp4 和 videos/*/*.jpg
        for p in self.videos_dir.glob("*/*"):
            if p.is_file() and (prefix == "" or str(p.relative_to(self.videos_dir)).startswith(prefix)):
                rel = str(p.relative_to(self.videos_dir))
                results.append({
                    "key": rel.replace("\\", "/"),
                    "size": p.stat().st_size,
                    "last_modified": p.stat().st_mtime,
                })
        return results

    def get_url(self, key: str, expires: int = 3600) -> str:
        return f"/videos/{key}"

    def get_size(self, key: str) -> int:
        p = self._full_path(key)
        return p.stat().st_size if p.is_file() else 0

    def total_size(self, prefix: str = "") -> int:
        if not self.videos_dir.exists():
            return 0
        return sum(
            p.stat().st_size
            for p in self.videos_dir.glob("*/*.mp4")
        )

    def get_disk_free(self) -> str:
        import os
        import shutil
        target = self.videos_dir if self.videos_dir.exists() else Path(__file__).parent
        try:
            usage = shutil.disk_usage(str(target))
            free = usage.free
        except Exception:
            return "unknown"
        if free >= 1 << 30:
            return f"{free / (1 << 30):.2f} GiB"
        if free >= 1 << 20:
            return f"{free / (1 << 20):.1f} MiB"
        return f"{free} B"

    def get_file(self, key: str) -> bytes:
        return self._full_path(key).read_bytes()

    def put_file(self, key: str, data: bytes, content_type: str = "") -> None:
        self.upload_bytes(key, data, content_type)


# ─── 腾讯云 COS 实现 ────────────────────────────────────────────────────────

class CosStorage(StorageBackend):
    """基于腾讯云 COS 的存储实现。"""

    def __init__(self):
        from qcloud_cos import CosConfig, CosS3Client

        region = os.environ["COS_REGION"]
        secret_id = os.environ["COS_SECRET_ID"]
        secret_key = os.environ["COS_SECRET_KEY"]
        self.bucket = os.environ["COS_BUCKET"]

        scheme = os.environ.get("COS_SCHEME", "https")

        config = CosConfig(
            Region=region,
            SecretId=secret_id,
            SecretKey=secret_key,
            Scheme=scheme,
        )
        self.client = CosS3Client(config)
        logger.info("COS 存储后端已初始化: region=%s bucket=%s", region, self.bucket)

    def _cos_key(self, key: str) -> str:
        """将内部 key 转为 COS 对象键（添加前缀）。"""
        if key.startswith(COS_PREFIX):
            return key
        return f"{COS_PREFIX}{key}"

    def upload_file(self, key: str, local_path: str | Path,
                    content_type: str = "") -> None:
        cos_key = self._cos_key(key)
        kwargs = {}
        if content_type:
            kwargs["ContentType"] = content_type
        self.client.upload_file(
            Bucket=self.bucket,
            Key=cos_key,
            LocalFilePath=str(local_path),
            PartSize=5,
            MAXThread=5,
            **kwargs,
        )
        logger.info("COS 上传完成: %s", cos_key)

    def upload_bytes(self, key: str, data: bytes,
                     content_type: str = "") -> None:
        cos_key = self._cos_key(key)
        kwargs = {}
        if content_type:
            kwargs["ContentType"] = content_type
        self.client.put_object(
            Bucket=self.bucket,
            Key=cos_key,
            Body=data,
            **kwargs,
        )
        logger.info("COS 上传完成(bytes): %s", cos_key)

    def exists(self, key: str) -> bool:
        cos_key = self._cos_key(key)
        return self.client.object_exists(
            Bucket=self.bucket,
            Key=cos_key,
        )

    def delete(self, key: str) -> None:
        cos_key = self._cos_key(key)
        self.client.delete_object(
            Bucket=self.bucket,
            Key=cos_key,
        )
        logger.info("COS 已删除: %s", cos_key)

    def list_objects(self, prefix: str = "") -> list[dict]:
        cos_prefix = self._cos_key(prefix) if prefix else COS_PREFIX
        results = []
        marker = ""
        while True:
            resp = self.client.list_objects(
                Bucket=self.bucket,
                Prefix=cos_prefix,
                Marker=marker,
                MaxKeys=1000,
            )
            for obj in resp.get("Contents", []):
                # 去掉 COS_PREFIX 前缀返回内部 key
                obj_key = obj["Key"]
                if obj_key.startswith(COS_PREFIX):
                    obj_key = obj_key[len(COS_PREFIX):]
                results.append({
                    "key": obj_key,
                    "size": int(obj["Size"]),
                    "last_modified": obj["LastModified"],
                })
            if resp.get("IsTruncated") == "true":
                marker = resp["NextMarker"]
            else:
                break
        return results

    def get_url(self, key: str, expires: int = 3600) -> str:
        cos_key = self._cos_key(key)
        return self.client.get_presigned_download_url(
            Bucket=self.bucket,
            Key=cos_key,
            Expired=expires,
        )

    def get_size(self, key: str) -> int:
        cos_key = self._cos_key(key)
        resp = self.client.head_object(
            Bucket=self.bucket,
            Key=cos_key,
        )
        return int(resp.get("Content-Length", 0))

    def total_size(self, prefix: str = "") -> int:
        """列举所有 .mp4 对象并累加大小。"""
        objects = self.list_objects(prefix)
        return sum(o["size"] for o in objects if o["key"].endswith(".mp4"))

    def get_disk_free(self) -> str:
        return "∞"

    def url_requires_redirect(self) -> bool:
        return True

    def csp_media_domain(self) -> str:
        bucket = os.environ.get("COS_BUCKET", "")
        region = os.environ.get("COS_REGION", "")
        return f"{bucket}.cos.{region}.myqcloud.com"

    def get_file(self, key: str) -> bytes:
        cos_key = self._cos_key(key)
        resp = self.client.get_object(
            Bucket=self.bucket,
            Key=cos_key,
        )
        # StreamBody: read raw stream to bytes
        return resp["Body"].get_raw_stream().read()

    def put_file(self, key: str, data: bytes, content_type: str = "") -> None:
        self.upload_bytes(key, data, content_type)


# ─── 工厂函数 ────────────────────────────────────────────────────────────────

_storage_instance: StorageBackend | None = None


def get_storage() -> StorageBackend:
    """获取全局存储后端实例（单例）。"""
    global _storage_instance
    if _storage_instance is not None:
        return _storage_instance

    backend = os.environ.get("STORAGE_BACKEND", "local").lower()
    if backend == "cos":
        _storage_instance = CosStorage()
    else:
        from app import BASE_DIR
        _storage_instance = LocalStorage(BASE_DIR / "videos")

    return _storage_instance


def reset_storage():
    """重置存储实例（仅用于测试）。"""
    global _storage_instance
    _storage_instance = None
