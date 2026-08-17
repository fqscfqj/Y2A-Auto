"""
AI 客户端兜底层：当主 OpenAI 兼容端点不可用时，自动切换到用户配置的备用端点。

设计要点（与具体 provider 无关，仅依赖 OpenAI 兼容协议）：
- 主端点来自传入配置中的 OPENAI_*。
- 备用端点来自配置中的 FALLBACK_OPENAI_*（可选；未配置则退化为单端点，行为与原先一致）。
- 仅当主端点出现「连接错误 / 超时 / 5xx」这类“可用性”错误时才切换兜底；
  4xx（请求本身的问题，例如 JSON 模式不被某些网关支持）不切换，交由上层既有逻辑处理。
- 调用方式与 openai.OpenAI 完全兼容：client.chat.completions.create(...)。
- 每个端点的 model 以自身配置为准（主端点用 OPENAI_MODEL_NAME，兜底端点用
  FALLBACK_OPENAI_MODEL_NAME）；兜底端点的 base_url / model 若留空则**继承主端点**，
  与设置页「留空则沿用主端点」语义一致，而不是回退到硬编码的官方默认值。
- 仅在多端点（故障转移）模式下关闭 SDK 内部自动重试并使用 failover 连接短超时；
  单端点（无兜底）保留 SDK 默认重试（2 次）与用户配置的超时，行为与原先一致，
  不丢失瞬时 5xx / 连接失败的恢复能力。
"""
import logging
import re

import httpx
from openai import OpenAI, APIConnectionError, APITimeoutError, APIStatusError

logger = logging.getLogger(__name__)

# 兜底端点字段；统一客户端需要在所有调用路径上可靠拿到它们。
FALLBACK_KEYS = (
    "FALLBACK_OPENAI_API_KEY",
    "FALLBACK_OPENAI_BASE_URL",
    "FALLBACK_OPENAI_MODEL_NAME",
)

# 连接阶段（failover 探测）默认超时秒数；可通过全局配置
# AI_FAILOVER_TIMEOUT_SECONDS 覆盖。仅用于快速判断主端点是否可达，
# 不影响正常响应的读取超时。
_FAILOVER_CONNECT_TIMEOUT_DEFAULT = 8.0

# 无结构化 HTTP 状态码时，仅匹配「带明确语境」的 5xx 文本，避免把
# "maximum output is 500 tokens" 这类 4xx 误判为端点宕机。
_5XX_TEXT_RE = re.compile(
    r"(?:error code|status|http/?|response|code)\D{0,15}5\d{2}"
    r"|\b5\d{2}\s*(?:bad gateway|service unavailable|gateway timeout|internal server error)\b",
    re.IGNORECASE,
)
# 连接类失败信号（无状态码时的谨慎文本匹配，不含泛化的 "500"/"502" 子串）。
_CONNECTION_TEXT_SIGNALS = (
    "name or service not known",
    "failed to connect",
    "connection refused",
    "connection reset",
    "connection aborted",
    "connecttimeout",
    "errno 61",   # macOS 连接被拒
    "errno 111",  # Linux 连接被拒
)


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


def _load_global_config():
    """读取全局配置；导入或读取失败时（如依赖缺失）返回空字典，降级为单端点。

    集中到单一函数，便于在无 config_manager 依赖的测试环境中被稳妥替换。
    """
    try:
        from modules.config_manager import load_config
        return load_config() or {}
    except Exception:
        return {}


def _get_failover_connect_timeout():
    """读取全局配置中的 failover 连接超时（秒），默认 8s。

    仅在 [1, 60] 区间有效；越界或非法（手工配置 / 直接 POST 负值）一律回退默认 8s，
    避免 httpx 在创建客户端 / 请求时抛 timeout range error。
    """
    try:
        v = _load_global_config().get("AI_FAILOVER_TIMEOUT_SECONDS")
        if v is not None and str(v).strip() != "":
            fv = float(str(v).strip())
            if 1.0 <= fv <= 60.0:
                return fv
    except Exception:
        pass
    return _FAILOVER_CONNECT_TIMEOUT_DEFAULT


def _make_raw_client(ep, multi_endpoint=False):
    """构造单个端点的 OpenAI 客户端。

    读取 / 写入超时严格保留用户配置（OPENAI_TIMEOUT_SECONDS，默认 600s），
    不截断——思考模型 / 长输出 / 字幕批量翻译等正常请求不应被 20s 误杀；
    原先「无兜底配置时 20s 必失败」的回归在此消除。

    multi_endpoint=True（故障转移模式）时：
    - 仅连接阶段使用可配置的短超时（AI_FAILOVER_TIMEOUT_SECONDS，默认 8s）
      做 failover 快速探测：主端点连不上时快速失败并切到下一个端点，
      但响应较慢（只是读得久）不会被当作宕机重复请求；
    - 关闭 SDK 内部自动重试：失败立即交由兜底层切换到下一个端点。
    multi_endpoint=False（单端点，无兜底）时：
    - 保留 SDK 默认重试（默认 2 次）与用户配置的超时（连接/读取/写入一致），
      不强制 8s 连接超时，避免把“响应较慢”误当成宕机并丢失瞬时 5xx/连接恢复能力
      —— 即“未配置兜底时行为完全不变”。
    """
    opts = {}
    if ep.get("base_url"):
        opts["base_url"] = ep["base_url"]
    to = float(ep.get("timeout") or 600)
    if multi_endpoint:
        connect_to = float(ep.get("connect_timeout") or _get_failover_connect_timeout())
        # 显式 httpx.Timeout：连接短、读取/写入保留用户配置。
        opts["timeout"] = httpx.Timeout(connect=connect_to, read=to, write=to, pool=connect_to)
        # 关闭 SDK 内部自动重试：失败立即交由兜底层切换到下一个端点。
        opts["max_retries"] = 0
    else:
        # 单端点：连接/读取/写入一致使用用户配置超时，保留 SDK 默认重试。
        opts["timeout"] = httpx.Timeout(connect=to, read=to, write=to, pool=to)
    return OpenAI(api_key=ep["api_key"], **opts)


