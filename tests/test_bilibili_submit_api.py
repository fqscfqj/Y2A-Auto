import unittest
from unittest.mock import patch

from modules.bili_sdk.exceptions.NetworkException import NetworkException
from modules.bili_sdk.exceptions.ResponseCodeException import ResponseCodeException
from modules.bili_sdk.utils.AsyncEvent import AsyncEvent
from modules.bili_sdk.utils.network import Credential
from modules.bili_sdk.video_uploader import (
    BCUT_PC_BUILD,
    BCUT_PC_PLATFORM,
    BCUT_PC_USER_AGENT,
    SUBMIT_API_BCUT_PC,
    SUBMIT_API_WEB,
    VideoUploader,
    VideoUploaderEvents,
    get_submit_api_candidates,
    is_submit_risk_error,
    normalize_submit_api,
)
from modules.bilibili_uploader import (
    _bilibili_603_hint,
    _is_bilibili_account_risk,
    _submit_attempts_hit_account_risk,
)


class _FakeApi:
    """记录请求参数并返回预设结果的 Api 替身。"""

    calls = []
    results = []

    def __init__(self, **kwargs):
        self.spec = kwargs
        self.params = {}
        self.data = {}
        self.headers = {}
        self.credential = kwargs.get("credential")
        _FakeApi.calls.append(self)

    def update_params(self, **kwargs):
        self.params = kwargs
        return self

    def update_data(self, **kwargs):
        self.data = kwargs
        return self

    def update_headers(self, **kwargs):
        self.headers = kwargs
        return self

    @property
    def result(self):
        outcome = _FakeApi.results.pop(0)

        async def _resolve():
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return _resolve()


def _make_uploader(submit_api="auto"):
    uploader = object.__new__(VideoUploader)
    AsyncEvent.__init__(uploader)
    uploader.credential = Credential(sessdata="sess", bili_jct="jct")
    uploader.meta = {
        "title": "t",
        "copyright": 2,
        "tid": 124,
        "tag": "a,b",
        "desc": "",
        "videos": [],
    }
    uploader.submit_api = normalize_submit_api(submit_api)
    uploader.submit_api_used = None
    uploader.submit_attempts = []
    return uploader


_VIDEOS = [{"title": "t", "desc": "", "filename": "n12345"}]


