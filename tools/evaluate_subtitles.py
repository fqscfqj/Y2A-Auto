#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import json
import sys
from pathlib import Path
from statistics import mean
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.srt_transform_engine import SrtTransformEngine, SrtTransformConfig


def _load_cues(path: Path) -> List[Dict[str, object]]:
    with open(path, "r", encoding="utf-8", errors="replace") as file_obj:
        content = file_obj.read()
    return SrtTransformEngine(SrtTransformConfig()).parse_srt(content)


def _boundary_error(reference: List[Dict[str, object]], generated: List[Dict[str, object]]) -> float:
    if not reference or not generated:
        return 0.0
    count = min(len(reference), len(generated))
    errors = []
    for index in range(count):
        errors.append(abs(float(reference[index]["start"]) - float(generated[index]["start"])))
        errors.append(abs(float(reference[index]["end"]) - float(generated[index]["end"])))
    return mean(errors) if errors else 0.0


def _speech_coverage(cues: List[Dict[str, object]], media_duration_s: float) -> float:
    if media_duration_s <= 0:
        return 0.0
    speech_s = sum(max(0.0, float(cue["end"]) - float(cue["start"])) for cue in cues)
    return speech_s / media_duration_s


def _duplicate_rate(cues: List[Dict[str, object]]) -> float:
    if not cues:
        return 0.0
    normalized = [" ".join(str(cue.get("text") or "").strip().lower().split()) for cue in cues]
    duplicates = 0
    previous = None
    for item in normalized:
        if item and item == previous:
            duplicates += 1
        previous = item
    return duplicates / len(cues)


def evaluate_case(case_dir: Path) -> Dict[str, object]:
    generated_path = case_dir / "generated.srt"
    reference_path = case_dir / "reference.srt"
    metadata_path = case_dir / "metadata.json"

    generated = _load_cues(generated_path)
    reference = _load_cues(reference_path)
    metadata = {}
    if metadata_path.exists():
        with open(metadata_path, "r", encoding="utf-8") as file_obj:
            metadata = json.load(file_obj)

    media_duration_s = float(metadata.get("media_duration_s", 0.0) or 0.0)
    fallback_tokens = metadata.get("fallback_tokens", []) or []

    return {
        "case": case_dir.name,
        "generated_cues": len(generated),
        "reference_cues": len(reference),
        "boundary_error_s": round(_boundary_error(reference, generated), 4),
        "speech_coverage": round(_speech_coverage(generated, media_duration_s), 4),
        "duplicate_rate": round(_duplicate_rate(generated), 4),
        "fallback_frequency": round(len(fallback_tokens) / max(1, len(generated)), 4),
        "fallback_tokens": fallback_tokens,
    }


def evaluate_pair(
    generated_path: Path,
    reference_path: Path,
    *,
    media_duration_s: float = 0.0,
    case_name: str = "ad_hoc",
) -> Dict[str, object]:
    generated = _load_cues(generated_path)
    reference = _load_cues(reference_path)
    return {
        "case": case_name,
        "generated_cues": len(generated),
        "reference_cues": len(reference),
        "boundary_error_s": round(_boundary_error(reference, generated), 4),
        "speech_coverage": round(_speech_coverage(generated, media_duration_s), 4),
        "duplicate_rate": round(_duplicate_rate(generated), 4),
        "fallback_frequency": 0.0,
        "fallback_tokens": [],
    }


def main():
    parser = argparse.ArgumentParser(description="Evaluate generated subtitles against reference SRT files.")
    parser.add_argument("cases_root", nargs="?", help="Directory containing per-case folders with generated.srt/reference.srt")
    parser.add_argument("--generated", help="Single generated subtitle file path")
    parser.add_argument("--reference", help="Single reference subtitle file path")
    parser.add_argument("--media-duration", type=float, default=0.0, help="Optional media duration in seconds for single-pair mode")
    parser.add_argument("--output", help="Optional JSON output path")
    args = parser.parse_args()

    reports = []
    if args.generated and args.reference:
        generated_path = Path(args.generated).resolve()
        reference_path = Path(args.reference).resolve()
        reports.append(
            evaluate_pair(
                generated_path,
                reference_path,
                media_duration_s=float(args.media_duration or 0.0),
                case_name=generated_path.stem,
            )
        )
    else:
        if not args.cases_root:
            parser.error("cases_root is required unless --generated and --reference are provided")
        cases_root = Path(args.cases_root).resolve()
        for case_dir in sorted(path for path in cases_root.iterdir() if path.is_dir()):
            generated_path = case_dir / "generated.srt"
            reference_path = case_dir / "reference.srt"
            if generated_path.exists() and reference_path.exists():
                reports.append(evaluate_case(case_dir))

    summary = {
        "cases": reports,
        "mean_boundary_error_s": round(mean([item["boundary_error_s"] for item in reports]), 4) if reports else 0.0,
        "mean_speech_coverage": round(mean([item["speech_coverage"] for item in reports]), 4) if reports else 0.0,
        "mean_duplicate_rate": round(mean([item["duplicate_rate"] for item in reports]), 4) if reports else 0.0,
        "mean_fallback_frequency": round(mean([item["fallback_frequency"] for item in reports]), 4) if reports else 0.0,
    }

    payload = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
