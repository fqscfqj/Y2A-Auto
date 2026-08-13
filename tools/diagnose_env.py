#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
快速诊断脚本：检查 yt-dlp / ffmpeg 可用性、cookies 文件存在与基本格式、并尝试用 yt-dlp 做一次格式列举。
在项目根运行： python tools/diagnose_env.py
默认不会在没有明确 --use-cookies 的情况下对外发起带 cookies 的网络请求以避免触发反爬。
"""
import os
import shutil
import subprocess
import json
from pathlib import Path
import argparse

ROOT = Path(".").resolve()
COOKIES_DIR = ROOT / "cookies"
YT_COOKIES = COOKIES_DIR / "yt_cookies.txt"
BILI_COOKIES = COOKIES_DIR / "bili_cookies.json"


def check_command(cmd):
    path = shutil.which(cmd)
    if path:
        try:
            r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=10)
            return True, path, (r.stdout or r.stderr).strip().splitlines()[0][:200]
        except Exception as e:
            return True, path, f"无法获取版本信息: {e}"
    return False, None, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--use-cookies", action="store_true", help="If set, run the gentle yt-dlp format test using the yt_cookies.txt file (only when present).")
    args = parser.parse_args()

    report = {"yt-dlp": None, "ffmpeg": None, "yt_cookies": None, "bili_cookies": None, "yt_dlp_test": None}
    ok, path, info = check_command("yt-dlp")
    report["yt-dlp"] = {"found": ok, "path": path, "info": info}
    okf, pathf, infof = check_command("ffmpeg")
    report["ffmpeg"] = {"found": okf, "path": pathf, "info": infof}

    report["yt_cookies"] = {"exists": YT_COOKIES.exists(), "size": YT_COOKIES.stat().st_size if YT_COOKIES.exists() else 0}
    report["bili_cookies"] = {"exists": BILI_COOKIES.exists(), "size": BILI_COOKIES.stat().st_size if BILI_COOKIES.exists() else 0}

    # Only run the gentle online test when user explicitly requests using --use-cookies
    if args.use_cookies and report["yt-dlp"]["found"] and report["yt_cookies"]["exists"]:
        test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"  # known public video
        try:
            cmd = ["yt-dlp", "--no-warnings", "--skip-download", "--print", "%(id)s\t%(title)s", test_url, "--cookies", str(YT_COOKIES)]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            report["yt_dlp_test"] = {"returncode": r.returncode, "stdout_first": (r.stdout or "").splitlines()[:5], "stderr_first": (r.stderr or "").splitlines()[:10]}
        except Exception as e:
            report["yt_dlp_test"] = {"error": str(e)}

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
