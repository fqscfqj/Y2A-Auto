"""针对 ai_fallback_client 的单元测试。

覆盖审查反馈中要求的故障转移矩阵与配置传播路径：
- 5xx / 连接超时 / 超时 → 切换兜底端点
- 4xx（含错误文本出现 "500"）→ 不切换，原样抛出
- 读取超时不被硬性截断为 20s；连接阶段使用可配置 failover 超时
- 兜底配置（FALLBACK_OPENAI_*）即便调用方未显式透传，也能从全局配置补齐
- 各调用路径（元数据翻译 / 标签 / 分区 / 字幕）都能实际拿到兜底配置
"""
import unittest
from unittest.mock import patch, MagicMock

import openai
from openai import APIConnectionError, APITimeoutError, APIStatusError

from modules import ai_fallback_client as afc


# ---------------------------------------------------------------------------
# 异常桩：避免构造真实 APIStatusError（需要 request/response 上下文）
# ---------------------------------------------------------------------------
class _StubStatusError(APIStatusError):
    def __init__(self, message, status_code):
        self.status_code = status_code
        self.message = message
        self.request = None
        self.body = None

    def __str__(self):
        return self.message


class _StubTimeoutError(APITimeoutError):
    def __init__(self, message="timed out"):
        self.message = message

    def __str__(self):
        return self.message


class _StubConnectionError(APIConnectionError):
    def __init__(self, message="connection refused"):
        self.message = message

    def __str__(self):
        return self.message


class _StubGenericError(Exception):
    pass


# ---------------------------------------------------------------------------
# 假 OpenAI 客户端
# ---------------------------------------------------------------------------
class _FakeCompletions:
    def __init__(self, owner):
        self._owner = owner

    def create(self, **kwargs):
        return self._owner._create(kwargs)


class _FakeChat:
    def __init__(self, owner):
        self.completions = _FakeCompletions(owner)


class _FakeClient:
    def __init__(self, name, side_effect=None, response=None):
        self._name = name
        self._side_effect = side_effect
        self._response = response
        self.chat = _FakeChat(self)
        self.last_kwargs = None

    def _create(self, kwargs):
        self.last_kwargs = kwargs
        if self._side_effect is not None:
            raise self._side_effect
        return self._response


def _fake_maker(primary_side_effect=None, primary_response=None, fallback_response=None):
    """返回 (maker, primary_fake, fallback_fake)。"""
    primary = _FakeClient("primary", side_effect=primary_side_effect, response=primary_response)
    fallback = _FakeClient("fb", response=fallback_response)

    def maker(ep, **kwargs):
        # 端点 label 由 _build_endpoint 生成：主=openai:<base>，兜=fallback_openai:<base>
        if "fallback" in (ep.get("label") or ""):
            return fallback
        return primary

    return maker, primary, fallback


def _full_config(**overrides):
    cfg = {
        "OPENAI_API_KEY": "k1",
        "OPENAI_BASE_URL": "http://primary",
        "OPENAI_MODEL_NAME": "m1",
        "OPENAI_TIMEOUT_SECONDS": 600,
        "FALLBACK_OPENAI_API_KEY": "k2",
        "FALLBACK_OPENAI_BASE_URL": "http://fb",
        "FALLBACK_OPENAI_MODEL_NAME": "m2",
    }
    cfg.update(overrides)
    return cfg


