#!/usr/bin/env python3
"""
批量生成带字幕烧录效果的预览 PNG，用于人工/AI 视觉审查。

用法示例::

    python tools/render_subtitle_preview.py \
        --texts "这是第一句示例字幕" "第二句稍长一点，用来测试换行效果" \
        --resolutions 1920x1080 3840x2160 1080x1920 \
        --font SourceHanSansHWSC-VF.otf \
        --outdir temp/subtitle_preview

说明:
- 输出文件命名包含分辨率、样本编号。
- 每行文本生成一张独立的 PNG，时间戳固定为 0.0 ~ 5.0。
- 默认启用电影级单行优先与最小字体缩放，与正式流程一致。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.task_manager import TaskProcessor  # noqa: E402


def parse_resolution(value: str) -> tuple[int, int]:
    w, h = value.lower().replace(" ", "").split("x")
    return int(w), int(h)


def render_sample(
    text: str,
    width: int,
    height: int,
    font_family: str,
    out_path: Path,
    prefer_single_line: bool,
    single_line_min_font_scale: float,
    ffmpeg_path: str = "ffmpeg",
) -> Path:
    cues = [
        {"start": 0.0, "end": 5.0, "text": text},
    ]
    ass_content = TaskProcessor._build_default_ass_document(
        cues,
        font_family=font_family,
        video_width=width,
        video_height=height,
        prefer_single_line=prefer_single_line,
        single_line_min_font_scale=single_line_min_font_scale,
    )

    # Write the ASS file next to the output image with a stable relative path
    # so FFmpeg's subtitles filter parses it reliably on Windows.
    ass_path = out_path.with_suffix(".ass")
    ass_path.write_text(ass_content, encoding="utf-8")
    relative_ass = os.path.relpath(ass_path, os.getcwd()).replace("\\", "/")

    try:
        cmd = [
            ffmpeg_path,
            "-y",
            "-f", "lavfi",
            "-i", f"color=c=black:s={width}x{height}:d=1",
            "-vf",
            f"subtitles={relative_ass}",
            "-frames:v", "1",
            "-q:v", "2",
            str(out_path),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    finally:
        try:
            if ass_path.exists():
                ass_path.unlink()
        except OSError:
            pass

    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Render subtitle preview PNGs.")
    parser.add_argument(
        "--texts",
        nargs="+",
        required=True,
        help="要渲染的字幕文本列表",
    )
    parser.add_argument(
        "--resolutions",
        nargs="+",
        default=["1920x1080", "3840x2160", "1080x1920"],
        help="目标分辨率，例如 1920x1080",
    )
    parser.add_argument(
        "--font",
        default="SourceHanSansHWSC-VF.otf",
        help="字体文件名（需存在于 ffmpeg/fonts/ 或系统字体目录）",
    )
    parser.add_argument(
        "--outdir",
        default="temp/subtitle_preview",
        help="输出目录",
    )
    parser.add_argument(
        "--prefer-single-line",
        type=lambda s: s.lower() in ("1", "true", "yes"),
        default=True,
        help="是否优先单行显示（默认 true）",
    )
    parser.add_argument(
        "--single-line-min-font-scale",
        type=float,
        default=0.85,
        help="单行放不下时最小缩放比例（默认 0.85）",
    )
    parser.add_argument(
        "--ffmpeg",
        default="ffmpeg",
        help="ffmpeg 可执行文件路径",
    )
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    generated: list[Path] = []
    for res in args.resolutions:
        width, height = parse_resolution(res)
        for idx, text in enumerate(args.texts, start=1):
            safe_text = (
                "".join(c if c.isprintable() and c not in '\\/:*?"<>|' else "_" for c in text[:20])
                .strip()
            )
            short_hash = uuid.uuid4().hex[:8]
            out_name = f"preview_{width}x{height}_{idx:02d}_{short_hash}_{safe_text}.png"
            out_path = outdir / out_name
            render_sample(
                text=text,
                width=width,
                height=height,
                font_family=args.font,
                out_path=out_path,
                prefer_single_line=args.prefer_single_line,
                single_line_min_font_scale=args.single_line_min_font_scale,
                ffmpeg_path=args.ffmpeg,
            )
            generated.append(out_path)
            print(f"Rendered {out_path}")

    print(f"\nGenerated {len(generated)} preview image(s) in {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
