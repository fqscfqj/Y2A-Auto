import json
import unittest

from modules.security_logging import (
    REDACTED_VALUE,
    build_config_summary_for_logging,
    describe_network_mode,
    redact_config_for_logging,
    strip_url_credentials,
)


class SecurityLoggingTests(unittest.TestCase):
    def test_strip_url_credentials_removes_userinfo(self):
        sanitized = strip_url_credentials("http://alice:topsecret@proxy.example.com:7890")
        self.assertEqual(sanitized, "http://proxy.example.com:7890")

    def test_describe_network_mode_keeps_host_and_port_only(self):
        mode = describe_network_mode("socks5://alice:topsecret@127.0.0.1:1080")
        self.assertEqual(mode, "代理 socks5://127.0.0.1:1080")
        self.assertNotIn("alice", mode)
        self.assertNotIn("topsecret", mode)

    def test_redact_config_for_logging_masks_sensitive_values(self):
        config = {
            "OPENAI_API_KEY": "sk-live-secret",
            "password": "super-pass",
            "YOUTUBE_API_PROXY_URL": "http://proxy.example.com:7890",
            "nested": {
                "token": "nested-secret",
            },
        }
        redacted = redact_config_for_logging(config)
        self.assertEqual(redacted["OPENAI_API_KEY"], REDACTED_VALUE)
        self.assertEqual(redacted["password"], REDACTED_VALUE)
        self.assertEqual(redacted["nested"]["token"], REDACTED_VALUE)
        self.assertEqual(redacted["YOUTUBE_API_PROXY_URL"], "http://proxy.example.com:7890")

    def test_config_summary_does_not_include_sensitive_plaintext(self):
        config = {
            "AUTO_MODE_ENABLED": True,
            "password_protection_enabled": True,
            "YOUTUBE_API_KEY": "api-key-secret",
            "YOUTUBE_API_PROXY_PASSWORD": "proxy-password-secret",
            "YOUTUBE_API_PROXY_URL": "http://proxy.example.com:7890",
        }
        summary = build_config_summary_for_logging(config)
        summary_text = json.dumps(summary, ensure_ascii=False)

        self.assertTrue(summary["feature_flags"]["AUTO_MODE_ENABLED"])
        self.assertTrue(summary["credentials_configured"]["YOUTUBE_API_KEY"])
        self.assertTrue(summary["credentials_configured"]["YOUTUBE_API_PROXY_PASSWORD"])
        self.assertNotIn("api-key-secret", summary_text)
        self.assertNotIn("proxy-password-secret", summary_text)


if __name__ == "__main__":
    unittest.main()

