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


TEST_UUID = "cookiecloud-test-uuid"
TEST_PASSWORD = hashlib.sha256(TEST_UUID.encode("utf-8")).hexdigest()[:24]


def _derive_key_seed(uuid_value, password):
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        uuid_value.encode("utf-8"),
        200000,
        dklen=16,
    )


def _aes_cbc_encrypt(plaintext_bytes, key, iv):
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext_bytes) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _evp_bytes_to_key(password_seed, salt, key_len=32, iv_len=16):
    derived = hashlib.pbkdf2_hmac("sha256", password_seed, salt, 200000, dklen=key_len + iv_len)
    return derived[:key_len], derived[key_len:key_len + iv_len]


def _encrypt_legacy(payload, uuid_value=TEST_UUID, password=TEST_PASSWORD, salt=b"12345678"):
    plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    password_seed = _derive_key_seed(uuid_value, password)
    key, iv = _evp_bytes_to_key(password_seed, salt)
    encrypted = _aes_cbc_encrypt(plaintext, key, iv)
    return base64.b64encode(b"Salted__" + salt + encrypted).decode("utf-8")


def _encrypt_fixed(payload, uuid_value=TEST_UUID, password=TEST_PASSWORD):
    plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    key = _derive_key_seed(uuid_value, password)
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
        auto_url = build_cookiecloud_get_url("https://cookiecloud.example.com/", TEST_UUID, crypto_type="auto")
        legacy_url = build_cookiecloud_get_url("https://cookiecloud.example.com/", TEST_UUID, crypto_type="legacy")
        fixed_url = build_cookiecloud_get_url("https://cookiecloud.example.com/root", TEST_UUID, crypto_type="aes-128-cbc-fixed")

        self.assertEqual(auto_url, f"https://cookiecloud.example.com/get/{TEST_UUID}")
        self.assertEqual(legacy_url, f"https://cookiecloud.example.com/get/{TEST_UUID}")
        self.assertEqual(
            fixed_url,
            f"https://cookiecloud.example.com/root/get/{TEST_UUID}?crypto_type=aes-128-cbc-fixed",
        )

    def test_decrypt_cookiecloud_payload_supports_legacy(self):
        encrypted = _encrypt_legacy(self.payload)
        data, crypto_type = decrypt_cookiecloud_payload(
            {"encrypted": encrypted},
            TEST_UUID,
            TEST_PASSWORD,
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
            TEST_UUID,
            TEST_PASSWORD,
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

    def test_sync_cookiecloud_to_youtube_file_requires_plaintext_export_opt_in(self):
        with patch(
            "modules.cookiecloud.test_cookiecloud_youtube_sync",
            return_value={
                "content": "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2000000000\tSAPISID\tvalue\n",
                "cookie_count": 1,
                "crypto_type_used": COOKIECLOUD_CRYPTO_LEGACY,
            },
        ):
            with self.assertRaisesRegex(CookieCloudConfigError, "允许明文导出"):
                sync_cookiecloud_to_youtube_file({
                    "COOKIECLOUD_ENABLED": True,
                    "COOKIECLOUD_SERVER_URL": "https://cookiecloud.example.com",
                    "COOKIECLOUD_UUID": TEST_UUID,
                    "COOKIECLOUD_PASSWORD": TEST_PASSWORD,
                    "COOKIECLOUD_CRYPTO_TYPE": "auto",
                    "YOUTUBE_COOKIES_PATH": self.sync_relative_path,
                })

    def test_resolve_cookie_output_path_rejects_escape_outside_project_root(self):
        with self.assertRaises(CookieCloudConfigError):
            resolve_cookie_output_path(os.path.join("..", "outside-cookiecloud.txt"))

    def test_sync_cookiecloud_to_youtube_file_writes_generated_content(self):
        with patch(
            "modules.cookiecloud.test_cookiecloud_youtube_sync",
            return_value={
                "content": "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2000000000\tSAPISID\tvalue\n",
                "cookie_count": 1,
                "crypto_type_used": COOKIECLOUD_CRYPTO_LEGACY,
            },
        ):
            result = sync_cookiecloud_to_youtube_file({
                "COOKIECLOUD_ENABLED": True,
                "COOKIECLOUD_SERVER_URL": "https://cookiecloud.example.com",
                "COOKIECLOUD_UUID": TEST_UUID,
                "COOKIECLOUD_PASSWORD": TEST_PASSWORD,
                "COOKIECLOUD_ALLOW_PLAINTEXT_EXPORT": True,
                "COOKIECLOUD_CRYPTO_TYPE": "auto",
                "YOUTUBE_COOKIES_PATH": self.sync_relative_path,
            })

        self.assertEqual(result["cookie_count"], 1)
        self.assertEqual(result["output_path_display"].replace("\\", "/"), self.sync_relative_path.replace("\\", "/"))
        self.assertTrue(self.sync_absolute_path.exists())
        written = self.sync_absolute_path.read_text(encoding="utf-8")
        self.assertIn("SAPISID", written)


if __name__ == "__main__":
    unittest.main()
