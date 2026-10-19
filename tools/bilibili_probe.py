#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
bilibili 投稿通道连通性探测（本地专用）

分阶段验证，风险从 0 递增：
  qr        扫码登录，把 Cookie 写入本地文件（不打印任何凭据）
  auth      只读：验证登录态
  preupload 只申请上传凭证，不上传文件、不投稿
  submit    真实投稿（默认「仅自己可见」），需要显式 --confirm

设计约束：
  * 任何阶段都不打印 SESSDATA / bili_jct / buvid / access_token；
  * submit 默认 is_only_self=1，且必须显式传 --confirm；
  * 失败信息只保留 code 与 message。

用法示例：
  python tools/bilibili_probe.py --stage qr
  python tools/bilibili_probe.py --stage auth
  python tools/bilibili_probe.py --stage preupload
  python tools/bilibili_probe.py --stage submit --api bcut_pc --video test.mp4 \
      --cover cover.jpg --tid 21 --title "接口连通性测试" --confirm

测试稿件请在创作中心「稿件管理」手动删除：删除接口尚未验证，本工具不做自动删除。
"""

import argparse
import asyncio
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from modules.bili_sdk import login_v2  # noqa: E402
from modules.bili_sdk.exceptions import (  # noqa: E402
    NetworkException,
    ResponseCodeException,
)
from modules.bili_sdk.utils.network import Api, Credential, get_api  # noqa: E402
from modules.bili_sdk.video_uploader import (  # noqa: E402
    VideoUploader,
    VideoUploaderPage,
    normalize_submit_api,
)
from modules.bilibili_auth import (  # noqa: E402
    load_credential_from_file,
    save_credential_to_file,
    validate_credential_remote,
)
from modules.bilibili_runtime import configure_bilibili_runtime  # noqa: E402

DEFAULT_COOKIE = os.path.join(ROOT, "cookies", "bili_cookies.json")

# 必剪 PC 端 preupload 参数（参考 @renmu/bili-api bcutParams）
BCUT_PREUPLOAD_BUILD = "22559864"
BCUT_PREUPLOAD_VERSION = "3.11.20"
BCUT_PREUPLOAD_PROFILE = "ugcfx/bcut-pc"

PREUPLOAD_URL = "https://member.bilibili.com/preupload"


def _mask(value, keep=2):
    text = str(value or "")
    if not text:
        return "<empty>"
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}***{text[-keep:]}"


def _err_text(err: Exception) -> str:
    if isinstance(err, ResponseCodeException):
        return f"code={err.code} msg={err.msg}"
    if isinstance(err, NetworkException):
        return f"HTTP {getattr(err, 'status', '?')}"
    return f"{type(err).__name__}: {err}"


def _client():
    from modules.bili_sdk.utils.network import get_client

    return get_client()


async def _nav(credential: Credential) -> dict:
    api = get_api("credential")["info"]["valid"]
    return await Api(**api, credential=credential).result


# --------------------------------------------------------------------------- qr
async def stage_qr(cookie_path: str, timeout: int, retries: int = 4) -> int:
    configure_bilibili_runtime()
    qr_path = os.path.join(ROOT, "qrcode.png")

    for attempt in range(1, retries + 1):
        login = login_v2.QrCodeLogin()
        await login.generate_qrcode()
        picture = login.get_qrcode_picture()
        with open(qr_path, "wb") as stream:
            stream.write(picture.content)

        print(f"[qr] 第 {attempt}/{retries} 张二维码已保存: {qr_path}")
        print("[qr] 请用 B 站手机 App 扫码，并在手机上确认登录")
        print(f"[qr] 本张有效期约 {timeout} 秒，过期会自动换新（同一文件路径）")

        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                state = await login.check_state()
            except Exception as exc:  # 网络抖动不致命
                await asyncio.sleep(2)
                last = _err_text(exc)
                continue
            if state != last:
                print(f"[qr] 状态: {state.value}")
                last = state
            if state == login_v2.QrCodeLoginEvents.DONE:
                credential = login.get_credential()
                if not save_credential_to_file(credential, cookie_path):
                    print("[qr] Cookie 保存失败")
                    return 1
                print(f"[qr] 登录成功，Cookie 已写入: {cookie_path}")
                return 0
            if state == login_v2.QrCodeLoginEvents.TIMEOUT:
                print("[qr] 本张二维码已过期，换新…")
                break
            await asyncio.sleep(2)

    print("[qr] 等待超时，请重新执行本命令")
    return 2


# ------------------------------------------------------------------------- auth
async def stage_auth(cookie_path: str) -> int:
    configure_bilibili_runtime()
    credential = load_credential_from_file(cookie_path)
    ok, message = validate_credential_remote(credential)
    print(f"[auth] 登录态: {'有效' if ok else '无效'} ({message})")
    if not ok:
        return 1
    try:
        nav = await _nav(credential)
    except Exception as exc:
        print(f"[auth] nav 读取失败: {_err_text(exc)}")
        return 1
    print(f"[auth] mid={_mask(nav.get('mid'))} uname={_mask(nav.get('uname'), 1)} level={nav.get('level_info', {}).get('current_level')}")
    print(f"[auth] buvid3={'有' if credential.has_buvid3() else '无'} buvid4={'有' if credential.has_buvid4() else '无'} bili_jct={'有' if credential.has_bili_jct() else '无'}")
    return 0


# -------------------------------------------------------------------- preupload
async def stage_preupload(credential: Credential, size: int = 1_048_576) -> int:
    configure_bilibili_runtime()
    from modules.bili_sdk.utils.network import HEADERS

    scenarios = [
        (
            "web (ugcfx/bup)",
            {
                "profile": "ugcfx/bup",
                "name": "probe.mp4",
                "size": size,
                "r": "upos",
                "ssl": 0,
                "version": "2.14.0",
                "build": "2100400",
                "upcdn": "bda2",
                "probe_version": "20211012",
            },
            HEADERS.copy(),
        ),
        (
            "bcut (ugcfx/bcut-pc)",
            {
                "profile": BCUT_PREUPLOAD_PROFILE,
                "name": "probe.mp4",
                "size": size,
                "r": "upos",
                "upcdn": "bda2",
                "build": BCUT_PREUPLOAD_BUILD,
                "version": BCUT_PREUPLOAD_VERSION,
            },
            {"User-Agent": f"bcut-pc build:{BCUT_PREUPLOAD_BUILD}"},
        ),
    ]

    client = _client()
    failures = 0
    for label, params, headers in scenarios:
        try:
            resp = await client.request(
                method="GET",
                url=PREUPLOAD_URL,
                params=params,
                cookies=await credential.get_buvid_cookies(),
                headers=headers,
            )
        except Exception as exc:
            print(f"[preupload] {label}: 请求异常 {_err_text(exc)}")
            failures += 1
            continue
        if resp.code != 200:
            print(f"[preupload] {label}: HTTP {resp.code}")
            failures += 1
            continue
        data = resp.json()
        ok = data.get("OK")
        endpoint = str(data.get("endpoint") or "")
        host = endpoint.split("//")[-1].split("/")[0] if endpoint else ""
        print(
            f"[preupload] {label}: OK={ok} biz_id={'有' if data.get('biz_id') else '无'} "
            f"chunk={data.get('chunk_size')} endpoint={host or '<空>'}"
        )
        if ok != 1:
            print(f"[preupload] {label}: 返回体 {json.dumps(data, ensure_ascii=False)[:300]}")
            failures += 1
    return 0 if failures == 0 else 1


# ----------------------------------------------------------------------- submit
def _build_meta(args) -> dict:
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    meta = {
        "title": args.title,
        "copyright": 2 if args.reprint else 1,
        "tid": int(args.tid),
        "tag": ",".join(tags),
        "desc_format_id": 9999,
        "desc": args.desc or "",
        "recreate": -1,
        "dynamic": "",
        "interactive": 0,
        "act_reserve_create": 0,
        "no_disturbance": 0,
        "no_reprint": 1,
        "subtitle": {"open": 0, "lan": ""},
        "dolby": 0,
        "lossless_music": 0,
        "up_selection_reply": False,
        "up_close_reply": False,
        "up_close_danmu": False,
        "web_os": 1,
    }
    if args.reprint:
        meta["source"] = args.source or "https://www.bilibili.com"
    if not args.public:
        # 仅自己可见，避免测试稿件出现在公开列表
        meta["is_only_self"] = 1
    return meta


async def stage_submit(args, credential: Credential) -> int:
    configure_bilibili_runtime()
    if not os.path.exists(args.video):
        print(f"[submit] 视频不存在: {args.video}")
        return 1
    if not os.path.exists(args.cover):
        print(f"[submit] 封面不存在: {args.cover}")
        return 1

    meta = _build_meta(args)
    page = VideoUploaderPage(path=args.video, title=args.title)
    uploader = VideoUploader(
        pages=[page],
        meta=meta,
        credential=credential,
        cover=args.cover,
        submit_api=args.api,
    )

    @uploader.on("SUBMIT_FALLBACK")
    def _on_fallback(data):
        print(
            f"[submit] 回退: {data.get('from')} -> {data.get('to')} "
            f"(code={data.get('code')} risk={data.get('risk')})"
        )

    @uploader.on("AFTER_SUBMIT")
    def _on_after(_data):
        print(f"[submit] 投稿接口: {uploader.submit_api_used}")

    if not args.public:
        print("[submit] 模式: 仅自己可见（is_only_self=1）")
    else:
        print("[submit] 模式: 公开投稿（--public）")

    try:
        result = await uploader.start()
    except Exception as exc:
        print(f"[submit] 失败: {_err_text(exc)}")
        if isinstance(exc, ResponseCodeException) and exc.code == 603:
            print("[submit] 603 = 账号级风控，本次测试请立即停止，不要重复重试")
        return 1

    if not isinstance(result, dict):
        print(f"[submit] 返回格式异常: {result}")
        return 1
    bvid = result.get("bvid") or (result.get("data") or {}).get("bvid")
    aid = result.get("aid") or (result.get("data") or {}).get("aid")
    if bvid:
        print(
            f"[submit] 成功 bvid={bvid} aid={aid} "
            f"url=https://www.bilibili.com/video/{bvid}"
        )
    else:
        print(f"[submit] 成功，但返回里没有 bvid（aid={aid}）: {result}")
    print("[submit] 测试稿件请到创作中心「稿件管理」手动删除（删除接口未验证，不做自动删除）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="bilibili 投稿通道探测")
    parser.add_argument(
        "--stage",
        required=True,
        choices=["qr", "auth", "preupload", "submit"],
    )
    parser.add_argument("--cookie", default=DEFAULT_COOKIE)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--api", default="bcut_pc", choices=["auto", "web", "bcut_pc"])
    parser.add_argument("--video")
    parser.add_argument("--cover")
    parser.add_argument("--title", default="接口连通性测试")
    parser.add_argument("--desc", default="本地接口连通性测试，稍后删除。")
    parser.add_argument("--tags", default="测试")
    parser.add_argument("--tid", default="21")
    parser.add_argument("--public", action="store_true", help="公开投稿（默认仅自己可见）")
    parser.add_argument(
        "--reprint", action="store_true", help="按转载投稿（copyright=2），需配合 --source"
    )
    parser.add_argument("--source", default="", help="转载来源 URL")
    parser.add_argument("--confirm", action="store_true", help="submit 阶段必须显式确认")
    args = parser.parse_args()

    if args.stage == "qr":
        return asyncio.run(stage_qr(args.cookie, args.timeout))

    if not os.path.exists(args.cookie):
        print(f"Cookie 文件不存在: {args.cookie}，请先执行 --stage qr")
        return 1

    credential = load_credential_from_file(args.cookie)

    if args.stage == "auth":
        return asyncio.run(stage_auth(args.cookie))
    if args.stage == "preupload":
        return asyncio.run(stage_preupload(credential))

    if not args.confirm:
        print("[submit] 真实投稿会消耗一次投稿配额，请加 --confirm 再执行")
        return 1
    if not args.video or not args.cover:
        print("[submit] 需要 --video 与 --cover")
        return 1
    args.api = normalize_submit_api(args.api)
    return asyncio.run(stage_submit(args, credential))


if __name__ == "__main__":
    raise SystemExit(main())