class IsUnavailableErrorTests(unittest.TestCase):
    def test_connection_error_is_unavailable(self):
        self.assertTrue(afc._is_unavailable_error(_StubConnectionError()))

    def test_timeout_error_is_unavailable(self):
        self.assertTrue(afc._is_unavailable_error(_StubTimeoutError()))

    def test_5xx_is_unavailable(self):
        self.assertTrue(afc._is_unavailable_error(_StubStatusError("down", 503)))
        self.assertTrue(afc._is_unavailable_error(_StubStatusError("down", 500)))

    def test_4xx_is_not_unavailable(self):
        self.assertFalse(afc._is_unavailable_error(_StubStatusError("bad request", 400)))
        self.assertFalse(afc._is_unavailable_error(_StubStatusError("not found", 404)))

    def test_4xx_with_500_in_text_is_not_unavailable(self):
        # 关键回归：HTTP 400 但消息含 "500 tokens" 不应触发兜底
        err = _StubStatusError("maximum output is 500 tokens", 400)
        self.assertFalse(afc._is_unavailable_error(err))

    def test_generic_connection_signal_is_unavailable(self):
        self.assertTrue(afc._is_unavailable_error(_StubGenericError("connection refused: errno 111")))
        self.assertTrue(afc._is_unavailable_error(_StubGenericError("name or service not known")))

    def test_generic_5xx_text_with_context_is_unavailable(self):
        # 无结构化状态码，但文本带明确 5xx 语境
        self.assertTrue(afc._is_unavailable_error(_StubGenericError("Error code: 502")))
        self.assertTrue(afc._is_unavailable_error(_StubGenericError("502 Bad Gateway")))

    def test_generic_500_in_text_without_context_is_not_unavailable(self):
        # "500 tokens" 这种 4xx 语义文本，不应被误判为 5xx 宕机
        self.assertFalse(afc._is_unavailable_error(_StubGenericError("maximum output is 500 tokens")))
        self.assertFalse(afc._is_unavailable_error(_StubGenericError("some unrelated error")))


class FallbackSwitchingTests(unittest.TestCase):
    def test_5xx_on_primary_switches_to_fallback(self):
        maker, primary, fallback = _fake_maker(
            primary_side_effect=_StubStatusError("down", 503),
            fallback_response={"choices": [{"message": {"content": "FB"}}]},
        )
        with patch.object(afc, "_make_raw_client", side_effect=maker):
            client = afc.get_ai_client(_full_config())
        result = client.chat.completions.create(model="ignored", messages=[])
        self.assertEqual(result["choices"][0]["message"]["content"], "FB")
        self.assertIsNotNone(primary.last_kwargs)
        self.assertIsNotNone(fallback.last_kwargs)

    def test_connection_timeout_on_primary_switches_to_fallback(self):
        maker, primary, fallback = _fake_maker(
            primary_side_effect=_StubTimeoutError(),
            fallback_response={"choices": [{"message": {"content": "FB"}}]},
        )
        with patch.object(afc, "_make_raw_client", side_effect=maker):
            client = afc.get_ai_client(_full_config())
        result = client.chat.completions.create(model="ignored", messages=[])
        self.assertEqual(result["choices"][0]["message"]["content"], "FB")
        self.assertIsNotNone(fallback.last_kwargs)

    def test_4xx_on_primary_does_not_switch(self):
        maker, primary, fallback = _fake_maker(
            primary_side_effect=_StubStatusError("bad request", 400),
        )
        with patch.object(afc, "_make_raw_client", side_effect=maker):
            client = afc.get_ai_client(_full_config())
        with self.assertRaises(APIStatusError):
            client.chat.completions.create(model="ignored", messages=[])
        # 兜底端点绝不应被调用
        self.assertIsNone(fallback.last_kwargs)

    def test_4xx_with_500_in_text_does_not_switch(self):
        maker, primary, fallback = _fake_maker(
            primary_side_effect=_StubStatusError("maximum output is 500 tokens", 400),
        )
        with patch.object(afc, "_make_raw_client", side_effect=maker):
            client = afc.get_ai_client(_full_config())
        with self.assertRaises(APIStatusError):
            client.chat.completions.create(model="ignored", messages=[])
        self.assertIsNone(fallback.last_kwargs)

    def test_model_is_taken_from_each_endpoint(self):
        maker, primary, fallback = _fake_maker(
            primary_side_effect=_StubStatusError("down", 503),
            fallback_response={"ok": True},
        )
        with patch.object(afc, "_make_raw_client", side_effect=maker):
            client = afc.get_ai_client(_full_config())
        client.chat.completions.create(model="caller-wants-this", messages=[])
        # 主端点用自己的模型，兜底端点用自己的模型（调用方 model 被覆盖）
        self.assertEqual(primary.last_kwargs["model"], "m1")
        self.assertEqual(fallback.last_kwargs["model"], "m2")

    def test_no_fallback_config_single_endpoint_no_switch(self):
        cfg = _full_config()
        for k in afc.FALLBACK_KEYS:
            cfg.pop(k, None)
        maker, primary, fallback = _fake_maker(
            primary_side_effect=_StubStatusError("down", 503),
        )
        with patch.object(afc, "_make_raw_client", side_effect=maker):
            client = afc.get_ai_client(cfg)
        # 单端点：直接返回裸客户端，而非 FallbackChatClient
        self.assertNotIsInstance(client, afc.FallbackChatClient)
        with self.assertRaises(APIStatusError):
            client.chat.completions.create(model="x", messages=[])

    def test_no_fallback_config_single_endpoint_success(self):
        cfg = _full_config()
        for k in afc.FALLBACK_KEYS:
            cfg.pop(k, None)
        maker, primary, fallback = _fake_maker(primary_response={"choices": [{"message": {"content": "OK"}}]})
        with patch.object(afc, "_make_raw_client", side_effect=maker):
            client = afc.get_ai_client(cfg)
        result = client.chat.completions.create(model="x", messages=[])
        self.assertEqual(result["choices"][0]["message"]["content"], "OK")
        self.assertIsNone(fallback.last_kwargs)


