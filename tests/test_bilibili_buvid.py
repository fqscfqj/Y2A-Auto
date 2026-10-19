import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from modules.bilibili_auth import (
    build_credential,
    ensure_credential_buvid,
    load_credential_from_file,
    save_credential_to_file,
)


class BuvidPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cookie_path = os.path.join(self.temp_dir.name, "bili_cookies.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _cookie_names(self):
        with open(self.cookie_path, encoding="utf-8") as stream:
            return [item["name"] for item in json.load(stream)]

    def test_save_keeps_buvid_and_uuid(self):
        credential = build_credential(
            {
                "SESSDATA": "sess",
                "bili_jct": "jct",
                "DedeUserID": "42",
                "buvid3": "b3-value",
                "buvid4": "b4-value",
                "_uuid": "uuid-value",
            }
        )
        self.assertTrue(save_credential_to_file(credential, self.cookie_path))

        names = self._cookie_names()
        self.assertIn("buvid3", names)
        self.assertIn("buvid4", names)
        self.assertIn("_uuid", names)
        # 设备字段按固定顺序落在文件里，方便人工排查
        self.assertLess(names.index("buvid4"), names.index("_uuid"))

        reloaded = load_credential_from_file(self.cookie_path)
        self.assertEqual(reloaded.buvid3, "b3-value")
        self.assertEqual(reloaded.buvid4, "b4-value")
        self.assertEqual(reloaded.get_cookies().get("_uuid"), "uuid-value")

    def test_save_drops_internal_underscore_keys(self):
        credential = build_credential(
            {"SESSDATA": "sess", "bili_jct": "jct", "DedeUserID": "42", "_uuid": "keep-me"}
        )
        credential._extra_cookies["_internal_debug"] = "drop-me"

        self.assertTrue(save_credential_to_file(credential, self.cookie_path))

        names = self._cookie_names()
        self.assertIn("_uuid", names)
        self.assertNotIn("_internal_debug", names)

    def test_ensure_generates_buvid_once(self):
        credential = build_credential({"SESSDATA": "sess", "bili_jct": "jct", "DedeUserID": "42"})
        self.assertFalse(credential.has_buvid3())

        with patch(
            "modules.bilibili_auth.get_buvid",
            new=AsyncMock(return_value=("new-b3", "new-b4")),
        ), patch("modules.bilibili_auth.get_buvid_uuid", return_value="new-uuid"):
            self.assertTrue(ensure_credential_buvid(credential))
            # 第二次不应重复生成
            self.assertFalse(ensure_credential_buvid(credential))

        self.assertEqual(credential.buvid3, "new-b3")
        self.assertEqual(credential.buvid4, "new-b4")
        self.assertEqual(credential.get_cookies().get("_uuid"), "new-uuid")

    def test_ensure_keeps_existing_buvid(self):
        credential = build_credential(
            {
                "SESSDATA": "sess",
                "bili_jct": "jct",
                "DedeUserID": "42",
                "buvid3": "keep-b3",
                "buvid4": "keep-b4",
            }
        )
        with patch(
            "modules.bilibili_auth.get_buvid",
            new=AsyncMock(side_effect=AssertionError("不应重新生成 buvid")),
        ), patch("modules.bilibili_auth.get_buvid_uuid", return_value=""):
            self.assertFalse(ensure_credential_buvid(credential))

        self.assertEqual(credential.buvid3, "keep-b3")
        self.assertEqual(credential.buvid4, "keep-b4")

    def test_extra_cookies_survive_round_trip(self):
        credential = build_credential(
            {
                "SESSDATA": "sess",
                "bili_jct": "jct",
                "DedeUserID": "42",
                "sid": "sid-value",
                "DedeUserID__ckMd5": "ckmd5-value",
                "b_nut": "1700000000",
            }
        )
        self.assertTrue(save_credential_to_file(credential, self.cookie_path))

        reloaded = load_credential_from_file(self.cookie_path).get_cookies()
        self.assertEqual(reloaded["sid"], "sid-value")
        self.assertEqual(reloaded["DedeUserID__ckMd5"], "ckmd5-value")
        self.assertEqual(reloaded["b_nut"], "1700000000")

    def test_round_trip_survives_save_and_load(self):
        credential = build_credential({"SESSDATA": "sess", "bili_jct": "jct", "DedeUserID": "42"})
        with patch(
            "modules.bilibili_auth.get_buvid",
            new=AsyncMock(return_value=("rt-b3", "rt-b4")),
        ), patch("modules.bilibili_auth.get_buvid_uuid", return_value="rt-uuid"):
            ensure_credential_buvid(credential)
        self.assertTrue(save_credential_to_file(credential, self.cookie_path))

        reloaded = load_credential_from_file(self.cookie_path)
        cookies = reloaded.get_cookies()
        self.assertEqual(cookies["buvid3"], "rt-b3")
        self.assertEqual(cookies["buvid4"], "rt-b4")
        self.assertEqual(cookies["_uuid"], "rt-uuid")


if __name__ == "__main__":
    unittest.main()