class SubmitApiSelectionTests(unittest.TestCase):
    def setUp(self):
        _FakeApi.calls = []
        _FakeApi.results = []

    def test_normalize_and_candidates(self):
        self.assertEqual(normalize_submit_api(None), "auto")
        self.assertEqual(normalize_submit_api("BCUT-PC"), SUBMIT_API_BCUT_PC)
        self.assertEqual(normalize_submit_api("unknown"), "auto")
        # 默认 auto 必剪优先，Web 兜底
        self.assertEqual(get_submit_api_candidates("auto"), ["bcut_pc", "web"])
        self.assertEqual(get_submit_api_candidates("web"), [SUBMIT_API_WEB])
        self.assertEqual(get_submit_api_candidates("bcut_pc"), [SUBMIT_API_BCUT_PC])

    def test_risk_error_codes(self):
        self.assertTrue(is_submit_risk_error(ResponseCodeException(603, "x")))
        self.assertTrue(is_submit_risk_error(ResponseCodeException(-352, "x")))
        self.assertTrue(is_submit_risk_error(ResponseCodeException(-412, "x")))
        self.assertFalse(is_submit_risk_error(ResponseCodeException(21010, "x")))
        self.assertFalse(is_submit_risk_error(ValueError("boom")))

    def test_auto_tries_bcut_first(self):
        uploader = _make_uploader("auto")
        _FakeApi.results = [{"bvid": "BV1first", "aid": 1}]

        with patch("modules.bili_sdk.video_uploader.Api", _FakeApi):
            result = __import__("asyncio").run(uploader._submit(_VIDEOS))

        self.assertEqual(result["bvid"], "BV1first")
        self.assertEqual(uploader.submit_api_used, SUBMIT_API_BCUT_PC)
        self.assertEqual(len(_FakeApi.calls), 1)
        self.assertEqual(
            _FakeApi.calls[0].spec["url"],
            "https://member.bilibili.com/x/vu/mvp/pc/add",
        )

    def test_auto_falls_back_to_web_on_bcut_failure(self):
        uploader = _make_uploader("auto")
        _FakeApi.results = [
            ResponseCodeException(603, "系统检测到异常行为"),
            {"bvid": "BV1xx", "aid": 1},
        ]
        fallbacks = []
        uploader.add_event_listener(
            VideoUploaderEvents.SUBMIT_FALLBACK.value, fallbacks.append
        )

        with patch("modules.bili_sdk.video_uploader.Api", _FakeApi):
            result = __import__("asyncio").run(uploader._submit(_VIDEOS))

        self.assertEqual(result["bvid"], "BV1xx")
        self.assertEqual(uploader.submit_api_used, SUBMIT_API_WEB)
        self.assertEqual(len(fallbacks), 1)
        self.assertEqual(fallbacks[0]["from"], SUBMIT_API_BCUT_PC)
        self.assertEqual(fallbacks[0]["to"], SUBMIT_API_WEB)
        self.assertTrue(fallbacks[0]["risk"])
        self.assertEqual(len(_FakeApi.calls), 2)

    def test_falls_back_on_plain_rejection(self):
        # code != 0 说明稿件未创建，换通道重试不会重复投稿
        uploader = _make_uploader("auto")
        _FakeApi.results = [
            ResponseCodeException(21010, "稿件不可见"),
            {"bvid": "BV1retry", "aid": 5},
        ]

        with patch("modules.bili_sdk.video_uploader.Api", _FakeApi):
            result = __import__("asyncio").run(uploader._submit(_VIDEOS))

        self.assertEqual(result["bvid"], "BV1retry")
        self.assertEqual(len(_FakeApi.calls), 2)

    def test_does_not_fall_back_on_login_or_rate_limit(self):
        for code in (-101, -111, 137022):
            with self.subTest(code=code):
                _FakeApi.calls = []
                _FakeApi.results = [ResponseCodeException(code, "x")]
                uploader = _make_uploader("auto")
                with patch("modules.bili_sdk.video_uploader.Api", _FakeApi):
                    with self.assertRaises(ResponseCodeException):
                        __import__("asyncio").run(uploader._submit(_VIDEOS))
                self.assertEqual(len(_FakeApi.calls), 1)
                self.assertIsNone(uploader.submit_api_used)

    def test_network_timeout_does_not_fall_back(self):
        uploader = _make_uploader("auto")
        _FakeApi.results = [NetworkException(504, "Gateway Timeout")]

        with patch("modules.bili_sdk.video_uploader.Api", _FakeApi):
            with self.assertRaises(NetworkException):
                __import__("asyncio").run(uploader._submit(_VIDEOS))

        self.assertEqual(len(_FakeApi.calls), 1)

    def test_network_412_does_fall_back(self):
        uploader = _make_uploader("auto")
        _FakeApi.results = [
            NetworkException(412, "Precondition Failed"),
            {"bvid": "BV1n", "aid": 9},
        ]
        fallbacks = []
        uploader.add_event_listener(
            VideoUploaderEvents.SUBMIT_FALLBACK.value, fallbacks.append
        )

        with patch("modules.bili_sdk.video_uploader.Api", _FakeApi):
            result = __import__("asyncio").run(uploader._submit(_VIDEOS))

        self.assertEqual(result["bvid"], "BV1n")
        self.assertEqual(len(_FakeApi.calls), 2)
        # NetworkException 只有 .status，事件里的 code 不能是 None
        self.assertEqual(len(fallbacks), 1)
        self.assertEqual(fallbacks[0]["code"], 412)
        self.assertTrue(fallbacks[0]["risk"])
        self.assertEqual(
            uploader.submit_attempts,
            [
                {
                    "api": SUBMIT_API_BCUT_PC,
                    "code": 412,
                    "risk": True,
                    "error": "网络错误，状态码：412 - Precondition Failed。",
                }
            ],
        )

    def test_submit_failed_carries_every_attempt(self):
        # 必剪 603 → Web -352：最终异常不是 603，但尝试记录里两个通道都在，
        # 上层据此仍能给出 603 排障提示。
        uploader = _make_uploader("auto")
        _FakeApi.results = [
            ResponseCodeException(603, "系统检测到异常行为"),
            ResponseCodeException(-352, "风控校验失败"),
        ]
        failures = []
        uploader.add_event_listener(
            VideoUploaderEvents.SUBMIT_FAILED.value, failures.append
        )

        with patch("modules.bili_sdk.video_uploader.Api", _FakeApi):
            with self.assertRaises(ResponseCodeException):
                __import__("asyncio").run(uploader._submit(_VIDEOS))

        self.assertEqual(
            [(item["api"], item["code"]) for item in uploader.submit_attempts],
            [(SUBMIT_API_BCUT_PC, 603), (SUBMIT_API_WEB, -352)],
        )
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["api"], SUBMIT_API_WEB)
        self.assertEqual(len(failures[0]["attempts"]), 2)
        self.assertIsNone(uploader.submit_api_used)

    def test_bcut_request_shape(self):
        uploader = _make_uploader("bcut_pc")
        _FakeApi.results = [{"bvid": "BV1yy", "aid": 2}]

        with patch("modules.bili_sdk.video_uploader.Api", _FakeApi):
            __import__("asyncio").run(uploader._submit(_VIDEOS))

        self.assertEqual(len(_FakeApi.calls), 1)
        call = _FakeApi.calls[0]
        self.assertEqual(call.spec["url"], "https://member.bilibili.com/x/vu/mvp/pc/add")
        self.assertEqual(call.spec["method"], "POST")
        self.assertEqual(call.params["platform"], BCUT_PC_PLATFORM)
        self.assertEqual(call.params["build"], BCUT_PC_BUILD)
        self.assertEqual(call.params["csrf"], "jct")
        self.assertEqual(call.data["csrf"], "jct")
        self.assertEqual(call.headers["User-Agent"], BCUT_PC_USER_AGENT)
        self.assertNotIn("Origin", call.headers)
        self.assertNotIn("Referer", call.headers)

    def test_web_request_shape(self):
        uploader = _make_uploader("web")
        _FakeApi.results = [{"bvid": "BV1zz", "aid": 3}]

        with patch("modules.bili_sdk.video_uploader.Api", _FakeApi):
            __import__("asyncio").run(uploader._submit(_VIDEOS))

        call = _FakeApi.calls[0]
        self.assertEqual(call.spec["url"], "https://member.bilibili.com/x/vu/web/add/v3")
        self.assertEqual(call.headers, {})
        self.assertIn("t", call.params)


