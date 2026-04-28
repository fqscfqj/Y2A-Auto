from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from ..utils import get_app_subdir
from .adapters import (
    ALL_CHANNELS,
    CHANNEL_LABELS,
    NotificationSendError,
    build_notifier_registry,
    iter_enabled_channel_ids,
)
from .models import EVENT_CONFIG_KEY_MAP, NotificationEvent, NotificationMessage
from .renderers import build_notification_message


logger = logging.getLogger("notifications")

DB_CONNECT_TIMEOUT_SECONDS = 10
DB_BUSY_TIMEOUT_MS = 30000
DEFAULT_RETRY_SCHEDULE_SECONDS = (30, 120, 600, 1800, 3600)

OUTBOX_STATUS_PENDING = "pending"
OUTBOX_STATUS_SENDING = "sending"
OUTBOX_STATUS_SENT = "sent"
OUTBOX_STATUS_FAILED = "failed"

_global_notification_service = None
_global_notification_lock = threading.Lock()


def _current_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in ("true", "1", "on", "yes")


def _notification_db_path() -> str:
    db_dir = get_app_subdir("db")
    os.makedirs(db_dir, exist_ok=True)
    return os.path.join(db_dir, "tasks.db")


def is_notification_enabled(config: dict[str, Any] | None) -> bool:
    normalized = dict(config or {})
    return _as_bool(normalized.get("NOTIFY_ENABLED", False))


def is_notification_event_enabled(config: dict[str, Any] | None, event_type: str) -> bool:
    normalized = dict(config or {})
    if not is_notification_enabled(normalized):
        return False
    config_key = EVENT_CONFIG_KEY_MAP.get(event_type)
    if not config_key:
        return False
    return _as_bool(normalized.get(config_key, False))


