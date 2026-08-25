"""
AI 客户端协议与兜底层：兼容 Chat Completions / Responses API，并在主端点
不可用时自动切换到用户配置的备用端点。

设计要点（与具体 provider 无关，仅依赖 OpenAI 兼容协议）：
- 主端点来自传入配置中的 OPENAI_*。
- 备用端点来自配置中的 FALLBACK_OPENAI_*（可选；未配置则退化为单端点，行为与原先一致）。
- 仅当主端点出现「连接错误 / 超时 / 5xx」这类“可用性”错误时才切换兜底；
  4xx（请求本身的问题，例如 JSON 模式不被某些网关支持）不切换，交由上层既有逻辑处理。
- 上层继续使用 client.chat.completions.create(...)；当配置地址以 /responses 结尾时，
  本层自动转换请求并调用 client.responses.create(...)，再把输出归一化为 chat 结构。
- 每个端点的 model 以自身配置为准（主端点用 OPENAI_MODEL_NAME，兜底端点用
  FALLBACK_OPENAI_MODEL_NAME）；兜底端点的 base_url / model 若留空则**继承主端点**，
  与设置页「留空则沿用主端点」语义一致，而不是回退到硬编码的官方默认值。
- 仅在多端点（故障转移）模式下关闭 SDK 内部自动重试并使用 failover 连接短超时；
  单端点（无兜底）保留 SDK 默认重试（2 次）与用户配置的超时，行为与原先一致，
  不丢失瞬时 5xx / 连接失败的恢复能力。
"""
import logging
import re
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlparse, urlunparse

import httpx
from openai import OpenAI, APIConnectionError, APITimeoutError, APIStatusError

from modules.utils import normalize_openai_base_url

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


def _api_mode_from_url(base_url):
    """完整 /responses 地址选择 Responses API；根地址保持原 Chat 默认。"""
    value = str(base_url or '').strip()
    if not value:
        return 'chat_completions'
    try:
        path = urlparse(value).path.rstrip('/').lower()
    except Exception:
        path = value.rstrip('/').lower()
    return 'responses' if path.endswith('/responses') else 'chat_completions'


def _base_url_and_default_query(base_url):
    """拆分完整端点查询参数，避免 SDK 把资源路径追加到查询字符串之后。"""
    value = str(base_url or '').strip()
    if not value:
        return '', {}
    parsed = urlparse(value)
    default_query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    normalized_value = urlunparse(parsed._replace(query='', fragment=''))
    return normalize_openai_base_url(normalized_value), default_query


def _build_endpoint(prefix, cfg, default_model="gpt-3.5-turbo",
                    default_base="https://api.openai.com/v1",
                    default_api_mode='chat_completions', default_query=None):
    """从配置字典中按前缀读取一个端点配置。无 API key 时返回 None。"""
    api_key = (cfg.get(prefix + "API_KEY") or "").strip()
    if not api_key:
        return None
    # 兼容设置 API 根地址或完整的 /chat/completions、/responses 地址。规范化集中在统一
    # 客户端入口，确保单端点、主端点和兜底端点采用完全一致的 URL 语义。
    configured_base = str(cfg.get(prefix + "BASE_URL") or '').strip()
    if configured_base:
        api_mode = _api_mode_from_url(configured_base)
        base_url, endpoint_query = _base_url_and_default_query(configured_base)
    else:
        api_mode = default_api_mode
        base_url = normalize_openai_base_url(default_base)
        endpoint_query = dict(default_query or {})
    model = str(cfg.get(prefix + "MODEL_NAME") or "").strip() or str(default_model).strip()
    timeout = cfg.get("OPENAI_TIMEOUT_SECONDS", 600)
    try:
        timeout = float(str(timeout).strip())
    except Exception:
        timeout = 600.0
    if timeout <= 0:
        # <=0 视为“未配置”：沿用 OpenAI SDK 默认超时（connect 5s，read/write 600s），
        # 而非全阶段 600s 或负数导致 httpx 抛 timeout range error（旧配置语义回归）。
        timeout = 0.0
    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "api_mode": api_mode,
        "default_query": endpoint_query,
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
    if ep.get("default_query"):
        opts["default_query"] = dict(ep["default_query"])
    raw_to = ep.get("timeout")
    try:
        to = float(raw_to) if raw_to is not None else 600.0
    except (TypeError, ValueError):
        to = 600.0
    if to <= 0:
        # <=0（含 _build_endpoint 规范化的 0.0 哨兵）：旧语义视为未配置 timeout，
        # 单端点不传 timeout，沿用 OpenAI SDK 默认（connect 5s，read/write 600s）；
        # 多端点仍用 failover 连接短超时快速探测，读取/写入保留 SDK 默认 600s。
        if multi_endpoint:
            connect_to = float(ep.get("connect_timeout") or _get_failover_connect_timeout())
            opts["timeout"] = httpx.Timeout(connect=connect_to, read=600.0, write=600.0, pool=connect_to)
            opts["max_retries"] = 0
    elif multi_endpoint:
        # 显式 httpx.Timeout：连接短、读取/写入保留用户配置。
        connect_to = float(ep.get("connect_timeout") or _get_failover_connect_timeout())
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


