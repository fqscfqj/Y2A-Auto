import base64
import hashlib
import json
import os
import pathlib
import shutil
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from modules.cookiecloud import (
    CookieCloudConfigError,
    COOKIECLOUD_CRYPTO_AES_128_CBC_FIXED,
    COOKIECLOUD_CRYPTO_LEGACY,
    build_cookiecloud_get_url,
    build_youtube_netscape_cookies,
    decrypt_cookiecloud_payload,
    resolve_cookie_output_path,
    sync_cookiecloud_to_youtube_file,
)


TEST_CC_USER = "cookiecloud-test-uuid"
TEST_CC_KEY = hashlib.sha256(TEST_CC_USER.encode("utf-8")).hexdigest()[:24]


def _derive_cryptojs_key(cc_user, cc_key):
    return hashlib.md5(f"{cc_user}-{cc_key}".encode("utf-8"), usedforsecurity=False).hexdigest()[:16].encode("utf-8")


def _derive_preview_compat_key_seed(cc_user, cc_key):
    return hashlib.pbkdf2_hmac(
        "sha256",
        cc_key.encode("utf-8"),
        cc_user.encode("utf-8"),
        200000,
        dklen=16,
    )


def _aes_cbc_encrypt(plaintext_bytes, key, iv):
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext_bytes) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _evp_md5_derive(data, salt, key_len=32, iv_len=16):
    """Test helper: OpenSSL EVP_BytesToKey — MD5 required by protocol."""
    derived = b""
    prev = b""
    while len(derived) < key_len + iv_len:
        prev = hashlib.md5(prev + data + salt, usedforsecurity=False).digest()
        derived += prev
    return derived[:key_len], derived[key_len:key_len + iv_len]


def _preview_compat_pbkdf2_key_iv(seed, salt, key_len=32, iv_len=16):
    derived = hashlib.pbkdf2_hmac("sha256", seed, salt, 200000, dklen=key_len + iv_len)
    return derived[:key_len], derived[key_len:key_len + iv_len]


def _encrypt_legacy(payload, cc_user=TEST_CC_USER, cc_key=TEST_CC_KEY, salt=b"12345678"):
    plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    seed = _derive_cryptojs_key(cc_user, cc_key)
    key, iv = _evp_md5_derive(seed, salt)
    encrypted = _aes_cbc_encrypt(plaintext, key, iv)
    return base64.b64encode(b"Salted__" + salt + encrypted).decode("utf-8")


def _encrypt_fixed(payload, cc_user=TEST_CC_USER, cc_key=TEST_CC_KEY):
    plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    key = _derive_cryptojs_key(cc_user, cc_key)
    encrypted = _aes_cbc_encrypt(plaintext, key, b"\x00" * 16)
    return base64.b64encode(encrypted).decode("utf-8")


def _encrypt_legacy_preview_compat(payload, cc_user=TEST_CC_USER, cc_key=TEST_CC_KEY, salt=b"12345678"):
    plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    seed = _derive_preview_compat_key_seed(cc_user, cc_key)
    key, iv = _preview_compat_pbkdf2_key_iv(seed, salt)
    encrypted = _aes_cbc_encrypt(plaintext, key, iv)
    return base64.b64encode(b"Salted__" + salt + encrypted).decode("utf-8")


def _encrypt_fixed_preview_compat(payload, cc_user=TEST_CC_USER, cc_key=TEST_CC_KEY):
    plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    key = _derive_preview_compat_key_seed(cc_user, cc_key)
    encrypted = _aes_cbc_encrypt(plaintext, key, b"\x00" * 16)
    return base64.b64encode(encrypted).decode("utf-8")


class CookieCloudTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "cookie_data": {
                "youtube.com": [
                    {
                        "domain": ".youtube.com",
                        "hostOnly": False,
                        "path": "/",
                        "secure": True,
                        "expirationDate": 2000000000,
                        "name": "SAPISID",
                        "value": "youtube-sapisid",
                    },
                    {
                        "domain": "music.youtube.com",
                        "hostOnly": True,
                        "path": "/",
                        "secure": True,
                        "expirationDate": 2000000001,
                        "name": "LOGIN_INFO",
                        "value": "youtube-login-info",
                    },
                ],
                "google.com": [
                    {
                        "domain": ".google.com",
                        "hostOnly": False,
                        "path": "/",
                        "secure": True,
                        "expirationDate": 2000000002,
                        "name": "HSID",
                        "value": "google-hsid",
                    }
                ],
                "bilibili.com": [
                    {
                        "domain": ".bilibili.com",
                        "hostOnly": False,
                        "path": "/",
                        "secure": False,
                        "expirationDate": 2000000003,
                        "name": "SESSDATA",
                        "value": "should-not-appear",
                    }
                ],
            },
            "local_storage_data": {},
        }
        self.sync_relative_path = os.path.join("temp", "unit-tests", "cookiecloud", "yt_cookies.txt")
        self.sync_absolute_path = pathlib.Path(__file__).resolve().parents[1] / self.sync_relative_path
        self.sync_absolute_path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.sync_absolute_path.parent, ignore_errors=True)

    def test_build_cookiecloud_get_url_only_adds_query_for_fixed_iv(self):
        auto_url = build_cookiecloud_get_url("https://cookiecloud.example.com/", TEST_CC_USER, crypto_type="auto")
        legacy_url = build_cookiecloud_get_url("https://cookiecloud.example.com/", TEST_CC_USER, crypto_type="legacy")
        fixed_url = build_cookiecloud_get_url("https://cookiecloud.example.com/root", TEST_CC_USER, crypto_type="aes-128-cbc-fixed")

        self.assertEqual(auto_url, f"https://cookiecloud.example.com/get/{TEST_CC_USER}")
        self.assertEqual(legacy_url, f"https://cookiecloud.example.com/get/{TEST_CC_USER}")
        self.assertEqual(
            fixed_url,
            f"https://cookiecloud.example.com/root/get/{TEST_CC_USER}?crypto_type=aes-128-cbc-fixed",
        )

    def test_decrypt_cookiecloud_payload_supports_legacy(self):
        encrypted = _encrypt_legacy(self.payload)
        data, crypto_type = decrypt_cookiecloud_payload(
            {"encrypted": encrypted},
            TEST_CC_USER,
            TEST_CC_KEY,
            crypto_type=COOKIECLOUD_CRYPTO_LEGACY,
        )

        self.assertEqual(crypto_type, COOKIECLOUD_CRYPTO_LEGACY)
        self.assertEqual(data["cookie_data"]["youtube.com"][0]["name"], "SAPISID")

    def test_decrypt_cookiecloud_payload_auto_detects_fixed_iv(self):
        encrypted = _encrypt_fixed(self.payload)
        data, crypto_type = decrypt_cookiecloud_payload(
            {
                "encrypted": encrypted,
                "crypto_type": COOKIECLOUD_CRYPTO_AES_128_CBC_FIXED,
            },
            TEST_CC_USER,
            TEST_CC_KEY,
        )

        self.assertEqual(crypto_type, COOKIECLOUD_CRYPTO_AES_128_CBC_FIXED)
        self.assertEqual(data["cookie_data"]["google.com"][0]["name"], "HSID")

    def test_decrypt_cookiecloud_payload_auto_detects_fixed_iv_without_payload_crypto_type(self):
        encrypted = _encrypt_fixed(self.payload)
        data, crypto_type = decrypt_cookiecloud_payload(
            {"encrypted": encrypted},
            TEST_CC_USER,
            TEST_CC_KEY,
        )

        self.assertEqual(crypto_type, COOKIECLOUD_CRYPTO_AES_128_CBC_FIXED)
        self.assertEqual(data["cookie_data"]["google.com"][0]["name"], "HSID")

    def test_decrypt_cookiecloud_payload_keeps_preview_legacy_compatibility(self):
        encrypted = _encrypt_legacy_preview_compat(self.payload)
        data, crypto_type = decrypt_cookiecloud_payload(
            {"encrypted": encrypted},
            TEST_CC_USER,
            TEST_CC_KEY,
            crypto_type=COOKIECLOUD_CRYPTO_LEGACY,
        )

        self.assertEqual(crypto_type, COOKIECLOUD_CRYPTO_LEGACY)
        self.assertEqual(data["cookie_data"]["youtube.com"][0]["name"], "SAPISID")

    def test_decrypt_cookiecloud_payload_keeps_preview_fixed_iv_compatibility(self):
        encrypted = _encrypt_fixed_preview_compat(self.payload)
        data, crypto_type = decrypt_cookiecloud_payload(
            {
                "encrypted": encrypted,
                "crypto_type": COOKIECLOUD_CRYPTO_AES_128_CBC_FIXED,
            },
            TEST_CC_USER,
            TEST_CC_KEY,
        )

        self.assertEqual(crypto_type, COOKIECLOUD_CRYPTO_AES_128_CBC_FIXED)
        self.assertEqual(data["cookie_data"]["google.com"][0]["name"], "HSID")

    def test_build_youtube_netscape_cookies_filters_unrelated_domains(self):
        content, cookie_count = build_youtube_netscape_cookies(self.payload)

        self.assertEqual(cookie_count, 3)
        self.assertIn("SAPISID", content)
        self.assertIn("LOGIN_INFO", content)
        self.assertIn("HSID", content)
        self.assertNotIn("SESSDATA", content)
        self.assertIn("# Netscape HTTP Cookie File", content)

    def test_build_youtube_netscape_cookies_keeps_only_boundary_matching_domains(self):
        payload = {
            "cookie_data": {
                "notyoutube.com": [
                    {
                        "domain": ".notyoutube.com",
                        "hostOnly": False,
                        "path": "/",
                        "secure": True,
                        "expirationDate": 2000000100,
                        "name": "BAD",
                        "value": "bad-1",
                    }
                ],
                "youtube.com.evil.tld": [
                    {
                        "domain": ".youtube.com.evil.tld",
                        "hostOnly": False,
                        "path": "/",
                        "secure": True,
                        "expirationDate": 2000000101,
                        "name": "BAD2",
                        "value": "bad-2",
                    }
                ],
                "www.youtube.com": [
                    {
                        "domain": ".www.youtube.com",
                        "hostOnly": False,
                        "path": "/",
                        "secure": True,
                        "expirationDate": 2000000102,
                        "name": "GOOD1",
                        "value": "good-1",
                    }
                ],
                "mail.google.com": [
                    {
                        "domain": ".mail.google.com",
                        "hostOnly": False,
                        "path": "/",
                        "secure": True,
                        "expirationDate": 2000000103,
                        "name": "GOOD2",
                        "value": "good-2",
                    }
                ],
            }
        }

        content, cookie_count = build_youtube_netscape_cookies(payload)

        self.assertEqual(cookie_count, 2)
        self.assertIn("GOOD1", content)
        self.assertIn("GOOD2", content)
        self.assertNotIn("BAD\t", content)
        self.assertNotIn("BAD2\t", content)

    def test_build_youtube_netscape_cookies_sanitizes_field_separators(self):
        payload = {
            "cookie_data": {
                "youtube.com\n": [
                    {
                        "domain": ".youtube.com\n",
                        "hostOnly": False,
                        "path": "/\npath",
                        "secure": True,
                        "expirationDate": 2000000200,
                        "name": "SA\tPISID",
                        "value": "va\nlue",
                    }
                ]
            }
        }

        content, cookie_count = build_youtube_netscape_cookies(payload)
        cookie_line = content.strip().splitlines()[-1]
        fields = cookie_line.split("\t")

        self.assertEqual(cookie_count, 1)
        self.assertEqual(len(fields), 7)
        self.assertEqual(fields[0], ".youtube.com")
        self.assertEqual(fields[2], "/ path")
        self.assertEqual(fields[5], "SA PISID")
        self.assertEqual(fields[6], "va lue")

    def test_build_youtube_netscape_cookies_ignores_overflow_expiration_values(self):
        payload = {
            "cookie_data": {
                "youtube.com": [
                    {
                        "domain": ".youtube.com",
                        "hostOnly": False,
                        "path": "/",
                        "secure": True,
                        "expires": "1e309",
                        "name": "SAPISID",
                        "value": "value",
                    }
                ]
            }
        }

        content, cookie_count = build_youtube_netscape_cookies(payload)
        cookie_line = content.strip().splitlines()[-1]
        fields = cookie_line.split("\t")

        self.assertEqual(cookie_count, 1)
        self.assertEqual(fields[4], "0")

    def test_sync_cookiecloud_to_youtube_file_requires_plaintext_export_opt_in(self):
        with self.assertRaisesRegex(CookieCloudConfigError, "允许明文导出"):
            sync_cookiecloud_to_youtube_file({
                "COOKIECLOUD_ENABLED": True,
                "COOKIECLOUD_SERVER_URL": "https://cookiecloud.example.com",
                "COOKIECLOUD_UUID": TEST_CC_USER,
                "COOKIECLOUD_PASSWORD": TEST_CC_KEY,
                "COOKIECLOUD_CRYPTO_TYPE": "auto",
                "YOUTUBE_COOKIES_PATH": self.sync_relative_path,
            })

    def test_resolve_cookie_output_path_rejects_escape_outside_project_root(self):
        with self.assertRaises(CookieCloudConfigError):
            resolve_cookie_output_path(os.path.join("..", "outside-cookiecloud.txt"))

    def test_sync_cookiecloud_to_youtube_file_writes_generated_content(self):
        fake_payload = {"encrypted": "fake"}
        fake_decrypted = self.payload
        with patch(
            "modules.cookiecloud.fetch_cookiecloud_payload",
            return_value=fake_payload,
        ), patch(
            "modules.cookiecloud.decrypt_cookiecloud_payload",
            return_value=(fake_decrypted, COOKIECLOUD_CRYPTO_LEGACY),
        ):
            result = sync_cookiecloud_to_youtube_file({
                "COOKIECLOUD_ENABLED": True,
                "COOKIECLOUD_SERVER_URL": "https://cookiecloud.example.com",
                "COOKIECLOUD_UUID": TEST_CC_USER,
                "COOKIECLOUD_PASSWORD": TEST_CC_KEY,
                "COOKIECLOUD_ALLOW_PLAINTEXT_EXPORT": True,
                "COOKIECLOUD_CRYPTO_TYPE": "auto",
                "YOUTUBE_COOKIES_PATH": self.sync_relative_path,
            })

        self.assertEqual(result["cookie_count"], 3)
        self.assertEqual(result["output_path_display"].replace("\\", "/"), self.sync_relative_path.replace("\\", "/"))
        self.assertTrue(self.sync_absolute_path.exists())
        written = self.sync_absolute_path.read_text(encoding="utf-8")
        self.assertIn("SAPISID", written)


if __name__ == "__main__":
    unittest.main()