class ConfigPropagationTests(unittest.TestCase):
    def test_global_fallback_fields_only_returns_set_keys(self):
        with patch.object(afc, "_load_global_config", return_value={
            "FALLBACK_OPENAI_API_KEY": "k",
            "FALLBACK_OPENAI_BASE_URL": "",  # 空，不应返回
            "FALLBACK_OPENAI_MODEL_NAME": "m",
        }):
            fields = afc._global_fallback_fields()
        self.assertEqual(set(fields.keys()), {"FALLBACK_OPENAI_API_KEY", "FALLBACK_OPENAI_MODEL_NAME"})

    def test_resolve_fallback_merges_from_global(self):
        with patch.object(afc, "_load_global_config", return_value={
            "FALLBACK_OPENAI_API_KEY": "k",
            "FALLBACK_OPENAI_BASE_URL": "u",
            "FALLBACK_OPENAI_MODEL_NAME": "m",
        }):
            merged = afc._resolve_fallback_fields({"OPENAI_API_KEY": "x"})
        self.assertEqual(merged["FALLBACK_OPENAI_API_KEY"], "k")
        self.assertEqual(merged["FALLBACK_OPENAI_BASE_URL"], "u")
        self.assertEqual(merged["FALLBACK_OPENAI_MODEL_NAME"], "m")

    def test_resolve_fallback_keeps_explicit_values(self):
        with patch.object(afc, "_load_global_config", return_value={
            "FALLBACK_OPENAI_API_KEY": "global-k",
            "FALLBACK_OPENAI_BASE_URL": "global-u",
            "FALLBACK_OPENAI_MODEL_NAME": "global-m",
        }):
            merged = afc._resolve_fallback_fields({
                "OPENAI_API_KEY": "x",
                "FALLBACK_OPENAI_API_KEY": "local-k",
            })
        # 显式传入的优先，缺失项才从全局补齐
        self.assertEqual(merged["FALLBACK_OPENAI_API_KEY"], "local-k")
        self.assertEqual(merged["FALLBACK_OPENAI_BASE_URL"], "global-u")

    def test_call_path_without_explicit_fallback_still_gets_fallback(self):
        # 模拟各调用路径早期只透传 OPENAI_* 的配置（旧 task_manager / subtitle 字典形态）
        # 只要全局配置了兜底端点，统一客户端都应拿到 → 返回多端点 FallbackChatClient。
        partial_configs = [
            {"OPENAI_API_KEY": "x", "OPENAI_BASE_URL": "u", "OPENAI_MODEL_NAME": "m",
             "OPENAI_THINKING_ENABLED": False},
            {"OPENAI_API_KEY": "x", "OPENAI_BASE_URL": "u", "OPENAI_MODEL_NAME": "m",
             "OPENAI_TIMEOUT_SECONDS": 600, "FIXED_PARTITION_ID": ""},
            {"OPENAI_API_KEY": "x", "OPENAI_BASE_URL": "u"},
        ]
        with patch.object(afc, "_load_global_config", return_value={
            "FALLBACK_OPENAI_API_KEY": "fk",
            "FALLBACK_OPENAI_BASE_URL": "fu",
            "FALLBACK_OPENAI_MODEL_NAME": "fm",
        }):
            for cfg in partial_configs:
                with patch.object(afc, "_make_raw_client", side_effect=lambda ep, **kw: _FakeClient(ep.get("label"))):
                    client = afc.get_ai_client(cfg)
                self.assertIsInstance(client, afc.FallbackChatClient,
                                      f"调用路径 {cfg} 未拿到兜底配置")
                self.assertEqual(len(client._endpoints), 2)