def _extract_status_code(exc):
    """从异常中提取 HTTP 状态码；无结构化状态码时返回 None。"""
    status = getattr(exc, "status_code", None)
    if status is None:
        # 个别 provider 把状态码放在消息里，如 "Error code: 502"
        m = re.search(r"error code:\s*(\d{3})", str(exc) or "", re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
    return status


def _is_unavailable_error(exc):
    """判断是否为「端点不可用」类错误（应触发兜底切换）。

    规则：
    - 连接错误 / 超时错误 → 端点不可用（True）。
    - 带明确 HTTP 状态码的 APIStatusError：
        * 5xx → 端点不可用（True）。
        * 4xx 及更低 → 请求本身的问题，不切换（False）。
    - 仅对「无结构化状态码」的异常做谨慎文本匹配：只匹配带明确语境的
      5xx 或连接失败信号，避免把 "maximum output is 500 tokens" 这类
      4xx（消息中含 "500" 子串）误判为端点宕机而错误地切换 / 重发。
    """
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    status = _extract_status_code(exc)
    if status is not None:
        # 有结构化状态码：严格按状态码判定，4xx 一律不切换（修复前会把含 "500" 的 4xx 误判）。
        return int(status) >= 500
    text = (str(exc) or "").lower()
    for sig in _CONNECTION_TEXT_SIGNALS:
        if sig in text:
            return True
    if _5XX_TEXT_RE.search(text):
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
        # 仅故障转移（多端点）模式下关闭 SDK 重试、使用 failover 连接短超时
        self._raw = [_make_raw_client(ep, multi_endpoint=True) for ep in endpoints]
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


def _global_fallback_fields():
    """从全局配置读取已配置的兜底端点字段（FALLBACK_OPENAI_*）。"""
    cfg = _load_global_config()
    return {k: cfg[k] for k in FALLBACK_KEYS if cfg.get(k)}


def _resolve_fallback_fields(openai_config):
    """补齐 FALLBACK_OPENAI_* 字段，使统一客户端在所有调用路径上都能可靠拿到兜底配置。

    优先使用 openai_config 中已携带的兜底字段；缺失项回退到全局配置
    （config_manager.load_config）。这样无论调用方（task_manager 的元数据翻译 /
    标签生成 / 分区推荐、subtitle_translator 字幕翻译、subtitle_qc 字幕质检）
    是否显式透传兜底配置，只要用户在全局配置里填了兜底端点，客户端都会拿到它，
    消除「用户已配置兜底却仍是单端点」的回归。
    """
    if not openai_config:
        return openai_config
    # 已完整携带兜底字段则无需回退（也避免每次都读全局配置）。
    if all(openai_config.get(k) for k in FALLBACK_KEYS):
        return openai_config
    merged = dict(openai_config)
    for k, v in _global_fallback_fields().items():
        if not merged.get(k):
            merged[k] = v
    return merged


def get_ai_client(openai_config):
    """
    返回 AI 客户端。

    - 若解析后存在 FALLBACK_OPENAI_API_KEY（可来自传入配置或全局配置），
      则返回带兜底能力的 FallbackChatClient；
    - 否则返回与原来一致的裸 OpenAI 客户端（行为完全不变）。
    - 仅当主端点出现「连接 / 超时 / 5xx」时才切换兜底；4xx 类请求错误不切换。

    Args:
        openai_config: 配置字典，需包含 OPENAI_*，可选包含 FALLBACK_OPENAI_*。
            缺失的兜底字段会自动从全局配置补齐。
    """
    openai_config = _resolve_fallback_fields(openai_config)
    primary = _build_endpoint("OPENAI_", openai_config)
    endpoints = [primary] if primary else []

    # 兜底端点（可选）。设置页声明“兜底 URL / 模型留空则沿用主端点”，
    # 因此空字段继承主端点的 base_url / model，而不是回退到硬编码的官方默认值。
    fb_default_base = (primary or {}).get("base_url") or "https://api.openai.com/v1"
    fb_default_model = (primary or {}).get("model") or "gpt-3.5-turbo"
    fb = _build_endpoint(
        "FALLBACK_OPENAI_", openai_config,
        default_base=fb_default_base, default_model=fb_default_model,
    )
    if fb:
        endpoints.append(fb)

    if not endpoints:
        raise RuntimeError("未配置任何可用 AI 端点（OPENAI_API_KEY 为空）")
    if len(endpoints) == 1:
        # 单端点（无兜底）：保留 SDK 默认重试与用户配置超时，行为与原先一致
        return _make_raw_client(endpoints[0], multi_endpoint=False)
    return FallbackChatClient(endpoints)
