import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.subtitle_qc import _read_srt_items, _rule_check, _sample_items, run_subtitle_qc


def _write_srt(entries):
    tmp_dir = tempfile.TemporaryDirectory()
    path = Path(tmp_dir.name) / "sample.srt"
    blocks = []
    for index, (start_time, end_time, text) in enumerate(entries, start=1):
        blocks.append(f"{index}\n{start_time} --> {end_time}\n{text}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")
    return tmp_dir, path


class SubtitleQCTestCase(unittest.TestCase):
    def test_ultra_short_repeat_fails_at_rule_layer(self):
        tmp_dir, srt_path = _write_srt([
            ("00:01:48,700", "00:01:50,000", "Thank you."),
            ("00:03:39,200", "00:03:41,000", "Thank you."),
            ("00:08:32,900", "00:08:34,200", "Thank you."),
        ])
        self.addCleanup(tmp_dir.cleanup)

        result = run_subtitle_qc(str(srt_path), {"SUBTITLE_QC_PROVIDER": "none"})

        self.assertFalse(result.passed)
        self.assertEqual(result.decision, "rule_fail")
        self.assertEqual(result.reason, "rule_fail:ultra_short_repeat")

    def test_short_distinct_lines_are_not_hard_failed(self):
        tmp_dir, srt_path = _write_srt([
            ("00:00:01,000", "00:00:02,000", "Hello."),
            ("00:00:03,000", "00:00:04,000", "Thanks."),
            ("00:00:05,000", "00:00:06,000", "Goodbye."),
        ])
        self.addCleanup(tmp_dir.cleanup)

        result = run_subtitle_qc(str(srt_path), {"SUBTITLE_QC_PROVIDER": "none"})

        self.assertEqual(result.decision, "needs_ai")
        self.assertFalse(result.reason.startswith("rule_fail:"))

    def test_suspicious_sampling_keeps_repeated_lines(self):
        tmp_dir, srt_path = _write_srt([
            ("00:00:01,000", "00:00:02,000", "Hello."),
            ("00:00:10,000", "00:00:11,000", "Thanks."),
            ("00:00:20,000", "00:00:21,000", "Hello."),
            ("00:00:30,000", "00:00:31,000", "Okay."),
            ("00:00:40,000", "00:00:41,000", "Hello."),
            ("00:00:50,000", "00:00:51,000", "Sure."),
        ])
        self.addCleanup(tmp_dir.cleanup)

        items = _read_srt_items(str(srt_path))
        rule_result = _rule_check(items)
        item_stats = list(rule_result.metrics.get("item_stats") or [])
        sample_text, sample_meta = _sample_items(
            items,
            item_stats=item_stats,
            max_items=80,
            max_chars=9000,
            boundary_level=rule_result.boundary_level,
        )

        self.assertEqual(rule_result.decision, "needs_ai")
        self.assertGreaterEqual(sample_text.count("Hello."), 3)
        self.assertEqual(rule_result.metrics.get("top_repeated_texts"), [{"text": "Hello.", "count": 3}])
        self.assertGreaterEqual(sample_meta.get("sample_items", 0), 3)

    def test_final_score_is_capped_by_rule_score(self):
        tmp_dir, srt_path = _write_srt([
            ("00:00:01,000", "00:00:02,000", "Hello."),
            ("00:00:10,000", "00:00:11,000", "Thanks."),
            ("00:00:20,000", "00:00:21,000", "Hello."),
            ("00:00:30,000", "00:00:31,000", "Okay."),
            ("00:00:40,000", "00:00:41,000", "Hello."),
            ("00:00:50,000", "00:00:51,000", "Sure."),
        ])
        self.addCleanup(tmp_dir.cleanup)

        with patch(
            "modules.subtitle_qc._call_ai_judge",
            return_value=(True, 0.95, {"reason": "healthy_dialogue"}, "ok"),
        ):
            result = run_subtitle_qc(
                str(srt_path),
                {
                    "SUBTITLE_QC_PROVIDER": "openai",
                    "SUBTITLE_QC_THRESHOLD": 0.60,
                },
            )

        self.assertFalse(result.passed)
        self.assertEqual(result.decision, "needs_ai")
        self.assertAlmostEqual(result.score, result.rule_score, places=6)
        self.assertEqual(result.ai_score, 0.95)
        self.assertEqual(result.reason, "ai_fail:healthy_dialogue")


if __name__ == "__main__":
    unittest.main()
