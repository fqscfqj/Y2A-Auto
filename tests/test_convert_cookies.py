import ast
import json
import logging
import os
import pathlib
import tempfile
import unittest

from modules import youtube_handler
from modules.tools import convert_cookies


class ConvertCookiesTests(unittest.TestCase):
    def _write(self, directory, name, content):
        path = pathlib.Path(directory) / name
        path.write_text(content, encoding="utf-8")
        return str(path)

    def test_is_netscape_file_rejects_header_only_file(self):
        with tempfile.TemporaryDirectory() as d:
            # 只有 "# Netscape HTTP Cookie File" 头部、没有任何数据行的文件不算有效 Netscape
            path = self._write(d, "header_only.txt", "# Netscape HTTP Cookie File\n")
            self.assertFalse(convert_cookies.is_netscape_file(path))

    def test_is_netscape_file_detects_header(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(
                d, "c.txt",
                "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tFALSE\t0\tSID\tx\n",
            )
            self.assertTrue(convert_cookies.is_netscape_file(path))

    def test_is_netscape_file_detects_httponly_rows(self):
        with tempfile.TemporaryDirectory() as d:
            # 纯 #HttpOnly_ 文件（带 HttpOnly 标志的合法 Netscape 数据行）
            path = self._write(
                d, "httponly.txt",
                "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1800000000\tSID\tsecret\n",
            )
            self.assertTrue(convert_cookies.is_netscape_file(path))
            # 混合：普通数据行 + #HttpOnly_ 数据行
            mixed = self._write(
                d, "mixed.txt",
                ".youtube.com\tTRUE\t/\tFALSE\t0\tSID\tx\n"
                "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1800000000\tHSID\ty\n",
            )
            self.assertTrue(convert_cookies.is_netscape_file(mixed))

    def test_is_netscape_file_rejects_text_table(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(
                d, "c.txt",
                "name value domain path expiration\nSID x .youtube.com / 2027-09-14T13:19:23.436Z\n",
            )
            self.assertFalse(convert_cookies.is_netscape_file(path))

    def test_is_netscape_file_rejects_fake_7col_tsv(self):
        with tempfile.TemporaryDirectory() as d:
            # 表头形态的伪 7 列 TSV
            path = self._write(d, "fake_header.txt", "name value domain path expiration secure httpOnly\n")
            self.assertFalse(convert_cookies.is_netscape_file(path))
            # 数据行形态：tab 分隔 7 列但第 2/4 列不是 TRUE/FALSE
            path2 = self._write(d, "fake_row.txt", "SID\tx\t.youtube.com\t/\t1\tTRUE\tFALSE\n")
            self.assertFalse(convert_cookies.is_netscape_file(path2))

    def test_convert_text_table_to_netscape(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._write(d, "c.txt", "SID\tvalue1\t.youtube.com\t/\t2027-09-14T13:19:23.436Z\n")
            out = self._write(d, "out.txt", "")
            res = convert_cookies.convert_any_to_netscape(src, out)
            self.assertEqual(res, out)

            lines = pathlib.Path(out).read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "# Netscape HTTP Cookie File")
            parts = lines[1].split("\t")
            self.assertEqual(len(parts), 7)
            self.assertEqual(parts[0], ".youtube.com")
            self.assertEqual(parts[1], "TRUE")
            self.assertEqual(parts[2], "/")
            self.assertEqual(parts[3], "FALSE")
            self.assertTrue(parts[4].isdigit())
            self.assertEqual(parts[5], "SID")
            self.assertEqual(parts[6], "value1")

    def test_numeric_value_not_mistaken_for_expiration(self):
        with tempfile.TemporaryDirectory() as d:
            # 数值型 value（1234567890）不应被当成过期时间，第 5 列应为最后一列的 1800000000
            src = self._write(d, "c.txt", "SID\t1234567890\t.youtube.com\t/\t1800000000\n")
            out = self._write(d, "out.txt", "")
            res = convert_cookies.convert_any_to_netscape(src, out)
            self.assertEqual(res, out)

            parts = pathlib.Path(out).read_text(encoding="utf-8").splitlines()[1].split("\t")
            self.assertEqual(parts[4], "1800000000")
            self.assertEqual(parts[6], "1234567890")

    def test_expiration_reads_first_epoch_after_path(self):
        # 过期时间应从 path 后第一列从左往右取，右侧的 1/0（如 httpOnly/secure 标志）不应覆盖真实时间
        parsed = convert_cookies._parse_text_cookie_line("SID\tvalue\t.youtube.com\t/\t1800000000\t1\t0")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["expiration"], "1800000000")

    def test_inf_nan_not_treated_as_epoch(self):
        self.assertFalse(convert_cookies._looks_like_epoch("inf"))
        self.assertFalse(convert_cookies._looks_like_epoch("nan"))
        # 处理含 inf 的输入不应抛异常，按会话 cookie（0）处理
        self.assertEqual(convert_cookies._to_epoch_str("inf"), "0")
        lines = convert_cookies.convert_to_netscape_lines("SID\tvalue\t.youtube.com\t/\tinf\n")
        self.assertIsNotNone(lines)
        self.assertEqual(lines[1].split("\t")[4], "0")

    def test_csv_and_space_separated_convert(self):
        with tempfile.TemporaryDirectory() as d:
            for idx, text in enumerate([
                "SID,value1,.youtube.com,/,2027-09-14T13:19:23.436Z\n",   # CSV
                "SID value1 .youtube.com / 2027-09-14T13:19:23.436Z\n",  # 空格分隔
            ]):
                src = self._write(d, f"c{idx}.txt", text)
                out = self._write(d, f"out{idx}.txt", "")
                res = convert_cookies.convert_any_to_netscape(src, out)
                self.assertEqual(res, out)

                parts = pathlib.Path(out).read_text(encoding="utf-8").splitlines()[1].split("\t")
                self.assertEqual(len(parts), 7)
                self.assertEqual(parts[0], ".youtube.com")
                self.assertEqual(parts[5], "SID")
                self.assertEqual(parts[6], "value1")
                self.assertTrue(parts[4].isdigit())

    def test_convert_json_to_netscape(self):
        with tempfile.TemporaryDirectory() as d:
            data = [{
                "domain": ".youtube.com",
                "name": "SID",
                "value": "abc",
                "path": "/",
                "secure": True,
                "expirationDate": 1800000000.0,
            }]
            src = self._write(d, "c.json", json.dumps(data))
            out = self._write(d, "out.txt", "")
            res = convert_cookies.convert_any_to_netscape(src, out)

            parts = pathlib.Path(res).read_text(encoding="utf-8").splitlines()[1].split("\t")
            self.assertEqual(parts[0], ".youtube.com")
            self.assertEqual(parts[1], "TRUE")
            self.assertEqual(parts[3], "TRUE")  # secure=True
            self.assertEqual(parts[4], "1800000000")
            self.assertEqual(parts[5], "SID")
            self.assertEqual(parts[6], "abc")

    def test_convert_no_valid_cookies_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            # 无有效 cookie 记录（name/value 缺失），转换应返回 None 且不遗留输出文件
            src = self._write(d, "c.json", '[{"foo": "bar"}]')
            out = os.path.join(d, "out.txt")
            pathlib.Path(out).write_text("stale", encoding="utf-8")
            res = convert_cookies.convert_any_to_netscape(src, out)
            self.assertIsNone(res)
            self.assertFalse(os.path.exists(out))

    def test_garbage_text_without_domain_is_rejected(self):
        # 不含 YouTube 域名的普通文本不应被当成 cookie
        self.assertIsNone(convert_cookies.convert_to_netscape_lines("this is not a cookie file at all"))
        self.assertIsNone(convert_cookies.convert_to_netscape_lines("SID value1 / 2027-09-14T13:19:23.436Z"))

    def test_convert_to_netscape_lines_handles_httponly(self):
        lines = convert_cookies.convert_to_netscape_lines(
            "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1800000000\tSID\tsecret\n"
        )
        self.assertIsNotNone(lines)
        # 去掉 #HttpOnly_ 前缀后透传为 Netscape 数据行
        self.assertEqual(lines[1], ".youtube.com\tTRUE\t/\tTRUE\t1800000000\tSID\tsecret")

    def test_convert_httponly_file_passthrough(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._write(
                d, "c.txt",
                "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1800000000\tSID\tsecret\n",
            )
            # 已是合法 Netscape（含 #HttpOnly_ 行），应直接透传返回原路径
            self.assertEqual(convert_cookies.convert_any_to_netscape(src), src)

    def test_secure_prefix_sets_secure_flag(self):
        line = convert_cookies._render_netscape_line(".youtube.com", "__Secure-1PSID", "v")
        self.assertIsNotNone(line)
        self.assertEqual(line.split("\t")[3], "TRUE")

    def test_naive_iso_datetime_treated_as_utc(self):
        naive = convert_cookies._iso8601_to_epoch("2027-09-14T13:19:23")
        utc = convert_cookies._iso8601_to_epoch("2027-09-14T13:19:23Z")
        self.assertEqual(naive, utc)
        self.assertGreater(int(naive), 0)

    def test_sanitize_field_removes_control_chars(self):
        sanitized = convert_cookies._sanitize_field("a\tb\nc\rd")
        self.assertNotIn("\t", sanitized)
        self.assertNotIn("\n", sanitized)
        self.assertNotIn("\r", sanitized)

    def test_render_line_never_emits_control_chars(self):
        # 恶意 value 含制表符/换行，不应注入额外的 Netscape 行
        line = convert_cookies._render_netscape_line(
            ".youtube.com", "SID",
            "evil\t.domain.com\tTRUE\t/\tFALSE\t0\tinjected\tx\nmore",
        )
        self.assertIsNotNone(line)
        self.assertEqual(len(line.split("\t")), 7)
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)


class EnsureNetscapeCookiesTests(unittest.TestCase):
    def test_passthrough_when_already_netscape(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "yt.txt"
            path.write_text(
                "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tFALSE\t0\tSID\tx\n",
                encoding="utf-8",
            )
            res = youtube_handler._ensure_netscape_cookies(str(path), logging.getLogger("test"))
            self.assertEqual(os.path.normpath(res), os.path.normpath(str(path)))

    def test_converts_when_not_netscape(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "yt.txt"
            path.write_text(
                '[{"domain": ".youtube.com", "name": "SID", "value": "x", "path": "/"}]',
                encoding="utf-8",
            )
            res = youtube_handler._ensure_netscape_cookies(str(path), logging.getLogger("test"))
            self.assertIsNotNone(res)
            self.assertNotEqual(os.path.normpath(res), os.path.normpath(str(path)))
            self.assertTrue(pathlib.Path(res).is_file())
            content = pathlib.Path(res).read_text(encoding="utf-8")
            self.assertIn("# Netscape HTTP Cookie File", content)
            self.assertIn("SID", content)

    def test_different_sources_get_different_derived_files(self):
        with tempfile.TemporaryDirectory() as d:
            path1 = pathlib.Path(d) / "yt1.txt"
            path1.write_text(
                '[{"domain": ".youtube.com", "name": "SID", "value": "aaa", "path": "/"}]',
                encoding="utf-8",
            )
            path2 = pathlib.Path(d) / "yt2.txt"
            path2.write_text(
                '[{"domain": ".youtube.com", "name": "SID", "value": "bbb", "path": "/"}]',
                encoding="utf-8",
            )
            res1 = youtube_handler._ensure_netscape_cookies(str(path1), logging.getLogger("test"))
            res2 = youtube_handler._ensure_netscape_cookies(str(path2), logging.getLogger("test"))
            self.assertIsNotNone(res1)
            self.assertIsNotNone(res2)
            # 不同源文件必须得到不同派生路径，避免互相覆盖
            self.assertNotEqual(os.path.normpath(res1), os.path.normpath(res2))
            self.assertTrue(pathlib.Path(res1).is_file())
            self.assertTrue(pathlib.Path(res2).is_file())
            # 内容各自正确、互不覆盖
            c1 = pathlib.Path(res1).read_text(encoding="utf-8")
            c2 = pathlib.Path(res2).read_text(encoding="utf-8")
            self.assertIn("SID", c1)
            self.assertIn("SID", c2)
            self.assertIn("\tSID\taaa", c1)
            self.assertIn("\tSID\tbbb", c2)
            self.assertNotIn("\tSID\tbbb", c1)
            self.assertNotIn("\tSID\taaa", c2)
            # 同一源文件重复转换复用同一派生路径
            res1_again = youtube_handler._ensure_netscape_cookies(str(path1), logging.getLogger("test"))
            self.assertEqual(os.path.normpath(res1_again), os.path.normpath(res1))

    def test_concurrent_same_source_returns_valid_netscape(self):
        import threading
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "yt.txt"
            path.write_text(
                '[{"domain": ".youtube.com", "name": "SID", "value": "x", "path": "/"}]',
                encoding="utf-8",
            )
            results = []
            errors = []

            def worker():
                try:
                    results.append(
                        youtube_handler._ensure_netscape_cookies(str(path), logging.getLogger("test"))
                    )
                except Exception as exc:  # pragma: no cover - 记录异常供断言
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            self.assertEqual(len(results), 20)
            # 同一源文件并发转换：所有线程都应拿到同一个有效的派生 Netscape 路径
            normalized = {os.path.normpath(r) for r in results}
            self.assertEqual(len(normalized), 1)
            res = results[0]
            self.assertTrue(pathlib.Path(res).is_file())
            self.assertIn("SID", pathlib.Path(res).read_text(encoding="utf-8"))
            # 没有任何线程返回原始 JSON 路径
            self.assertNotIn(os.path.normpath(str(path)), normalized)


class YoutubeHandlerCookieWiringTests(unittest.TestCase):
    def test_handlers_call_ensure_netscape_cookies(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        source = (root / "modules" / "youtube_handler.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        targets = {"download_video_data", "extract_video_urls_from_playlist"}
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name in targets
        }
        self.assertEqual(set(functions), targets)
        for func_node in functions.values():
            called = {
                node.func.id
                for node in ast.walk(func_node)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            self.assertIn("_ensure_netscape_cookies", called)


if __name__ == "__main__":
    unittest.main()
