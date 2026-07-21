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

from .bili_sdk import video_uploader
from .bili_sdk.exceptions import ArgsException, ResponseCodeException

from .bilibili_runtime import configure_bilibili_runtime
from .bilibili_auth import load_credential_from_file, validate_credential_remote
from .utils import get_app_subdir

BILIBILI_TITLE_LIMIT = 80
BILIBILI_DESCRIPTION_LIMIT = 2000


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
    max_len: int = BILIBILI_DESCRIPTION_LIMIT,
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


def _extract_response_code_from_exception(exc: Exception) -> Optional[int]:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    if isinstance(code, str) and code.isdigit():
        return int(code)

    info = getattr(exc, "raw", None)
    if isinstance(info, dict):
        raw_code = info.get("code")
        if isinstance(raw_code, int):
            return raw_code
        if isinstance(raw_code, str) and raw_code.isdigit():
            return int(raw_code)

    match = re.search(r"错误代码[:：]\s*(\d+)", str(exc))
    if match:
        return int(match.group(1))
    return None


def _compact_exception_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _format_bilibili_exception(exc: Exception) -> str:
    code = _extract_response_code_from_exception(exc)
    message = _compact_exception_text(getattr(exc, "msg", "") or str(exc))

    raw = getattr(exc, "raw", None)
    if isinstance(raw, dict):
        raw_msg = _compact_exception_text(str(raw.get("message", "") or ""))
        if raw_msg and raw_msg not in message:
            message = f"{message} | 接口消息: {raw_msg}" if message else raw_msg

    if code is not None and message:
        return f"接口返回错误代码：{code}，信息：{message}"
    if code is not None:
        return f"接口返回错误代码：{code}"
    return message or "未知错误"


def _is_bilibili_http_406(exc: Exception) -> bool:
    code = _extract_response_code_from_exception(exc)
    text = _compact_exception_text(str(exc))
    return code == 406 or "状态码：406" in text or "status code: 406" in text.lower()


def _bilibili_406_hint() -> str:
    return (
        "bilibili上传被 preupload 接口返回 406 拒绝。"
        "这通常是 B 站风控导致，可能与 Cookie/buvid 状态、服务器 IP 环境或网络指纹有关。"
        "已启用 curl_cffi 浏览器指纹伪装；如仍失败，请重新扫码登录或更换网络环境后重试。"
    )


class _BilibiliChunkProgress:
    def __init__(self):
        self._completed = {}
        self._totals = {}

    def record(self, payload: Any) -> Optional[float]:
        if not isinstance(payload, dict):
            return None
        page = payload.get("page")
        chunk_number = payload.get("chunk_number")
        total_chunk_count = payload.get("total_chunk_count")
        if page is None or not isinstance(chunk_number, int):
            return None
        if not isinstance(total_chunk_count, int) or total_chunk_count <= 0:
            return None

        page_key = id(page)
        self._totals[page_key] = total_chunk_count
        self._completed.setdefault(page_key, set()).add(chunk_number)
        completed = sum(len(chunks) for chunks in self._completed.values())
        total = sum(self._totals.values())
        if total <= 0:
            return None
        return min(95.0, completed / total * 95.0)


