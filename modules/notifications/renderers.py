from __future__ import annotations

import json
from typing import Any

from .models import (
    EVENT_LOGIN_LOCKED,
    EVENT_LOGIN_SUCCESS,
    EVENT_QR_LOGIN_FAILED,
    EVENT_QR_LOGIN_SUCCESS,
    EVENT_TASK_ADDED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    NotificationEvent,
    NotificationMessage,
)


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _truncate(text: str, limit: int = 240) -> str:
    clean = _as_text(text)
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 1)] + "…"


def _upload_target_label(upload_target: Any) -> str:
    target = _as_text(upload_target).lower()
    if target == "both":
        return "AcFun + bilibili"
    if target == "bilibili":
        return "bilibili"
    return "AcFun"


def _task_title(payload: dict[str, Any]) -> str:
    for key in ("video_title_translated", "video_title_original", "title"):
        value = _as_text(payload.get(key))
        if value:
            return value
    return "未命名任务"


def _task_platform_result(payload: dict[str, Any]) -> str:
    succeeded = []
    if payload.get("acfun_uploaded"):
        succeeded.append("AcFun")
    if payload.get("bilibili_uploaded"):
        succeeded.append("bilibili")
    if succeeded:
        return "、".join(succeeded)
    return _upload_target_label(payload.get("upload_target"))


def _pretty_error_text(value: Any) -> str:
    text = _truncate(_as_text(value), 300)
    return text or "未提供错误详情"


def _markdown_lines(*lines: str) -> str:
    return "\n".join(line for line in lines if _as_text(line))


def build_notification_message(event: NotificationEvent) -> NotificationMessage:
    payload = event.as_payload()
    event_type = event.event_type

    if event_type == EVENT_TASK_ADDED:
        title = "Y2A-Auto 任务已添加"
        summary = f"{payload.get('task_id', '')[:8]} | {_upload_target_label(payload.get('upload_target'))}"
        markdown = _markdown_lines(
            f"**任务已添加**",
            f"> 任务ID：`{_as_text(payload.get('task_id'))}`",
            f"> 投稿目标：{_upload_target_label(payload.get('upload_target'))}",
            f"> YouTube URL：{_truncate(_as_text(payload.get('youtube_url')), 500)}",
            f"> 时间：{_as_text(payload.get('occurred_at'))}",
        )
        return NotificationMessage(title=title, summary=summary, markdown=markdown)

    if event_type == EVENT_TASK_COMPLETED:
        task_title = _task_title(payload)
        title = "Y2A-Auto 任务已完成"
        summary = f"{task_title} | {_task_platform_result(payload)}"
        markdown = _markdown_lines(
            f"**任务已完成**",
            f"> 标题：{_truncate(task_title, 120)}",
            f"> 任务ID：`{_as_text(payload.get('task_id'))}`",
            f"> 投稿结果：{_task_platform_result(payload)}",
            f"> 投稿目标：{_upload_target_label(payload.get('upload_target'))}",
            f"> 时间：{_as_text(payload.get('occurred_at'))}",
        )
        return NotificationMessage(title=title, summary=summary, markdown=markdown)

    if event_type == EVENT_TASK_FAILED:
        task_title = _task_title(payload)
        error_text = _pretty_error_text(payload.get("error_message"))
        title = "Y2A-Auto 任务报错"
        summary = f"{task_title} | {error_text}"
        markdown = _markdown_lines(
            f"**任务报错**",
            f"> 标题：{_truncate(task_title, 120)}",
            f"> 任务ID：`{_as_text(payload.get('task_id'))}`",
            f"> 当前状态：{_as_text(payload.get('status')) or 'failed'}",
            f"> 投稿目标：{_upload_target_label(payload.get('upload_target'))}",
            f"> 错误摘要：{error_text}",
            f"> 时间：{_as_text(payload.get('occurred_at'))}",
        )
        return NotificationMessage(title=title, summary=summary, markdown=markdown)

    if event_type == EVENT_LOGIN_SUCCESS:
        title = "Y2A-Auto 后台登录成功"
        summary = f"{_as_text(payload.get('ip_address'))} | {_as_text(payload.get('occurred_at'))}"
        markdown = _markdown_lines(
            f"**后台登录成功**",
            f"> 来源IP：{_as_text(payload.get('ip_address')) or 'unknown'}",
            f"> 时间：{_as_text(payload.get('occurred_at'))}",
        )
        return NotificationMessage(title=title, summary=summary, markdown=markdown)

    if event_type == EVENT_LOGIN_LOCKED:
        title = "Y2A-Auto 登录已被锁定"
        summary = (
            f"{_as_text(payload.get('failed_attempts'))}/{_as_text(payload.get('max_attempts'))}"
            f" | {_as_text(payload.get('lock_minutes'))} 分钟"
        )
        markdown = _markdown_lines(
            f"**登录已被锁定**",
            f"> 来源IP：{_as_text(payload.get('ip_address')) or 'unknown'}",
            f"> 失败次数：{_as_text(payload.get('failed_attempts'))}/{_as_text(payload.get('max_attempts'))}",
            f"> 锁定时长：{_as_text(payload.get('lock_minutes'))} 分钟",
            f"> 时间：{_as_text(payload.get('occurred_at'))}",
        )
        return NotificationMessage(title=title, summary=summary, markdown=markdown)

    if event_type in (EVENT_QR_LOGIN_SUCCESS, EVENT_QR_LOGIN_FAILED):
        platform = _as_text(payload.get("platform")) or "平台"
        is_success = event_type == EVENT_QR_LOGIN_SUCCESS
        title = f"Y2A-Auto {platform}扫码登录{'成功' if is_success else '失败'}"
        message = _truncate(_as_text(payload.get("message")) or ("Cookies 已保存" if is_success else "登录失败"), 300)
        summary = f"{platform} | {message}"
        markdown = _markdown_lines(
            f"**{platform}扫码登录{'成功' if is_success else '失败'}**",
            f"> 平台：{platform}",
            f"> 结果：{message}",
            f"> 时间：{_as_text(payload.get('occurred_at'))}",
        )
        return NotificationMessage(title=title, summary=summary, markdown=markdown)

    title = "Y2A-Auto 系统通知"
    serialized = json.dumps(payload, ensure_ascii=False)
    summary = _truncate(serialized, 180)
    markdown = _markdown_lines(
        f"**系统通知**",
        f"> 事件：{event_type}",
        f"> 内容：{_truncate(serialized, 1500)}",
    )
    return NotificationMessage(title=title, summary=summary, markdown=markdown)
