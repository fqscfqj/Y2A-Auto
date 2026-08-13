#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快速诊断脚本：检查 yt-dlp / ffmpeg 可用性、cookies 文件存在与基本格式，并可选地用 yt-dlp 做一次格式列举。

用法：
    python tools/diagnose_env.py
    python tools/diagnose_env.py --use-cookies

默认不会在没有明确 --use-cookies 的情况下对外发起带 cookies 的网络请求，以避免触发反爬。
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# 以脚本位置定位项目根，避免依赖运行时的当前工作目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

COOKIES_DIR = PROJECT_ROOT / "cookies"
YT_COOKIES = COOKIES_DIR / "yt_cookies.txt"
BILI_COOKIES = COOKIES_DIR / "bili_cookies.json"
_KNOWN_PUBLIC_VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _first_line(text: str, limit: int = 200) -> str:
    lines = (text or "").strip().splitlines()
    if not lines:
        return ""
    return lines[0][:limit]


def check_command(cmd: str) -> tuple:
    """检查命令是否可用，返回 (found, path, info)。"""
    path = shutil.which(cmd)
    if not path:
        return False, None, None
    try:
        r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=10)
    except Exception as exc:
        return True, path, f"无法获取版本信息: {exc}"
    info = _first_line(r.stdout) or _first_line(r.stderr)
    if r.returncode != 0:
        info = (info + " " if info else "") + f"(exit {r.returncode})"
    return True, path, info


def describe_youtube_cookies() -> dict:
    """返回 yt_cookies.txt 的存在/大小/是否 Netscape 格式。"""
    if not YT_COOKIES.exists():
        return {"exists": False, "size": 0, "netscape": None}
    result = {"exists": True, "size": YT_COOKIES.stat().st_size, "netscape": None}
    try:
        from modules.tools.convert_cookies import is_netscape_file
        result["netscape"] = is_netscape_file(str(YT_COOKIES))
    except Exception:
        result["netscape"] = None
    return result


def _usable_youtube_cookies() -> Path | None:
    """返回可直接用于 yt-dlp 的 cookies 路径（必要时转换为临时 Netscape 文件）。"""
    if not YT_COOKIES.exists():
        return None
    try:
        from modules.tools.convert_cookies import convert_any_to_netscape
        converted = convert_any_to_netscape(str(YT_COOKIES))
        return Path(converted) if converted else None
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="检查 yt-dlp / ffmpeg / cookies 环境")
    parser.add_argument(
        "--use-cookies",
        action="store_true",
        help="用 yt_cookies.txt 对公开视频做一次轻量 yt-dlp 格式列举（仅当文件存在时）。",
    )
    args = parser.parse_args()

    report = {
        "yt-dlp": None,
        "ffmpeg": None,
        "yt_cookies": None,
        "bili_cookies": None,
        "yt_dlp_test": None,
    }

    ok, path, info = check_command("yt-dlp")
    report["yt-dlp"] = {"found": ok, "path": path, "info": info}
    okf, pathf, infof = check_command("ffmpeg")
    report["ffmpeg"] = {"found": okf, "path": pathf, "info": infof}

    report["yt_cookies"] = describe_youtube_cookies()
    report["bili_cookies"] = {
        "exists": BILI_COOKIES.exists(),
        "size": BILI_COOKIES.stat().st_size if BILI_COOKIES.exists() else 0,
    }

    # 仅当用户显式 --use-cookies 时才对外发起带 cookies 的轻量请求
    if args.use_cookies and report["yt-dlp"]["found"] and report["yt_cookies"]["exists"]:
        cookies = _usable_youtube_cookies()
        if cookies is None:
            report["yt_dlp_test"] = {"error": "cookies 文件无法转换为 yt-dlp 可用格式"}
        else:
            yt_dlp = report["yt-dlp"]["path"] or "yt-dlp"
            cmd = [
                yt_dlp, "--no-warnings", "--skip-download", "--no-playlist",
                "--print", "%(id)s\t%(title)s", _KNOWN_PUBLIC_VIDEO,
                "--cookies", str(cookies),
            ]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                report["yt_dlp_test"] = {
                    "returncode": r.returncode,
                    "stdout_first": (r.stdout or "").splitlines()[:5],
                    "stderr_first": (r.stderr or "").splitlines()[:10],
                }
            except Exception as exc:
                report["yt_dlp_test"] = {"error": str(exc)}

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
