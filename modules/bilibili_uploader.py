#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import json
import logging
import os
import re
import traceback
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, List, Optional, Tuple, Union

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


def _normalize_multiline_text(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    last_blank = True

    for raw_line in normalized.split("\n"):
        line = re.sub(r"[^\S\n]+", " ", raw_line).strip()
        if not line:
            if not last_blank and lines:
                lines.append("")
            last_blank = True
            continue
        lines.append(line)
        last_blank = False

    while lines and not lines[-1]:
        lines.pop()

    return "\n".join(lines)


def _truncate_multiline_text(text: str, max_len: int) -> str:
    normalized = _normalize_multiline_text(text)
    if len(normalized) <= max_len:
        return normalized
    if max_len <= 0:
        return ""
    if max_len <= 3:
        return normalized[:max_len]
    return normalized[: max_len - 3].rstrip() + "..."


def _remove_redundant_original_url(text: str, original_url: str) -> str:
    normalized = _normalize_multiline_text(text)
    visible_url = str(original_url or "").strip()
    if not normalized or not visible_url:
        return normalized

    cleaned_lines = []
    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if not line:
            cleaned_lines.append("")
            continue
        if line == visible_url:
            continue
        line = line.replace(visible_url, "").strip()
        if line:
            cleaned_lines.append(line)

    return _normalize_multiline_text("\n".join(cleaned_lines))


def format_bilibili_description(
    base_desc: str,
    original_url: str = "",
    original_uploader: str = "",
    original_upload_date: str = "",
    append_repost_notice: bool = True,
    max_len: int = 2000,
) -> str:
    summary = _remove_redundant_original_url(base_desc, original_url)
    is_repost = bool(original_url or original_uploader or original_upload_date)
    if not is_repost or not append_repost_notice:
        return _truncate_multiline_text(summary, max_len)

    notice_parts = ["本视频转载自YouTube"]
    if original_upload_date:
        notice_parts.append(f"原始上传时间：{original_upload_date}")
    if original_uploader:
        notice_parts.append(f"UP主：{original_uploader}")
    repost_notice = "，".join(notice_parts)

    if not summary:
        return _truncate_multiline_text(repost_notice, max_len)

    remain_len = max(0, max_len - len(repost_notice) - 2)
    summary = _truncate_multiline_text(summary, remain_len)
    if not summary:
        return _truncate_multiline_text(repost_notice, max_len)
    return f"{repost_notice}\n\n{summary}"


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
            safe_desc = _truncate_multiline_text(
                _remove_redundant_original_url(description or "", youtube_url or ""),
                2000,
            )
            safe_tags = [str(t).strip()[:20] for t in (tags or []) if str(t).strip()]
            safe_tags = safe_tags[:12]

            if not safe_title:
                return False, "标题为空，无法上传到bilibili"
            if not partition_id:
                return False, "分区ID为空，无法上传到bilibili"

            tid = int(partition_id)
            # 业务要求：bilibili强制按非自制（转载）投稿
            is_original = False
            source = youtube_url or None

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

            last_emitted_percent = 0.0
            last_emitted_text = ""

            def _emit_progress(text: str):
                nonlocal last_emitted_text
                if not progress_callback:
                    return
                progress_text = str(text or "").strip()
                if not progress_text:
                    return
                if progress_text == last_emitted_text:
                    return
                last_emitted_text = progress_text
                try:
                    progress_callback(progress_text)
                except Exception:
                    pass

            def _to_float(value: Any) -> Optional[float]:
                try:
                    if value is None:
                        return None
                    return float(value)
                except Exception:
                    return None

            def _extract_progress_percent(payload: Any) -> Optional[float]:
                if not isinstance(payload, dict):
                    return None

                candidates = []

                for key in (
                    "percent",
                    "progress",
                    "uploaded_percent",
                    "upload_percent",
                ):
                    value = _to_float(payload.get(key))
                    if value is None:
                        continue
                    if 0.0 <= value <= 1.0:
                        value *= 100.0
                    candidates.append(value)

                total_keys = (
                    "total_chunk_count",
                    "chunk_count",
                    "total_chunks",
                    "chunks_total",
                )
                current_keys = (
                    "chunk_number",
                    "chunk_index",
                    "uploaded_chunk_count",
                    "uploaded_chunks",
                    "chunk_id",
                    "current_chunk",
                )

                totals = [_to_float(payload.get(k)) for k in total_keys]
                currents = [_to_float(payload.get(k)) for k in current_keys]

                for total in totals:
                    if total is None or total <= 0:
                        continue
                    for current in currents:
                        if current is None:
                            continue
                        candidates.append((current / total) * 100.0)
                        candidates.append(((current + 1.0) / total) * 100.0)

                normalized = [
                    max(0.0, min(100.0, value))
                    for value in candidates
                    if value is not None
                ]
                if not normalized:
                    return None

                # 优先选择“略大于上一进度”的最小值，兼容 chunk 索引基数差异
                forward = [value for value in normalized if value > (last_emitted_percent + 0.05)]
                if forward:
                    return min(forward)
                return max(normalized)

            @uploader.on(video_uploader.VideoUploaderEvents.AFTER_CHUNK.value)
            def on_after_chunk(data):
                nonlocal last_emitted_percent
                try:
                    percent = _extract_progress_percent(data)
                    if percent is None:
                        _emit_progress("上传中...")
                        return
                    if percent < last_emitted_percent:
                        percent = last_emitted_percent
                    last_emitted_percent = min(100.0, percent)
                    _emit_progress(f"{last_emitted_percent:.1f}%")
                except Exception:
                    pass

            @uploader.on(video_uploader.VideoUploaderEvents.FAILED.value)
            def on_failed(data):
                err = data.get("err") if isinstance(data, dict) else data
                self.log(f"bilibili上传失败事件: {err}")

            _emit_progress("0.0%")
            self.log("开始上传到bilibili")
            result = asyncio.run(uploader.start())
            last_emitted_percent = 100.0
            _emit_progress("100.0%")
            self.log(f"bilibili上传完成: {result}")

            if not isinstance(result, dict):
                return False, "bilibili返回结果格式异常"

            bvid = result.get("bvid")
            aid = result.get("aid")
            if not bvid and isinstance(result.get("data"), dict):
                bvid = result["data"].get("bvid")
                aid = result["data"].get("aid", aid)

            if not bvid and not aid:
                return False, f"bilibili返回中未找到 bvid/aid: {result}"

            video_url = f"https://www.bilibili.com/video/{bvid}" if bvid else ""

            return True, {
                "bvid": bvid,
                "aid": aid,
                "url": video_url,
            }

        except ArgsException as e:
            return False, (
                "bilibili-api 缺少网络后端依赖，请安装 httpx/aiohttp/curl_cffi。"
                f" 详细错误: {e}"
            )
        except Exception as e:
            self.log(f"bilibili上传异常: {e}")
            self.log(traceback.format_exc())
            return False, f"bilibili上传异常: {e}"