class AccountRiskHintTests(unittest.TestCase):
    def test_603_is_detected(self):
        self.assertTrue(_is_bilibili_account_risk(ResponseCodeException(603, "x")))
        self.assertFalse(_is_bilibili_account_risk(ResponseCodeException(406, "x")))

    def test_hint_mentions_recovery_path(self):
        hint = _bilibili_603_hint()
        self.assertIn("603", hint)
        self.assertIn("实名", hint)

    def test_hint_reflects_actual_attempted_channels(self):
        both = _bilibili_603_hint(
            [{"api": SUBMIT_API_BCUT_PC}, {"api": SUBMIT_API_WEB}]
        )
        self.assertIn("「必剪 PC 接口」「Web 接口」两条通道", both)

        single = _bilibili_603_hint([{"api": SUBMIT_API_WEB}])
        self.assertIn("本次只走了「Web 接口」（单通道模式）", single)
        self.assertNotIn("两条通道", single)

    def test_reraises_when_risk_code_hidden_behind_another_error(self):
        self.assertTrue(
            _submit_attempts_hit_account_risk([{"api": "bcut_pc", "code": 603}])
        )
        self.assertFalse(
            _submit_attempts_hit_account_risk(
                [{"api": "bcut_pc", "code": -352}, {"api": "web", "code": -412}]
            )
        )
        self.assertFalse(_submit_attempts_hit_account_risk(None))


if __name__ == "__main__":
    unittest.main()