class NotificationService:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        db_path: str | None = None,
        retry_schedule_seconds: tuple[int, ...] = DEFAULT_RETRY_SCHEDULE_SECONDS,
        start_worker: bool = True,
    ) -> None:
        self.config = dict(config or {})
        self.db_path = db_path or _notification_db_path()
        self.retry_schedule_seconds = tuple(retry_schedule_seconds or DEFAULT_RETRY_SCHEDULE_SECONDS)
        self.max_attempts = len(self.retry_schedule_seconds)
        self._registry = build_notifier_registry()
        self._db_lock = threading.Lock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._worker_thread = None

        self.init_db()
        if start_worker:
            self._start_worker()

    def init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TIMESTAMP,
                    last_error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notification_outbox_status_retry "
                "ON notification_outbox(status, next_retry_at, created_at)"
            )
            conn.execute(
                "UPDATE notification_outbox SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE status = ?",
                (OUTBOX_STATUS_PENDING, OUTBOX_STATUS_SENDING),
            )
            conn.commit()

    def refresh_config(self, config: dict[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        self._wake_event.set()

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=DB_CONNECT_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        except Exception:
            pass
        return conn

    def emit(self, event: NotificationEvent) -> int:
        config = dict(self.config or {})
        if not is_notification_event_enabled(config, event.event_type):
            return 0

        now_text = _current_ts()
        payload_json = json.dumps(event.as_payload(), ensure_ascii=False)
        created = 0

        with self._get_connection() as conn:
            for channel_id in iter_enabled_channel_ids(config):
                notifier = self._registry.get(channel_id)
                if not notifier:
                    continue
                missing_fields = notifier.validate_config(config)
                if missing_fields:
                    logger.warning("通知渠道 %s 已启用但配置不完整，跳过事件 %s", channel_id, event.event_type)
                    continue
                conn.execute(
                    """
                    INSERT INTO notification_outbox (
                        id, channel, event_type, payload_json, status, attempts,
                        next_retry_at, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        channel_id,
                        event.event_type,
                        payload_json,
                        OUTBOX_STATUS_PENDING,
                        0,
                        now_text,
                        None,
                        now_text,
                        now_text,
                    ),
                )
                created += 1
            conn.commit()

        if created:
            self._wake_event.set()
        return created

    def send_test_message(self, channel_id: str) -> None:
        config = dict(self.config or {})
        notifier = self._registry.get(channel_id)
        if not notifier:
            raise ValueError(f"不支持的通知渠道: {channel_id}")

        missing_fields = notifier.validate_config(config)
        if missing_fields:
            readable = "、".join(missing_fields)
            raise ValueError(f"{CHANNEL_LABELS.get(channel_id, channel_id)} 配置不完整：缺少 {readable}")

        now_text = _current_ts()
        channel_label = CHANNEL_LABELS.get(channel_id, channel_id)
        message = NotificationMessage(
            title=f"Y2A-Auto 🔔 测试消息",
            summary=f"{channel_label} 测试发送成功触发",
            markdown="\n".join(
                (
                    "**🔔 Y2A-Auto 测试消息**",
                    "",
                    f"> **推送渠道：**{channel_label}",
                    f"> **发送时间：**{now_text}",
                    f"> **说明：**如果你收到这条消息，说明当前渠道配置可用。",
                )
            ),
        )
        notifier.send(message, config)

    def shutdown(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)

    def _start_worker(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="notification-outbox-worker",
            daemon=True,
        )
        self._worker_thread.start()
        self._wake_event.set()

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            processed = self._process_due_messages()
            if processed > 0:
                continue

            wait_seconds = self._seconds_until_next_due()
            self._wake_event.wait(timeout=wait_seconds)
            self._wake_event.clear()

    def _process_due_messages(self) -> int:
        processed = 0
        while not self._stop_event.is_set():
            row = self._claim_next_due_row()
            if row is None:
                break
            processed += 1
            self._deliver_row(row)
        return processed

    def _seconds_until_next_due(self) -> float:
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT next_retry_at
                FROM notification_outbox
                WHERE status = ?
                ORDER BY COALESCE(next_retry_at, created_at) ASC
                LIMIT 1
                """,
                (OUTBOX_STATUS_PENDING,),
            ).fetchone()
        if not row or not row["next_retry_at"]:
            return 30.0
        try:
            next_due = datetime.strptime(str(row["next_retry_at"]), "%Y-%m-%d %H:%M:%S")
            delay = (next_due - datetime.now()).total_seconds()
        except Exception:
            delay = 0.0
        return max(1.0, min(30.0, delay))

    def _claim_next_due_row(self):
        now_text = _current_ts()
        with self._db_lock:
            with self._get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT *
                    FROM notification_outbox
                    WHERE status = ?
                      AND (next_retry_at IS NULL OR next_retry_at <= ?)
                    ORDER BY COALESCE(next_retry_at, created_at) ASC, created_at ASC
                    LIMIT 1
                    """,
                    (OUTBOX_STATUS_PENDING, now_text),
                ).fetchone()
                if row is None:
                    return None

                updated_at = _current_ts()
                cursor = conn.execute(
                    """
                    UPDATE notification_outbox
                    SET status = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (OUTBOX_STATUS_SENDING, updated_at, row["id"], OUTBOX_STATUS_PENDING),
                )
                conn.commit()
                if cursor.rowcount != 1:
                    return None
                return dict(row)

    def _deliver_row(self, row: dict[str, Any]) -> None:
        channel_id = str(row.get("channel") or "").strip()
        event_type = str(row.get("event_type") or "").strip()
        notifier = self._registry.get(channel_id)
        if not notifier:
            self._mark_failed(row["id"], int(row.get("attempts") or 0) + 1, f"未知通知渠道: {channel_id}")
            return

        try:
            payload = json.loads(str(row.get("payload_json") or "{}"))
        except Exception as exc:
            self._mark_failed(row["id"], int(row.get("attempts") or 0) + 1, f"通知载荷解析失败: {exc}")
            return

        event = NotificationEvent(event_type=event_type, payload=payload, occurred_at=str(payload.get("occurred_at") or _current_ts()))
        message = build_notification_message(event)
        next_attempt = int(row.get("attempts") or 0) + 1

        try:
            notifier.send(message, dict(self.config or {}))
            self._mark_sent(row["id"], next_attempt)
        except Exception as exc:
            error_text = str(exc)
            if next_attempt >= self.max_attempts:
                self._mark_failed(row["id"], next_attempt, error_text)
            else:
                retry_seconds = self.retry_schedule_seconds[next_attempt - 1]
                self._mark_retry(row["id"], next_attempt, retry_seconds, error_text)

    def _mark_sent(self, row_id: str, attempts: int) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE notification_outbox
                SET status = ?, attempts = ?, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (OUTBOX_STATUS_SENT, attempts, _current_ts(), row_id),
            )
            conn.commit()

    def _mark_retry(self, row_id: str, attempts: int, retry_seconds: int, last_error: str) -> None:
        next_retry_at = datetime.fromtimestamp(time.time() + retry_seconds).strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE notification_outbox
                SET status = ?, attempts = ?, next_retry_at = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (OUTBOX_STATUS_PENDING, attempts, next_retry_at, last_error, _current_ts(), row_id),
            )
            conn.commit()
        self._wake_event.set()

    def _mark_failed(self, row_id: str, attempts: int, last_error: str) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE notification_outbox
                SET status = ?, attempts = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (OUTBOX_STATUS_FAILED, attempts, last_error, _current_ts(), row_id),
            )
            conn.commit()


def get_global_notification_service(config: dict[str, Any] | None = None) -> NotificationService:
    global _global_notification_service
    with _global_notification_lock:
        if _global_notification_service is None:
            if config is None:
                from ..config_manager import load_config
                config = load_config()
            _global_notification_service = NotificationService(config=config)
        elif config is not None:
            _global_notification_service.refresh_config(config)
        return _global_notification_service


def shutdown_global_notification_service() -> None:
    global _global_notification_service
    with _global_notification_lock:
        if _global_notification_service is not None:
            _global_notification_service.shutdown()
            _global_notification_service = None


def emit_notification_event(event: NotificationEvent) -> int:
    try:
        service = get_global_notification_service()
        return service.emit(event)
    except Exception as exc:
        logger.warning("写入通知队列失败: %s", exc)
        return 0
