from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from .models import NotificationMessage


CHANNEL_WECOM = "wecom"
CHANNEL_SERVERCHAN = "serverchan"
CHANNEL_MESSAGE_PUSHER = "message_pusher"

ALL_CHANNELS = (
    CHANNEL_WECOM,
    CHANNEL_SERVERCHAN,
    CHANNEL_MESSAGE_PUSHER,
)

CHANNEL_LABELS = {
    CHANNEL_WECOM: "企业微信",
    CHANNEL_SERVERCHAN: "Server酱",
    CHANNEL_MESSAGE_PUSHER: "message-pusher",
}

CHANNEL_ENABLE_KEY_MAP = {
    CHANNEL_WECOM: "NOTIFY_WECOM_ENABLED",
    CHANNEL_SERVERCHAN: "NOTIFY_SERVERCHAN_ENABLED",
    CHANNEL_MESSAGE_PUSHER: "NOTIFY_MESSAGE_PUSHER_ENABLED",
}

CHANNEL_REQUIRED_CONFIG_MAP = {
    CHANNEL_WECOM: ("NOTIFY_WECOM_WEBHOOK_URL",),
    CHANNEL_SERVERCHAN: ("NOTIFY_SERVERCHAN_SENDKEY",),
    CHANNEL_MESSAGE_PUSHER: (
        "NOTIFY_MESSAGE_PUSHER_SERVER",
        "NOTIFY_MESSAGE_PUSHER_USERNAME",
        "NOTIFY_MESSAGE_PUSHER_TOKEN",
    ),
}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in ("true", "1", "on", "yes")


def iter_enabled_channel_ids(config: dict[str, Any] | None) -> list[str]:
    normalized = dict(config or {})
    result = []
    for channel_id in ALL_CHANNELS:
        if _as_bool(normalized.get(CHANNEL_ENABLE_KEY_MAP[channel_id], False)):
            result.append(channel_id)
    return result


def validate_channel_config_fields(channel_id: str, config: dict[str, Any] | None) -> list[str]:
    normalized = dict(config or {})
    missing = []
    for key in CHANNEL_REQUIRED_CONFIG_MAP.get(channel_id, ()):
        if not str(normalized.get(key) or "").strip():
            missing.append(key)
    return missing


class NotificationSendError(RuntimeError):
    pass


@dataclass
class Notifier:
    channel_id: str
    label: str

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        return validate_channel_config_fields(self.channel_id, config)

    def send(self, message: NotificationMessage, config: dict[str, Any]) -> None:
        raise NotImplementedError

    @staticmethod
    def _raise_for_http_error(response) -> None:
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise NotificationSendError(str(exc)) from exc


class WeComNotifier(Notifier):
    def __init__(self) -> None:
        super().__init__(channel_id=CHANNEL_WECOM, label=CHANNEL_LABELS[CHANNEL_WECOM])

    def send(self, message: NotificationMessage, config: dict[str, Any]) -> None:
        webhook = str(config.get("NOTIFY_WECOM_WEBHOOK_URL") or "").strip()
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": message.markdown,
            },
        }
        try:
            response = requests.post(webhook, json=payload, timeout=10)
            self._raise_for_http_error(response)
            data = response.json()
            if int(data.get("errcode", -1)) == 0:
                return
            raise NotificationSendError(str(data.get("errmsg") or "企业微信 Markdown 推送失败"))
        except NotificationSendError:
            raise
        except Exception as original_exc:
            fallback_text = f"{message.title}\n{message.summary}"
            fallback = {
                "msgtype": "text",
                "text": {
                    "content": fallback_text,
                },
            }
            try:
                response = requests.post(webhook, json=fallback, timeout=10)
                self._raise_for_http_error(response)
                data = response.json()
                if int(data.get("errcode", -1)) != 0:
                    raise NotificationSendError(str(data.get("errmsg") or "企业微信推送失败")) from original_exc
            except NotificationSendError:
                raise
            except Exception as fallback_exc:
                raise NotificationSendError(f"企业微信推送失败(含回退): {fallback_exc}") from original_exc


class ServerChanNotifier(Notifier):
    def __init__(self) -> None:
        super().__init__(channel_id=CHANNEL_SERVERCHAN, label=CHANNEL_LABELS[CHANNEL_SERVERCHAN])

    def send(self, message: NotificationMessage, config: dict[str, Any]) -> None:
        sendkey = str(config.get("NOTIFY_SERVERCHAN_SENDKEY") or "").strip()
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
        response = requests.post(
            url,
            data={
                "title": message.title,
                "desp": message.markdown,
            },
            timeout=10,
        )
        self._raise_for_http_error(response)
        data = response.json()
        if int(data.get("code", -1)) != 0:
            raise NotificationSendError(str(data.get("message") or "Server酱推送失败"))


class MessagePusherNotifier(Notifier):
    def __init__(self) -> None:
        super().__init__(channel_id=CHANNEL_MESSAGE_PUSHER, label=CHANNEL_LABELS[CHANNEL_MESSAGE_PUSHER])

    def send(self, message: NotificationMessage, config: dict[str, Any]) -> None:
        server = str(config.get("NOTIFY_MESSAGE_PUSHER_SERVER") or "").strip().rstrip("/")
        username = str(config.get("NOTIFY_MESSAGE_PUSHER_USERNAME") or "").strip()
        token = str(config.get("NOTIFY_MESSAGE_PUSHER_TOKEN") or "").strip()
        channel = str(config.get("NOTIFY_MESSAGE_PUSHER_CHANNEL") or "").strip()
        payload = {
            "title": message.title,
            "description": message.summary,
            "content": message.markdown,
            "token": token,
        }
        if channel:
            payload["channel"] = channel
        response = requests.post(
            f"{server}/push/{username}",
            json=payload,
            timeout=10,
        )
        self._raise_for_http_error(response)
        data = response.json()
        if not bool(data.get("success")):
            raise NotificationSendError(str(data.get("message") or "message-pusher 推送失败"))


def build_notifier_registry() -> dict[str, Notifier]:
    return {
        CHANNEL_WECOM: WeComNotifier(),
        CHANNEL_SERVERCHAN: ServerChanNotifier(),
        CHANNEL_MESSAGE_PUSHER: MessagePusherNotifier(),
    }