class TimeoutPreservationTests(unittest.TestCase):
    def test_read_timeout_preserved_not_capped_multi(self):
        # 回归测试：多端点（故障转移）模式下，读取超时不得被硬性截断为 20s
        with patch.object(afc, "_load_global_config", return_value={}):
            client = afc._make_raw_client(
                {"api_key": "k", "base_url": "http://x", "timeout": 600}, multi_endpoint=True)
        self.assertEqual(client.timeout.read, 600.0)
        # 连接阶段使用默认 failover 短超时，并关闭 SDK 重试
        self.assertEqual(client.timeout.connect, afc._FAILOVER_CONNECT_TIMEOUT_DEFAULT)
        self.assertEqual(client.max_retries, 0)

    def test_single_endpoint_preserves_sdk_retry_and_user_timeout(self):
        # P2：单端点（无兜底）不应强制 max_retries=0，连接超时应与读取一致（用户配置），
        # 保留 SDK 默认重试以恢复瞬时 5xx / 连接失败；即“未配置兜底时行为完全不变”。
        with patch.object(afc, "_load_global_config", return_value={}):
            client = afc._make_raw_client(
                {"api_key": "k", "base_url": "http://x", "timeout": 600}, multi_endpoint=False)
        self.assertEqual(client.timeout.read, 600.0)
        self.assertEqual(client.timeout.connect, 600.0)
        self.assertEqual(client.max_retries, 2)  # OpenAI SDK 默认 2 次

    def test_read_timeout_custom_preserved(self):
        with patch.object(afc, "_load_global_config", return_value={}):
            client = afc._make_raw_client(
                {"api_key": "k", "base_url": "http://x", "timeout": 30}, multi_endpoint=True)
        self.assertEqual(client.timeout.read, 30.0)

    def test_failover_connect_timeout_configurable(self):
        with patch.object(afc, "_load_global_config", return_value={"AI_FAILOVER_TIMEOUT_SECONDS": 3}):
            client = afc._make_raw_client(
                {"api_key": "k", "base_url": "http://x", "timeout": 600}, multi_endpoint=True)
        self.assertEqual(client.timeout.connect, 3.0)
        self.assertEqual(client.timeout.read, 600.0)

    def test_failover_connect_timeout_out_of_range_falls_back(self):
        # P2：AI_FAILOVER_TIMEOUT_SECONDS 越界（如 -5 / 99）读路径应回退默认 8s
        for bad in (-5, 0, 99, 1000):
            with patch.object(afc, "_load_global_config", return_value={"AI_FAILOVER_TIMEOUT_SECONDS": bad}):
                self.assertEqual(
                    afc._get_failover_connect_timeout(),
                    afc._FAILOVER_CONNECT_TIMEOUT_DEFAULT,
                    f"越界值 {bad} 应回退默认 8s")
        # 合法区间返回原值
        with patch.object(afc, "_load_global_config", return_value={"AI_FAILOVER_TIMEOUT_SECONDS": 30}):
            self.assertEqual(afc._get_failover_connect_timeout(), 30.0)


