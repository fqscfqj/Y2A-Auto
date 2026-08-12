*** Begin Patch
*** Update File: modules/youtube_handler.py
@@
 from urllib.parse import parse_qs, urlparse
 import re
+import tempfile
@@
 def _youtube_cookies_look_authenticated(cookies_path: str | None) -> tuple[bool, str | None]:
@@
     except Exception as exc:
         return False, f"读取cookies失败: {exc}"
+
+
+def _is_netscape_cookie_file(path: str) -> bool:
+    """快速判断是否为 Netscape cookies.txt（简单检测头或行列数）。"""
+    try:
+        with open(path, "r", encoding="utf-8", errors="ignore") as f:
+            for i, ln in enumerate(f):
+                ln = ln.strip()
+                if i == 0 and ln.startswith("# Netscape HTTP Cookie File"):
+                    return True
+                if not ln or ln.startswith("#"):
+                    continue
+                parts = ln.split("\t")
+                if len(parts) >= 7:
+                    return True
+                if i > 50:
+                    break
+    except Exception:
+        return False
+    return False
+
+
+def _convert_any_to_netscape(input_path: str, output_path: str) -> bool:
+    """
+    简单尝试把非标准导出（制表或表格形式）转换为 Netscape 格式。
+    这是一个启发式实现，适用于常见的浏览器导出样式。
+    """
+    try:
+        from modules.tools.convert_cookies import convert_any_to_netscape as _conv
+    except Exception:
+        return False
+    try:
+        res = _conv(input_path, output_path)
+        return bool(res and os.path.exists(res))
+    except Exception:
+        return False
+
+
+def _ensure_netscape_cookies(cookies_path: str, log: logging.Logger | None = None) -> str | None:
+    """
+    如果 cookies_path 不是 Netscape 格式，尝试把它转换为临时的 Netscape 文件并返回新路径；
+    若已是 Netscape，直接返回原路径；若转换失败，返回原路径并在日志中记录警告。
+    """
+    _log = log or logger
+    try:
+        if not cookies_path or not os.path.exists(cookies_path):
+            return None
+        if _is_netscape_cookie_file(cookies_path):
+            return cookies_path
+
+        # create temp output path next to original for visibility
+        tmp_dir = os.path.dirname(os.path.realpath(cookies_path)) or tempfile.gettempdir()
+        fd, tmp_path = tempfile.mkstemp(prefix="y2a_yt_cookies_", suffix=".txt", dir=tmp_dir)
+        os.close(fd)
+        success = _convert_any_to_netscape(cookies_path, tmp_path)
+        if success and os.path.exists(tmp_path):
+            _log.info("自动将 cookies 转换为 Netscape 格式: %s", tmp_path)
+            return tmp_path
+        # 清理若转换失败
+        try:
+            if os.path.exists(tmp_path):
+                os.remove(tmp_path)
+        except Exception:
+            pass
+        _log.warning("无法将 cookies 自动转换为 Netscape 格式，仍使用原文件: %s", cookies_path)
+        return cookies_path
+    except Exception as exc:
+        _log.warning("检查/转换 cookies 时出错，仍尝试使用原文件: %s", exc)
+        return cookies_path
*** End Patch
