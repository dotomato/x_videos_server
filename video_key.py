"""
VideoKey — 视频文件名格式的唯一来源。

文件名格式: {tweet_id}_{index}.mp4
存储路径格式: {author}/{tweet_id}_{index}.mp4

所有生成和解析操作都通过此模块完成，避免格式知识散落各处。
"""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VideoKey:
    """代表一个视频文件的标识符。"""
    tweet_id: str
    index: int

    def filename(self) -> str:
        """返回视频文件名：{tweet_id}_{index}.mp4"""
        return f"{self.tweet_id}_{self.index}.mp4"

    def jpg_filename(self) -> str:
        """返回缩略图文件名：{tweet_id}_{index}.jpg"""
        return f"{self.tweet_id}_{self.index}.jpg"

    def storage_key(self, author: str) -> str:
        """返回存储路径：{author}/{tweet_id}_{index}.mp4"""
        return f"{author}/{self.filename()}"

    def jpg_storage_key(self, author: str) -> str:
        """返回缩略图存储路径：{author}/{tweet_id}_{index}.jpg"""
        return f"{author}/{self.jpg_filename()}"

    @staticmethod
    def from_filename(filename: str) -> VideoKey | None:
        """从文件名解析 VideoKey，解析失败返回 None。"""
        stem = Path(filename).stem
        parts = stem.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            return VideoKey(tweet_id=parts[0], index=int(parts[1]))
        return None

    @staticmethod
    def from_stem(stem: str) -> VideoKey | None:
        """从不含扩展名的文件名 stem 解析 VideoKey，解析失败返回 None。"""
        parts = stem.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            return VideoKey(tweet_id=parts[0], index=int(parts[1]))
        return None