class FallbackInheritsPrimaryTests(unittest.TestCase):
    def test_empty_fallback_url_model_inherits_primary(self):
        # P1：设置页声明“兜底 URL / 模型留空则沿用主端点”，空字段应继承主端点，
        # 而非回退到硬编码的 api.openai.com/v1 + gpt-3.5-turbo。
        cfg = {
            "OPENAI_API_KEY": "k1",
            "OPENAI_BASE_URL": "https://primary.example/v1",
            "OPENAI_MODEL_NAME": "primary-model",
            "FALLBACK_OPENAI_API_KEY": "k2",
            # FALLBACK_OPENAI_BASE_URL / FALLBACK_OPENAI_MODEL_NAME 故意留空
        }
        with patch.object(afc, "_load_global_config", return_value={}), \
             patch.object(afc, "_make_raw_client", side_effect=lambda ep, **kw: dict(ep)):
            client = afc.get_ai_client(cfg)
        self.assertIsInstance(client, afc.FallbackChatClient)
        fb_ep = client._endpoints[1]
        self.assertEqual(fb_ep["base_url"], "https://primary.example/v1")
        self.assertEqual(fb_ep["model"], "primary-model")

    def test_explicit_fallback_url_not_overridden_by_primary(self):
        # 显式填写的兜底 URL / 模型应保留，不被主端点覆盖
        cfg = {
            "OPENAI_API_KEY": "k1",
            "OPENAI_BASE_URL": "https://primary.example/v1",
            "OPENAI_MODEL_NAME": "primary-model",
            "FALLBACK_OPENAI_API_KEY": "k2",
            "FALLBACK_OPENAI_BASE_URL": "https://fb.example/v1",
            "FALLBACK_OPENAI_MODEL_NAME": "fb-model",
        }
        with patch.object(afc, "_load_global_config", return_value={}), \
             patch.object(afc, "_make_raw_client", side_effect=lambda ep, **kw: dict(ep)):
            client = afc.get_ai_client(cfg)
        fb_ep = client._endpoints[1]
        self.assertEqual(fb_ep["base_url"], "https://fb.example/v1")
        self.assertEqual(fb_ep["model"], "fb-model")

    def test_primary_only_single_endpoint_returns_raw_client(self):
        # 仅主端点（无兜底 key）时仍返回裸客户端，且保留 SDK 重试
        cfg = {
            "OPENAI_API_KEY": "k1",
            "OPENAI_BASE_URL": "https://primary.example/v1",
            "OPENAI_MODEL_NAME": "primary-model",
        }
        with patch.object(afc, "_load_global_config", return_value={}), \
             patch.object(afc, "_make_raw_client", side_effect=lambda ep, **kw: ep):
            client = afc.get_ai_client(cfg)
        self.assertNotIsInstance(client, afc.FallbackChatClient)


if __name__ == "__main__":
    unittest.main()


class DeepSeekThinkingControlTests(unittest.TestCase):
    """P2 回归：DeepSeek 端点仅在调用方显式要求禁用思考时才注入 enable_thinking=False，
    不得覆盖调用方显式开启的思考模式。"""

    def _create_with_extra(self, extra_body, deepseek_base="https://api.deepseek.com/v1"):
        maker, primary, fallback = _fake_maker(
            primary_side_effect=_StubStatusError("down", 503),
            fallback_response={"ok": True},
        )
        cfg = _full_config(
            FALLBACK_OPENAI_BASE_URL=deepseek_base,
            FALLBACK_OPENAI_MODEL_NAME="deepseek-reasoner",
        )
        with patch.object(afc, "_make_raw_client", side_effect=maker):
            client = afc.get_ai_client(cfg)
        kwargs = {"model": "ignored", "messages": []}
        if extra_body is not None:
            kwargs["extra_body"] = extra_body
        client.chat.completions.create(**kwargs)
        return fallback.last_kwargs.get("extra_body")

    def test_thinking_enabled_is_not_overridden(self):
        # 调用方启用思考：不发送 thinking 键 → 不得注入 enable_thinking=False
        extra = self._create_with_extra(None)
        self.assertIsNone(extra)
        # 即使带其它 extra_body（无 thinking 键）也不得注入 enable_thinking
        extra2 = self._create_with_extra({"temperature": 0.5})
        self.assertEqual(extra2, {"temperature": 0.5})  # 调用方 extra 原样保留
        self.assertNotIn("enable_thinking", extra2)

    def test_thinking_disabled_injects_deepseek_flag(self):
        # 调用方显式禁用思考：thinking={type:disabled,enabled:False}
        # → 移除 thinking 键并写入 DeepSeek 原生 enable_thinking=False
        extra = self._create_with_extra(
            {"thinking": {"type": "disabled", "enabled": False}})
        self.assertIsNotNone(extra)
        self.assertNotIn("thinking", extra)
        self.assertIs(extra.get("enable_thinking"), False)

    def test_thinking_disabled_flag_true_is_not_overridden(self):
        # 若调用方发送 thinking={enabled: True}（部分实现用该形式表示启用），
        # 也不得强制关闭
        extra = self._create_with_extra({"thinking": {"enabled": True}})
        self.assertIsNotNone(extra)
        self.assertIsNone(extra.get("enable_thinking"))


