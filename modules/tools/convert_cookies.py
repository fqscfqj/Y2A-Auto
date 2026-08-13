*** Begin Patch
*** Update File: modules/tools/convert_cookies.py
@@
-"""
-将多种浏览器导出格式（表格型 / 简易制表符 / Netscape 标准）尝试转换为 yt-dlp 可用的 Netscape cookies.txt。
-提供命令行使用也可供程序内 import 使用。
-"""
+"""
+将多种浏览器导出格式（表格型 / 简易制表符 / Netscape 标准）尝试转换为 yt-dlp 可用的 Netscape cookies.txt。
+提供命令行使用也可供程序内 import 使用。
+
+注意：一些浏览器/扩展会导出 JSON 格式（如 EditThisCookie）。当前实现主要针对文本表格/TSV/CSV
+的启发式转换，并不会尝试解析 JSON 导出。若需要对 JSON 自动识别并转换，应额外实现 JSON 解析逻辑。
+"""
@@
 def is_netscape_file(path: str) -> bool:
@@
-                # Skip a common CSV/TSV header like: name value domain path expiration
-                lower = ln.lower()
-                if all(tok in lower for tok in ("name", "value", "domain")) and len(lower.split()) <= 8:
-                    # treat as header line and skip
-                    continue
+                # Skip a common CSV/TSV header like: name value domain path expiration
+                # such header lines may appear at the file head;如果检测到此类行则跳过
+                lower = ln.lower()
+                if all(tok in lower for tok in ("name", "value", "domain")) and len(lower.split()) <= 8:
+                    # treat as header line and skip
+                    continue
@@
-    # heuristics parse each non-empty line
+    # heuristics parse each non-empty line
     with open(input_path, "r", encoding="utf-8", errors="ignore") as f:
         for i, raw in enumerate(f):
             line = raw.strip()
             if not line or line.startswith("#"):
                 continue
             # Skip header lines like: name value domain path expiration
             lower = line.lower()
             if all(tok in lower for tok in ("name", "value", "domain")) and len(lower.split()) <= 8:
                 continue
*** End Patch