class BilibiliUploader:
    """Bilibili uploader based on the internal SDK subset."""

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
        title_limit: int = BILIBILI_TITLE_LIMIT,
        description_limit: int = BILIBILI_DESCRIPTION_LIMIT,
    ) -> Tuple[bool, Union[dict, str]]:
        self.task_id = task_id
        self.logger = setup_task_logger(task_id or "unknown")

        try:
            configure_bilibili_runtime()

            if not os.path.exists(video_file_path):
                return False, f"视频文件不存在: {video_file_path}"
            if not os.path.exists(cover_file_path):
                return False, f"封面文件不存在: {cover_file_path}"

            credential = load_credential_from_file(self.cookie_file)
            credential_ok, credential_msg = validate_credential_remote(credential)
            if not credential_ok:
                return False, f"Bilibili登录态无效: {credential_msg}。请在设置页重新扫码登录后重试上传。"

            safe_title_limit = int(title_limit or BILIBILI_TITLE_LIMIT)
            safe_desc_limit = int(description_limit or BILIBILI_DESCRIPTION_LIMIT)
            safe_title = _compact_text(title or "", safe_title_limit)
            safe_desc = _truncate_multiline_text(
                _remove_redundant_original_url(description or "", youtube_url or ""),
                safe_desc_limit,
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

            last_emitted_text = ""
            chunk_progress = _BilibiliChunkProgress()
            page_positions = {
                id(item): index for index, item in enumerate(uploader.pages, 1)
            }
            page_count = len(uploader.pages)

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

            def _page_label(data: Any) -> str:
                page_obj = data.get("page") if isinstance(data, dict) else None
                page_number = page_positions.get(id(page_obj), 1)
                return f"第{page_number}/{page_count}P"

            def _event_error(data: Any) -> str:
                err = data.get("err") if isinstance(data, dict) else data
                return _compact_exception_text(str(err)) or "未知错误"

            @uploader.on(video_uploader.VideoUploaderEvents.AFTER_CHUNK.value)
            def on_after_chunk(data):
                try:
                    percent = chunk_progress.record(data)
                    if percent is None:
                        _emit_progress("上传中...")
                        return
                    _emit_progress(f"{percent:.1f}%")
                except Exception:
                    pass

            @uploader.on(video_uploader.VideoUploaderEvents.CHUNK_FAILED.value)
            def on_chunk_failed(data):
                if not isinstance(data, dict):
                    self.log("Bilibili 分块上传失败")
                    return
                chunk_number = int(data.get("chunk_number", 0)) + 1
                total_chunks = data.get("total_chunk_count", "?")
                attempt = data.get("attempt", "?")
                max_attempts = data.get("max_attempts", "?")
                info = _compact_exception_text(str(data.get("info") or "未知错误"))
                if data.get("retrying"):
                    delay = data.get("retry_delay_seconds", 0)
                    self.log(
                        f"Bilibili {_page_label(data)} 分块 {chunk_number}/{total_chunks} 上传失败，"
                        f"尝试 {attempt}/{max_attempts}，{delay} 秒后重试：{info}"
                    )
                else:
                    self.log(
                        f"Bilibili {_page_label(data)} 分块 {chunk_number}/{total_chunks} 上传失败，"
                        f"已停止重试（{attempt}/{max_attempts}）：{info}"
                    )

            @uploader.on(video_uploader.VideoUploaderEvents.PRE_PAGE_SUBMIT.value)
            def on_pre_page_submit(data):
                _emit_progress("95.0%")
                self.log(f"Bilibili {_page_label(data)} 分块上传完成，正在提交分P")

            @uploader.on(video_uploader.VideoUploaderEvents.AFTER_PAGE_SUBMIT.value)
            def on_after_page_submit(data):
                self.log(f"Bilibili {_page_label(data)} 分P提交成功")

            @uploader.on(video_uploader.VideoUploaderEvents.PAGE_SUBMIT_FAILED.value)
            def on_page_submit_failed(data):
                self.log(f"Bilibili {_page_label(data)} 分P提交失败：{_event_error(data)}")

            @uploader.on(video_uploader.VideoUploaderEvents.PRE_COVER.value)
            def on_pre_cover(_data):
                _emit_progress("96.0%")
                self.log("开始上传Bilibili封面")

            @uploader.on(video_uploader.VideoUploaderEvents.AFTER_COVER.value)
            def on_after_cover(_data):
                _emit_progress("98.0%")
                self.log("Bilibili封面上传成功")

            @uploader.on(video_uploader.VideoUploaderEvents.COVER_FAILED.value)
            def on_cover_failed(data):
                self.log(f"Bilibili封面上传失败：{_event_error(data)}")

            @uploader.on(video_uploader.VideoUploaderEvents.PRE_SUBMIT.value)
            def on_pre_submit(_data):
                _emit_progress("99.0%")
                self.log("视频和封面上传完成，正在提交Bilibili投稿")

            @uploader.on(video_uploader.VideoUploaderEvents.AFTER_SUBMIT.value)
            def on_after_submit(_data):
                self.log("Bilibili投稿接口提交成功")

            @uploader.on(video_uploader.VideoUploaderEvents.SUBMIT_FAILED.value)
            def on_submit_failed(data):
                self.log(f"Bilibili投稿提交失败：{_event_error(data)}")

            @uploader.on(video_uploader.VideoUploaderEvents.FAILED.value)
            def on_failed(data):
                err = data.get("err") if isinstance(data, dict) else data
                if isinstance(err, ResponseCodeException):
                    self.log(f"bilibili上传失败事件: {_format_bilibili_exception(err)}")
                else:
                    self.log(f"bilibili上传失败事件: {_compact_exception_text(str(err))}")

            _emit_progress("0.0%")
            self.log("开始上传到bilibili")
            try:
                result = asyncio.run(uploader.start())
            except RuntimeError:
                # 已有事件循环时，在新线程中运行
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    result = pool.submit(asyncio.run, uploader.start()).result()

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
        except ResponseCodeException as e:
            pretty_error = _format_bilibili_exception(e)
            if _is_bilibili_http_406(e):
                pretty_error = _bilibili_406_hint()
            self.log(f"bilibili上传异常: {pretty_error}")
            return False, f"bilibili上传异常: {pretty_error}"
        except Exception as e:
            if _is_bilibili_http_406(e):
                hint = _bilibili_406_hint()
                self.log(f"bilibili上传异常: {hint}")
                self.log(traceback.format_exc())
                return False, f"bilibili上传异常: {hint}"
            self.log(f"bilibili上传异常: {_compact_exception_text(str(e))}")
            self.log(traceback.format_exc())
            return False, f"bilibili上传异常: {_compact_exception_text(str(e))}"