class _FallbackEndpointClient:
    """Expose one concrete failover endpoint to the request adaptation layer.

    ``modules.utils`` needs the endpoint identity before it builds a request
    (thinking controls and compatibility actions are endpoint-specific).  The
    public ``FallbackChatClient`` cannot provide that identity because the
    endpoint is selected only after the request starts.  This small proxy
    makes one endpoint look like the existing chat client while keeping the
    actual failover loop in ``FallbackChatClient``.
    """

    def __init__(self, raw_client, endpoint):
        self._raw = raw_client
        self._endpoint = endpoint
        self.base_url = getattr(raw_client, "base_url", None) or endpoint.get("base_url", "")
        self.api_mode = endpoint.get("api_mode", "chat_completions")
        self._endpoint_model = endpoint.get("model", "")
        self.chat = _ChatProxy(self)

    def _create(self, kwargs):
        call_kwargs = dict(kwargs)
        # Each endpoint owns its model; callers may pass the primary model.
        call_kwargs["model"] = self._endpoint["model"]

        # Merge endpoint-level extra_body with request-level values while
        # preserving the request's values on conflicts.
        merged_extra = dict(self._endpoint.get("extra_body") or {})
        caller_extra = call_kwargs.pop("extra_body", None)
        if isinstance(caller_extra, dict):
            merged_extra.update(caller_extra)

        # DeepSeek's native Chat Completions field differs from the generic
        # thinking object used by the utility layer.  This conversion must be
        # performed here, after the actual endpoint has been selected.
        if "deepseek" in (self._endpoint.get("base_url") or "").lower():
            thinking_cfg = merged_extra.get("thinking")
            if isinstance(thinking_cfg, dict) and thinking_cfg.get("enabled") is False:
                merged_extra.pop("thinking", None)
                merged_extra["enable_thinking"] = False
        if merged_extra:
            call_kwargs["extra_body"] = merged_extra

        return _create_on_endpoint(self._raw, self._endpoint, call_kwargs)