class TimeoutNonPositiveNormalizationTests(unittest.TestCase):
    """P2 回归：OPENAI_TIMEOUT_SECONDS<=0 视为未配置，沿用 SDK 默认超时，
    不得抛 httpx timeout range error。"""

    def test_build_endpoint_normalizes_non_positive_to_zero(self):
        for bad in (0, -1, "-30", "0"):
            with patch.object(afc, "_load_global_config", return_value={}):
                ep = afc._build_endpoint("OPENAI_", {"OPENAI_API_KEY": "k",
                                                     "OPENAI_TIMEOUT_SECONDS": bad})
            self.assertEqual(ep["timeout"], 0.0, f"非正值 {bad} 应规范化为 0.0 哨兵")

    def test_single_endpoint_non_positive_uses_sdk_default_timeout(self):
        # <=0 单端点：不传 timeout → OpenAI SDK 默认（connect 5s，read/write 600s）
        with patch.object(afc, "_load_global_config", return_value={}):
            client = afc._make_raw_client(
                {"api_key": "k", "base_url": "http://x", "timeout": 0.0}, multi_endpoint=False)
        self.assertEqual(client.timeout.connect, 5.0)
        self.assertEqual(client.timeout.read, 600.0)
        self.assertEqual(client.max_retries, 2)  # SDK 默认重试仍保留

    def test_multi_endpoint_non_positive_uses_failover_connect_and_sdk_read(self):
        # <=0 多端点：连接用 failover 短超时，读取/写入用 SDK 默认 600s
        with patch.object(afc, "_load_global_config", return_value={}):
            client = afc._make_raw_client(
                {"api_key": "k", "base_url": "http://x", "timeout": -5}, multi_endpoint=True)
        self.assertEqual(client.timeout.connect, afc._FAILOVER_CONNECT_TIMEOUT_DEFAULT)
        self.assertEqual(client.timeout.read, 600.0)
        self.assertEqual(client.max_retries, 0)

    def test_negative_timeout_does_not_crash(self):
        # 负数直接进 _make_raw_client（绕过 _build_endpoint）也不得抛 range error
        with patch.object(afc, "_load_global_config", return_value={}):
            client = afc._make_raw_client(
                {"api_key": "k", "base_url": "http://x", "timeout": -999}, multi_endpoint=False)
        self.assertIsNotNone(client)


class SubtitleQcTimeoutDefaultTests(unittest.TestCase):
    """P2 回归：QC 仅在用户显式配置 OPENAI_TIMEOUT_SECONDS 时覆盖，
    未配置保持 120s 默认，不放大到 600s。"""

    def _capture_cfg(self, global_cfg):
        captured = {}

        def _fake_get_ai_client(cfg):
            captured['cfg'] = dict(cfg)
            return MagicMock()

        # _build_openai_client 函数内是 `from X import Y` 运行时绑定，
        # 需 patch 源模块属性（modules.config_manager.load_config 等）。
        with patch("modules.config_manager.load_config", return_value=global_cfg), \
             patch("modules.ai_fallback_client.get_ai_client", side_effect=_fake_get_ai_client):
            from modules.subtitle_qc import _build_openai_client
            _build_openai_client("k", "https://x/v1", "m")
        return captured['cfg']

    def test_unconfigured_defaults_to_120(self):
        cfg = self._capture_cfg({})
        self.assertEqual(cfg["OPENAI_TIMEOUT_SECONDS"], 120)

    def test_explicitly_configured_value_is_used(self):
        cfg = self._capture_cfg({"OPENAI_TIMEOUT_SECONDS": 30})
        self.assertEqual(cfg["OPENAI_TIMEOUT_SECONDS"], 30)

    def test_empty_string_treated_as_unconfigured(self):
        cfg = self._capture_cfg({"OPENAI_TIMEOUT_SECONDS": ""})
        self.assertEqual(cfg["OPENAI_TIMEOUT_SECONDS"], 120)
