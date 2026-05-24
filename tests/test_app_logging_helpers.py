import ast
import json
import pathlib
import unittest


def _load_functions(*names):
    app_path = pathlib.Path(__file__).resolve().parents[1] / "app.py"
    source = app_path.read_text(encoding="utf-8")
    module_ast = ast.parse(source, filename=str(app_path))
    selected = [
        node for node in module_ast.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    isolated_module = ast.Module(body=selected, type_ignores=[])
    namespace = {}
    exec(compile(isolated_module, str(app_path), "exec"), namespace)
    return [namespace[name] for name in names]


class AppLoggingHelperTests(unittest.TestCase):
    def test_status_mapping_uses_fixed_messages(self):
        describe_status, = _load_functions("_describe_youtube_api_status")

        self.assertEqual(
            describe_status("direct_ready"),
            "YouTube API 初始化成功，当前为直连模式"
        )
        self.assertEqual(
            describe_status("proxy_ready"),
            "YouTube API 初始化成功，独立代理已启用"
        )
        self.assertEqual(
            describe_status("init_failed"),
            "YouTube监控 API 初始化失败，请检查 API 密钥、代理配置与网络连通性。"
        )

    def test_startup_config_summary_contains_only_booleans_for_sensitive_fields(self):
        build_summary, = _load_functions("_build_startup_config_log_summary")
        summary = build_summary({
            "AUTO_MODE_ENABLED": True,
            "password": "super-secret",
            "OPENAI_API_KEY": "sk-secret",
            "YOUTUBE_API_KEY": "yt-secret",
            "ALIYUN_ACCESS_KEY_SECRET": "aliyun-secret",
            "YOUTUBE_COOKIES_PATH": "cookies/yt_cookies.txt",
            "COOKIECLOUD_ENABLED": True,
            "COOKIECLOUD_SERVER_URL": "https://cookiecloud.example.com",
            "COOKIECLOUD_UUID": "cookiecloud-secret-uuid",
            "COOKIECLOUD_PASSWORD": "cookiecloud-secret-password",
        })
        serialized = json.dumps(summary, ensure_ascii=False)

        self.assertTrue(summary["feature_flags"]["AUTO_MODE_ENABLED"])
        self.assertTrue(summary["feature_flags"]["COOKIECLOUD_ENABLED"])
        self.assertNotIn("credentials_configured", summary)
        self.assertNotIn("path_configured", summary)
        self.assertNotIn("super-secret", serialized)
        self.assertNotIn("sk-secret", serialized)
        self.assertNotIn("yt-secret", serialized)
        self.assertNotIn("aliyun-secret", serialized)
        self.assertNotIn("cookiecloud-secret-uuid", serialized)
        self.assertNotIn("cookiecloud-secret-password", serialized)
        self.assertNotIn("cookiecloud.example.com", serialized)
        self.assertNotIn("cookies/yt_cookies.txt", serialized)

    def test_cookiecloud_helpers_sanitize_errors_and_preserve_password(self):
        _coerce_checkbox_value, merge_runtime_settings, build_error_message = _load_functions(
            "_coerce_checkbox_value",
            "_merge_cookiecloud_runtime_settings",
            "_cookiecloud_operation_error_message",
        )

        merged = merge_runtime_settings(
            {
                "COOKIECLOUD_ENABLED": True,
                "COOKIECLOUD_PASSWORD": "",
                "COOKIECLOUD_UUID": "incoming-uuid",
            },
            {
                "COOKIECLOUD_PASSWORD": "stored-secret",
                "COOKIECLOUD_UUID": "stored-uuid",
            },
        )

        self.assertEqual(merged["COOKIECLOUD_PASSWORD"], "stored-secret")
        self.assertEqual(merged["COOKIECLOUD_UUID"], "incoming-uuid")

        test_message = build_error_message("test")
        sync_message = build_error_message("sync", retry_later=True)
        self.assertIn("CookieCloud", test_message)
        self.assertIn("CookieCloud", sync_message)
        self.assertNotIn("stored-secret", test_message + sync_message)
        self.assertNotIn("incoming-uuid", test_message + sync_message)

    def test_cookiecloud_runtime_merge_ignores_non_mapping_payload(self):
        _coerce_checkbox_value, merge_runtime_settings = _load_functions(
            "_coerce_checkbox_value",
            "_merge_cookiecloud_runtime_settings",
        )

        merged = merge_runtime_settings(
            ["unexpected", "payload"],
            {
                "COOKIECLOUD_ENABLED": True,
                "COOKIECLOUD_PASSWORD": "stored-secret",
                "COOKIECLOUD_UUID": "stored-uuid",
            },
        )

        self.assertTrue(merged["COOKIECLOUD_ENABLED"])
        self.assertEqual(merged["COOKIECLOUD_PASSWORD"], "stored-secret")
        self.assertEqual(merged["COOKIECLOUD_UUID"], "stored-uuid")


if __name__ == "__main__":
    unittest.main()
