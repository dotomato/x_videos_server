#!/usr/bin/env python3
"""
将本地 videos/ 目录下的视频和缩略图批量迁移到腾讯云 COS。

用法：
    # 先加载 .env 中的环境变量
    source .env && export $(grep -v '^#' .env | cut -d= -f1)
    python3 migrate_to_cos.py

    # 试运行（只打印，不上传）
    python3 migrate_to_cos.py --dry-run

    # 上传后删除本地文件
    python3 migrate_to_cos.py --delete-after-upload

功能：
    1. 扫描本地 videos/ 下所有 mp4 和 jpg 文件
    2. 检查 COS 上是否已存在，跳过已存在的
    3. 上传到 COS（multipart，5MB 分片，5 线程）
    4. 可选：上传成功后删除本地文件
"""

import os
import sys
import time
import logging
from pathlib import Path

# 项目根目录
BASE_DIR = Path(__file__).parent
VIDEOS_DIR = BASE_DIR / "videos"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def get_cos_client():
    """初始化 COS 客户端，需要环境变量。"""
    from qcloud_cos import CosConfig, CosS3Client

    region = os.environ.get("COS_REGION")
    secret_id = os.environ.get("COS_SECRET_ID")
    secret_key = os.environ.get("COS_SECRET_KEY")
    bucket = os.environ.get("COS_BUCKET")
    scheme = os.environ.get("COS_SCHEME", "https")

    missing = [k for k, v in {
        "COS_REGION": region,
        "COS_SECRET_ID": secret_id,
        "COS_SECRET_KEY": secret_key,
        "COS_BUCKET": bucket,
    }.items() if not v]

    if missing:
        logger.error("缺少环境变量: %s", ", ".join(missing))
        logger.error("请先 source .env 并 export 相关变量")
        sys.exit(1)

    config = CosConfig(
        Region=region,
        SecretId=secret_id,
        SecretKey=secret_key,
        Scheme=scheme,
    )
    client = CosS3Client(config)
    return client, bucket


def scan_local_files():
    """扫描本地 videos/ 目录，返回 [(key, full_path), ...]"""
    files = []
    if not VIDEOS_DIR.exists():
        logger.error("本地 videos/ 目录不存在: %s", VIDEOS_DIR)
        return files

    for p in sorted(VIDEOS_DIR.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in (".mp4", ".jpg"):
            continue
        rel = str(p.relative_to(VIDEOS_DIR)).replace("\\", "/")
        files.append((rel, p))

    return files


def check_cos_exists(client, bucket, cos_key):
    """检查 COS 上对象是否已存在。"""
    try:
        return client.object_exists(Bucket=bucket, Key=cos_key)
    except Exception as e:
        logger.warning("检查对象存在性失败 %s: %s", cos_key, e)
        return False


def human_size(size_bytes):
    """字节数转可读字符串。"""
    if size_bytes >= 1 << 30:
        return f"{size_bytes / (1 << 30):.2f} GiB"
    if size_bytes >= 1 << 20:
        return f"{size_bytes / (1 << 20):.1f} MiB"
    return f"{size_bytes / (1 << 10):.1f} KiB"


def main():
    dry_run = "--dry-run" in sys.argv
    delete_after = "--delete-after-upload" in sys.argv

    if dry_run:
        logger.info("=== 试运行模式（不会实际上传或删除）===")

    # 1. 扫描本地文件
    files = scan_local_files()
    if not files:
        logger.info("没有找到需要迁移的文件")
        return

    mp4_count = sum(1 for k, _ in files if k.endswith(".mp4"))
    jpg_count = sum(1 for k, _ in files if k.endswith(".jpg"))
    total_size = sum(p.stat().st_size for _, p in files)
    logger.info("扫描完成: %d 个 mp4, %d 个 jpg, 总计 %s", mp4_count, jpg_count, human_size(total_size))

    if dry_run:
        for key, path in files:
            logger.info("  %s (%s)", key, human_size(path.stat().st_size))
        return

    # 2. 初始化 COS 客户端
    client, bucket = get_cos_client()

    # 3. 逐个上传
    uploaded = 0
    skipped = 0
    failed = 0
    bytes_uploaded = 0

    for i, (key, path) in enumerate(files, 1):
        cos_key = f"videos/{key}"
        file_size = path.stat().st_size

        # 检查是否已存在
        if check_cos_exists(client, bucket, cos_key):
            logger.info("[%d/%d] 跳过(已存在): %s", i, len(files), key)
            skipped += 1
            if delete_after:
                # COS 上已存在，也可以安全删除本地文件
                logger.info("  删除本地文件: %s", path)
                path.unlink()
            continue

        # 上传
        content_type = ""
        if key.endswith(".mp4"):
            content_type = "video/mp4"
        elif key.endswith(".jpg"):
            content_type = "image/jpeg"

        try:
            logger.info("[%d/%d] 上传: %s (%s)", i, len(files), key, human_size(file_size))
            t0 = time.time()

            kwargs = {}
            if content_type:
                kwargs["ContentType"] = content_type

            client.upload_file(
                Bucket=bucket,
                Key=cos_key,
                LocalFilePath=str(path),
                PartSize=5,
                MAXThread=5,
                **kwargs,
            )

            elapsed = time.time() - t0
            speed = file_size / elapsed if elapsed > 0 else 0
            logger.info("  完成: %.1fs, %s/s", elapsed, human_size(int(speed)))

            uploaded += 1
            bytes_uploaded += file_size

            # 验证上传
            if not check_cos_exists(client, bucket, cos_key):
                logger.warning("  上传后验证失败: %s", key)
                failed += 1
                uploaded -= 1
                bytes_uploaded -= file_size
                continue

            # 删除本地文件
            if delete_after:
                logger.info("  删除本地文件: %s", path)
                path.unlink()

        except Exception as e:
            logger.error("  上传失败: %s - %s", key, e)
            failed += 1

    # 4. 清理空目录
    if delete_after:
        cleaned = 0
        for author_dir in sorted(VIDEOS_DIR.iterdir()):
            if author_dir.is_dir() and not any(author_dir.iterdir()):
                author_dir.rmdir()
                logger.info("删除空目录: %s", author_dir.name)
                cleaned += 1
        if VIDEOS_DIR.is_dir() and not any(VIDEOS_DIR.iterdir()):
            VIDEOS_DIR.rmdir()
            logger.info("videos/ 目录已清空并删除")

    # 5. 汇总
    logger.info("=" * 50)
    logger.info("迁移完成!")
    logger.info("  上传: %d 个文件 (%s)", uploaded, human_size(bytes_uploaded))
    logger.info("  跳过(已存在): %d", skipped)
    logger.info("  失败: %d", failed)
    if delete_after:
        logger.info("  本地文件已删除")


if __name__ == "__main__":
    main()