def _value(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _content_text(content):
    """把 Chat 消息内容转换成适合 Responses instructions 的纯文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or '')
    parts = []
    for part in content:
        part_type = _value(part, 'type', '')
        if part_type in {'text', 'input_text', 'output_text'}:
            text = _value(part, 'text', '')
            if isinstance(text, dict):
                text = text.get('value', '')
            parts.append(str(text or ''))
    return ''.join(parts)


def _responses_message_content(content):
    """将 Chat 多模态 content parts 转成 Responses 输入 content parts。"""
    if not isinstance(content, list):
        return content
    converted = []
    for part in content:
        if not isinstance(part, dict):
            converted.append(part)
            continue
        part_type = part.get('type')
        if part_type == 'text':
            converted.append({'type': 'input_text', 'text': part.get('text', '')})
        elif part_type == 'image_url':
            image = part.get('image_url')
            if isinstance(image, dict):
                converted_part = {
                    'type': 'input_image',
                    'image_url': image.get('url'),
                }
                if image.get('detail'):
                    converted_part['detail'] = image['detail']
            else:
                converted_part = {'type': 'input_image', 'image_url': image}
            converted.append(converted_part)
        else:
            converted.append(dict(part))
    return converted


def _responses_text_format(response_format):
    """Responses 的 json_schema 格式比 Chat 少一层 json_schema 包装。"""
    if not isinstance(response_format, dict):
        return response_format
    if response_format.get('type') != 'json_schema':
        return dict(response_format)
    schema_config = response_format.get('json_schema')
    if not isinstance(schema_config, dict):
        return dict(response_format)
    return {'type': 'json_schema', **schema_config}


def _responses_create_kwargs(chat_kwargs):
    """将项目使用的 Chat Completions 参数转换为 Responses API 参数。"""
    result = dict(chat_kwargs or {})
    # Chat Completions 调用默认不创建可检索的服务端状态，而 Responses API
    # 默认会保存响应。透明协议转换应保持原有数据留存语义；调用方显式传入
    # store 时仍尊重其选择。
    result.setdefault('store', False)
    messages = result.pop('messages', []) or []
    instructions = []
    response_input = []
    for message in messages:
        role = _value(message, 'role', 'user')
        content = _value(message, 'content', '')
        if role in {'system', 'developer'}:
            text = _content_text(content).strip()
            if text:
                instructions.append(text)
            continue
        response_input.append({
            'role': role,
            'content': _responses_message_content(content),
        })
    if instructions:
        result['instructions'] = '\n\n'.join(instructions)
    result['input'] = response_input

    token_limit = result.pop('max_completion_tokens', None)
    if token_limit is None:
        token_limit = result.pop('max_tokens', None)
    else:
        result.pop('max_tokens', None)
    if token_limit is not None:
        result['max_output_tokens'] = token_limit

    response_format = result.pop('response_format', None)
    if response_format is not None:
        text_config = result.get('text')
        if not isinstance(text_config, dict):
            text_config = {}
        else:
            text_config = dict(text_config)
        text_config['format'] = _responses_text_format(response_format)
        result['text'] = text_config
    return result


def _responses_output_text(response):
    """兼容 SDK 对象和普通字典，提取 Responses API 的所有文本输出。"""
    output_text = _value(response, 'output_text')
    if isinstance(output_text, str) and output_text:
        return output_text

    parts = []
    for item in _value(response, 'output', []) or []:
        if _value(item, 'type') != 'message':
            continue
        for content in _value(item, 'content', []) or []:
            content_type = _value(content, 'type')
            if content_type == 'output_text':
                parts.append(str(_value(content, 'text', '') or ''))
            elif content_type == 'refusal':
                parts.append(str(_value(content, 'refusal', '') or ''))
    return ''.join(parts)


def _as_chat_completion(response):
    """把 Responses API 结果适配为项目既有的 choices[0].message 结构。"""
    message = SimpleNamespace(role='assistant', content=_responses_output_text(response))
    return SimpleNamespace(
        id=_value(response, 'id'),
        model=_value(response, 'model'),
        usage=_value(response, 'usage'),
        choices=[SimpleNamespace(index=0, message=message, finish_reason=None)],
        raw_response=response,
    )


class ResponsesResultError(RuntimeError):
    """Responses API 在成功 HTTP 响应体中报告的生成失败。"""

    def __init__(self, response):
        self.response_result = response
        self.response_status = str(_value(response, 'status', '') or '').strip().lower()
        error = _value(response, 'error')
        self.code = str(_value(error, 'code', '') or '').strip().lower()
        message = str(_value(error, 'message', '') or '').strip()
        self.status_code = self._status_code_for_error(self.code)
        detail = message or self.code or self.response_status or 'unknown error'
        super().__init__(f'Responses API generation failed: {detail}')

    @staticmethod
    def _status_code_for_error(code):
        """映射为现有 failover 判定可识别的近似 HTTP 状态码。"""
        if not code:
            return None
        if 'timeout' in code:
            return 504
        if any(signal in code for signal in (
            'server_error', 'internal_error', 'service_unavailable', 'overloaded',
        )):
            return 500
        if 'rate_limit' in code:
            return 429
        # Responses 的其余已知错误（invalid_prompt / invalid_image 等）属于请求问题，
        # 不应绕过当前端点切换到兜底端点重复发送相同请求。
        return 400


def _raise_for_responses_failure(response):
    """Responses 可用 2xx 返回 failed + error；在归一化前将其恢复为异常语义。"""
    status = str(_value(response, 'status', '') or '').strip().lower()
    if status == 'failed' or _value(response, 'error') is not None:
        raise ResponsesResultError(response)


def _create_on_endpoint(raw_client, endpoint, chat_kwargs):
    if endpoint.get('api_mode') == 'responses':
        response = raw_client.responses.create(**_responses_create_kwargs(chat_kwargs))
        _raise_for_responses_failure(response)
        return _as_chat_completion(response)
    return raw_client.chat.completions.create(**chat_kwargs)


class ResponsesChatClient:
    """为单一 Responses 端点提供项目既有的 chat.completions 调用外观。"""

    api_mode = 'responses'

    def __init__(self, raw_client, endpoint):
        self._raw = raw_client
        self._endpoint = endpoint
        self.base_url = getattr(raw_client, 'base_url', None) or endpoint.get('base_url', '')
        self._endpoint_model = endpoint.get('model', '')
        self.api_mode = endpoint.get('api_mode', 'responses')
        self.chat = _ChatProxy(self)

    def _create(self, kwargs):
        call_kwargs = dict(kwargs)
        call_kwargs['model'] = self._endpoint['model']
        return _create_on_endpoint(self._raw, self._endpoint, call_kwargs)


class FallbackChatClient:
    """按顺序尝试多个 OpenAI 兼容端点；前一个“不可用”时自动切到下一个。"""

    def __init__(self, endpoints):
        self._endpoints = endpoints
        # 仅故障转移（多端点）模式下关闭 SDK 重试、使用 failover 连接短超时
        self._raw = [_make_raw_client(ep, multi_endpoint=True) for ep in endpoints]
        # 保留主端点身份供旧的直接调用方/诊断代码使用。统一请求层通过
        # _create_with_endpoint_fallback() 获取实际处理请求的端点代理，避免
        # thinking 控制和兼容性缓存把备用端点的能力写入主端点。
        self.base_url = getattr(self._raw[0], "base_url", None) or endpoints[0].get("base_url", "")
        self.api_mode = endpoints[0].get('api_mode', 'chat_completions')
        # 兼容 client.chat.completions.create(...) 调用链
        self.chat = _ChatProxy(self)

    def _create_with_endpoint_fallback(self, request_callback):
        """Run an endpoint-aware request callback with normal failover rules.

        The callback is responsible for retries that change request shape
        (for example dropping ``response_format``).  Such retries must stay on
        the same endpoint; this method only switches endpoints for genuine
        availability errors raised by the callback.
        """
        last_exc = None
        for idx, ep in enumerate(self._endpoints):
            endpoint_client = _FallbackEndpointClient(self._raw[idx], ep)
            try:
                return request_callback(endpoint_client)
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

    def _create(self, kwargs):
        return self._create_with_endpoint_fallback(
            lambda endpoint_client: endpoint_client.chat.completions.create(**dict(kwargs))
        )


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
    - 单一 Chat 端点返回与原来一致的裸 OpenAI 客户端；单一 Responses 端点
      返回保留 chat.completions 调用外观的 ResponsesChatClient。
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
    fb_default_api_mode = (primary or {}).get('api_mode') or 'chat_completions'
    fb_default_query = (primary or {}).get('default_query') or {}
    fb = _build_endpoint(
        "FALLBACK_OPENAI_", openai_config,
        default_base=fb_default_base, default_model=fb_default_model,
        default_api_mode=fb_default_api_mode, default_query=fb_default_query,
    )
    if fb:
        endpoints.append(fb)

    if not endpoints:
        raise RuntimeError("未配置任何可用 AI 端点（OPENAI_API_KEY 为空）")
    if len(endpoints) == 1:
        # 单端点（无兜底）：保留 SDK 默认重试与用户配置超时，行为与原先一致
        raw_client = _make_raw_client(endpoints[0], multi_endpoint=False)
        if endpoints[0].get('api_mode') == 'responses':
            return ResponsesChatClient(raw_client, endpoints[0])
        return raw_client
    return FallbackChatClient(endpoints)
