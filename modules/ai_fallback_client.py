"""
AI 客户端兜底层：当主 OpenAI 兼容端点不可用时，自动切换到用户配置的备用端点。

设计要点（与具体 provider 无关，仅依赖 OpenAI 兼容协议）：
- 主端点来自传入配置中的 OPENAI_*。
- 备用端点来自配置中的 FALLBACK_OPENAI_*（可选；未配置则退化为单端点，行为与原先一致）。
- 仅当主端点出现「连接错误 / 超时 / 5xx」这类“可用性”错误时才切换兜底；
  4xx（请求本身的问题，例如 JSON 模式不被某些网关支持）不切换，交由上层既有逻辑处理。
- 调用方式与 openai.OpenAI 完全兼容：client.chat.completions.create(...)。
- 每个端点的 model 以自身配置为准（主端点用 OPENAI_MODEL_NAME，兜底端点用
  FALLBACK_OPENAI_MODEL_NAME），调用方传入的 model 参数会被端点自身配置覆盖，
  以保证兜底端点使用正确的模型名。
- 关闭 SDK 内部自动重试：失败立即交由兜底层切换到下一个端点，避免单个端点
  长时间挂起把整条 AI 链路（翻译 / 标签 / 字幕）拖死。
"""
import logging

import httpx
from openai import OpenAI, APIConnectionError, APITimeoutError, APIStatusError

logger = logging.getLogger(__name__)


def _build_endpoint(prefix, cfg, default_model="gpt-3.5-turbo",
                    default_base="https://api.openai.com/v1"):
    """从配置字典中按前缀读取一个端点配置。无 API key 时返回 None。"""
    api_key = (cfg.get(prefix + "API_KEY") or "").strip()
    if not api_key:
        return None
    base_url = (cfg.get(prefix + "BASE_URL") or default_base).strip()
    model = (cfg.get(prefix + "MODEL_NAME") or default_model).strip()
    timeout = cfg.get("OPENAI_TIMEOUT_SECONDS", 600)
    try:
        timeout = float(str(timeout).strip())
    except Exception:
        timeout = 600.0
    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "timeout": timeout,
        "label": f"{prefix.rstrip('_').lower()}:{base_url}",
    }


def _make_raw_client(ep):
    opts = {}
    if ep.get("base_url"):
        opts["base_url"] = ep["base_url"]
    to = ep.get("timeout") or 0
    if to and to > 0:
        # 显式 httpx.Timeout 对象，确保连接 / 读取超时真正生效：
        # 连接超时短（8s）、读取超时封顶 20s——主端点挂起时快速失败并切换兜底，
        # 避免单个端点长时间挂起把整条 AI 链路拖死。
        read = min(float(to), 20.0)
        opts["timeout"] = httpx.Timeout(connect=8.0, read=read, write=read, pool=8.0)
    # 关闭 SDK 内部自动重试：失败立即交由兜底层切换到下一个端点
    opts["max_retries"] = 0
    return OpenAI(api_key=ep["api_key"], **opts)


def _is_unavailable_error(exc):
    """判断是否为「端点不可用」类错误（应触发兜底切换）。"""
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", 0) or 0
        if status >= 500:
            return True
    text = (str(exc) or "").lower()
    for sig in ("connection", "timed out", "timeout",
                "name or service not known", "failed to connect",
                "503", "502", "500", "504", "bad gateway", "service unavailable"):
        if sig in text:
            return True
    return False


class _CompletionsProxy:
    def __init__(self, parent):
        self._parent = parent

    def create(self, **kwargs):
        return self._parent._create(kwargs)


class _ChatProxy:
    def __init__(self, parent):
        self.completions = _CompletionsProxy(parent)


class FallbackChatClient:
    """按顺序尝试多个 OpenAI 兼容端点；前一个“不可用”时自动切到下一个。"""

    def __init__(self, endpoints):
        self._endpoints = endpoints
        self._raw = [_make_raw_client(ep) for ep in endpoints]
        # 兼容 client.chat.completions.create(...) 调用链
        self.chat = _ChatProxy(self)

    def _create(self, kwargs):
        requested_model = kwargs.pop("model", None)
        last_exc = None
        for idx, ep in enumerate(self._endpoints):
            call_kwargs = dict(kwargs)
            # 以端点自身配置的 model 为准
            call_kwargs["model"] = ep["model"]
            # 合并端点级 extra_body 与调用方 extra_body（调用方优先）
            merged_extra = dict(ep.get("extra_body") or {})
            caller_extra = call_kwargs.pop("extra_body", None)
            if isinstance(caller_extra, dict):
                merged_extra.update(caller_extra)
            # 部分推理模型（如 DeepSeek 的推理系列）默认把结果放在 reasoning_content、
            # content 为空；翻译 / 质检等场景需要 content 有值，故对这类端点关闭思考。
            if "deepseek" in (ep.get("base_url") or "").lower():
                merged_extra.pop("thinking", None)
                merged_extra["enable_thinking"] = False
            if merged_extra:
                call_kwargs["extra_body"] = merged_extra
            try:
                return self._raw[idx].chat.completions.create(**call_kwargs)
            except Exception as exc:  # noqa: BLE001
                if _is_unavailable_error(exc):
                    logger.warning(
                        f"[AI兜底] 端点 {ep['label']} 不可用: {str(exc)[:160]}，尝试下一个端点"
                    )
                    last_exc = exc
                    continue
                logger.error(
                    f"[AI兜底] 端点 {ep['label']} 返回非可用性错误，不再切换: {str(exc)[:160]}"
                )
                raise
        logger.error("[AI兜底] 所有 AI 端点均不可用")
        raise last_exc if last_exc else RuntimeError("all AI endpoints failed")


def get_ai_client(openai_config):
    """
    返回 AI 客户端。

    - 若配置了 FALLBACK_OPENAI_API_KEY，则返回带兜底能力的 FallbackChatClient；
    - 否则返回与原来一致的裸 OpenAI 客户端（行为完全不变）。
    - 仅当主端点出现「连接 / 超时 / 5xx」时才切换兜底；4xx 类请求错误不切换。

    Args:
        openai_config: 配置字典，需包含 OPENAI_*，可选包含 FALLBACK_OPENAI_*。
    """
    primary = _build_endpoint("OPENAI_", openai_config)
    endpoints = [primary] if primary else []

    # 兜底端点（可选）
    fb = _build_endpoint("FALLBACK_OPENAI_", openai_config)
    if fb:
        endpoints.append(fb)

    if not endpoints:
        raise RuntimeError("未配置任何可用 AI 端点（OPENAI_API_KEY 为空）")
    if len(endpoints) == 1:
        return _make_raw_client(endpoints[0])
    return FallbackChatClient(endpoints)
