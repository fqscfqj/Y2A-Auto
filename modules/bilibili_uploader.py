#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import logging
import os
import re
import traceback
from logging.handlers import RotatingFileHandler
from typing import Callable, List, Optional, Tuple, Union

from bilibili_api import video_uploader
from bilibili_api.exceptions import ArgsException

from .bilibili_auth import load_credential_from_file
from .utils import get_app_subdir


def setup_task_logger(task_id):
    log_dir = get_app_subdir("logs")
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, f"task_{task_id}.log")
    logger = logging.getLogger(f"bilibili_uploader_{task_id}")

    if not logger.handlers:
        logger.setLevel(logging.INFO)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=10485760, backupCount=5, encoding="utf-8"
        )
        file_formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
        logger.propagate = False

    return logger


def _compact_text(text: str, max_len: int) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..." if max_len > 3 else text[:max_len]


class BilibiliUploader:
    """Bilibili uploader based on bilibili-api-python."""

    def __init__(self, cookie_file: str):
        self.cookie_file = cookie_file
        self.logger = None
        self.task_id = None

    def log(self, message: str):
        if self.logger:
            self.logger.info(message)
        else:
            print(message)

    def upload_video(
        self,
        video_file_path: str,
        cover_file_path: str,
        title: str,
        description: str,
        tags: List[str],
        partition_id: Union[str, int],
        youtube_url: str = "",
        default_repost: bool = True,
        task_id: Optional[str] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[bool, Union[dict, str]]:
        self.task_id = task_id
        self.logger = setup_task_logger(task_id or "unknown")

        try:
            if not os.path.exists(video_file_path):
                return False, f"视频文件不存在: {video_file_path}"
            if not os.path.exists(cover_file_path):
                return False, f"封面文件不存在: {cover_file_path}"

            credential = load_credential_from_file(self.cookie_file)

            safe_title = _compact_text(title or "", 80)
            safe_desc = _compact_text(description or "", 2000)
            safe_tags = [str(t).strip()[:20] for t in (tags or []) if str(t).strip()]
            safe_tags = safe_tags[:12]

            if not safe_title:
                return False, "标题为空，无法上传到B站"
            if not partition_id:
                return False, "分区ID为空，无法上传到B站"

            tid = int(partition_id)
            is_original = not bool(default_repost)
            source = youtube_url if (not is_original and youtube_url) else None

            meta = video_uploader.VideoMeta(
                tid=tid,
                title=safe_title,
                desc=safe_desc,
                cover=cover_file_path,
                tags=safe_tags,
                original=is_original,
                source=source,
                no_reprint=False,
            )

            page = video_uploader.VideoUploaderPage(
                path=video_file_path,
                title=safe_title,
            )
            uploader = video_uploader.VideoUploader(
                pages=[page],
                meta=meta,
                credential=credential,
                cover=cover_file_path,
            )

            @uploader.on(video_uploader.VideoUploaderEvents.AFTER_CHUNK.value)
            def on_after_chunk(data):
                try:
                    total = int(data.get("total_chunk_count", 0) or 0)
                    idx = int(data.get("chunk_number", -1) or -1)
                    if total > 0 and idx >= 0:
                        pct = ((idx + 1) / total) * 100
                        text = f"{pct:.1f}%"
                        if progress_callback:
                            progress_callback(text)
                except Exception:
                    pass

            @uploader.on(video_uploader.VideoUploaderEvents.FAILED.value)
            def on_failed(data):
                err = data.get("err") if isinstance(data, dict) else data
                self.log(f"B站上传失败事件: {err}")

            self.log("开始上传到B站")
            result = asyncio.run(uploader.start())
            self.log(f"B站上传完成: {result}")

            if not isinstance(result, dict):
                return False, "B站返回结果格式异常"

            bvid = result.get("bvid")
            aid = result.get("aid")
            if not bvid and isinstance(result.get("data"), dict):
                bvid = result["data"].get("bvid")
                aid = result["data"].get("aid", aid)

            if not bvid and not aid:
                return False, f"B站返回中未找到 bvid/aid: {result}"

            video_url = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
            return True, {"bvid": bvid, "aid": aid, "url": video_url}

        except ArgsException as e:
            return False, (
                "bilibili-api 缺少网络后端依赖，请安装 httpx/aiohttp/curl_cffi。"
                f" 详细错误: {e}"
            )
        except Exception as e:
            self.log(f"B站上传异常: {e}")
            self.log(traceback.format_exc())
            return False, f"B站上传异常: {e}"

