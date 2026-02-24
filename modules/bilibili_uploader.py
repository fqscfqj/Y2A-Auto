#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import json
import logging
import os
import re
import traceback
from logging.handlers import RotatingFileHandler
from typing import Callable, Dict, List, Optional, Tuple, Union

from bilibili_api import video as bili_video
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


def _normalize_subtitle_language(language: Optional[str]) -> str:
    code = str(language or "").strip()
    if not code:
        return "zh-CN"

    normalized = code.replace("_", "-").lower()
    mapping = {
        "zh": "zh-CN",
        "zh-cn": "zh-CN",
        "zh-hans": "zh-Hans",
        "zh-hant": "zh-Hant",
        "en": "en-US",
        "en-us": "en-US",
        "ja": "ja",
        "jp": "ja",
        "ko": "ko",
    }
    return mapping.get(normalized, code)


def _parse_srt_timestamp_to_seconds(timestamp: str) -> float:
    ts = str(timestamp or "").strip().replace(".", ",")
    match = re.match(r"^(\d{1,2}):(\d{2}):(\d{2}),(\d{3})$", ts)
    if not match:
        return 0.0
    h, m, s, ms = [int(x) for x in match.groups()]
    return h * 3600 + m * 60 + s + ms / 1000.0


def _parse_srt_file_to_payload(subtitle_file_path: str) -> Optional[Dict[str, Union[float, str, List[dict]]]]:
    if not subtitle_file_path or not os.path.exists(subtitle_file_path):
        return None

    try:
        with open(subtitle_file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read().strip()
        if not content:
            return None

        content = content.replace("\r\n", "\n").replace("\r", "\n")

        strict_pattern = re.compile(
            r"(\d+)\n(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\n(.*?)(?=\n\d+\n|\Z)",
            re.DOTALL,
        )
        loose_pattern = re.compile(
            r"(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\n(.*?)(?=\n\d{1,2}:\d{2}:\d{2}|\Z)",
            re.DOTALL,
        )

        body = []

        strict_matches = strict_pattern.findall(content)
        if strict_matches:
            for _, start, end, text in strict_matches:
                merged_text = " ".join([line.strip() for line in text.split("\n") if line.strip()])
                if not merged_text:
                    continue
                body.append(
                    {
                        "from": _parse_srt_timestamp_to_seconds(start),
                        "to": _parse_srt_timestamp_to_seconds(end),
                        "location": 2,
                        "content": merged_text,
                    }
                )
        else:
            loose_matches = loose_pattern.findall(content)
            for start, end, text in loose_matches:
                merged_text = " ".join([line.strip() for line in text.split("\n") if line.strip()])
                if not merged_text:
                    continue
                body.append(
                    {
                        "from": _parse_srt_timestamp_to_seconds(start),
                        "to": _parse_srt_timestamp_to_seconds(end),
                        "location": 2,
                        "content": merged_text,
                    }
                )

        if not body:
            return None

        return {
            "font_size": 0.4,
            "font_color": "#FFFFFF",
            "background_alpha": 0.5,
            "background_color": "#9C27B0",
            "Stroke": "none",
            "body": body,
        }
    except Exception:
        return None


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
        subtitle_file_path: str = "",
        subtitle_language: str = "zh-CN",
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
                self.log(f"bilibili上传失败事件: {err}")

            self.log("开始上传到bilibili")
            result = asyncio.run(uploader.start())
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

            subtitle_result = None
            subtitle_error = None
            if subtitle_file_path and os.path.exists(subtitle_file_path):
                self.log(f"检测到字幕文件，开始上传字幕: {subtitle_file_path}")
                subtitle_ok, subtitle_payload = self.upload_subtitle(
                    bvid=bvid,
                    aid=aid,
                    subtitle_file_path=subtitle_file_path,
                    subtitle_language=subtitle_language,
                )
                if subtitle_ok:
                    subtitle_result = subtitle_payload
                    self.log(f"字幕上传成功: {subtitle_payload}")
                else:
                    subtitle_error = str(subtitle_payload)
                    self.log(f"字幕上传失败（不影响视频投稿结果）: {subtitle_error}")
            elif subtitle_file_path:
                self.log(f"字幕路径不存在，跳过字幕上传: {subtitle_file_path}")
            else:
                self.log("未提供字幕文件路径，跳过字幕上传")

            return True, {
                "bvid": bvid,
                "aid": aid,
                "url": video_url,
                "subtitle_uploaded": bool(subtitle_result),
                "subtitle_result": subtitle_result,
                "subtitle_error": subtitle_error,
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

    def upload_subtitle(
        self,
        bvid: Optional[str],
        aid: Optional[Union[int, str]],
        subtitle_file_path: str,
        subtitle_language: str = "zh-CN",
    ) -> Tuple[bool, Union[dict, str]]:
        try:
            subtitle_payload = _parse_srt_file_to_payload(subtitle_file_path)
            if not subtitle_payload:
                return False, f"字幕文件解析失败或为空: {subtitle_file_path}"

            credential = load_credential_from_file(self.cookie_file)
            video_obj = bili_video.Video(
                bvid=bvid or None,
                aid=int(aid) if aid not in (None, "", 0, "0") else None,
                credential=credential,
            )

            pages = asyncio.run(video_obj.get_pages())
            if not pages:
                return False, "未能获取稿件分P信息，无法上传字幕"

            cid = pages[0].get("cid")
            if not cid:
                return False, "未能获取分P cid，无法上传字幕"

            language_code = _normalize_subtitle_language(subtitle_language)
            result = asyncio.run(
                video_obj.submit_subtitle(
                    lan=language_code,
                    data=subtitle_payload,
                    submit=True,
                    sign=False,
                    cid=int(cid),
                )
            )
            return True, {
                "language": language_code,
                "cid": int(cid),
                "response": result,
            }
        except ArgsException as e:
            return False, f"字幕上传参数错误: {e}"
        except Exception as e:
            return False, f"字幕上传异常: {e}"
