import ast
import logging
import os
import pathlib
import unicodedata
import unittest


def _load_task_processor_class():
    module_path = pathlib.Path(__file__).resolve().parents[1] / 'modules' / 'task_manager.py'
    source = module_path.read_text(encoding='utf-8')
    module_ast = ast.parse(source, filename=str(module_path))
    selected = [
        node for node in module_ast.body
        if isinstance(node, ast.ClassDef) and node.name == 'TaskProcessor'
    ]
    isolated_module = ast.Module(body=selected, type_ignores=[])
    namespace = {
        'os': os,
        'unicodedata': unicodedata,
        'logger': logging.getLogger('test_task_processor_layout'),
    }
    exec(compile(isolated_module, str(module_path), 'exec'), namespace)
    return namespace['TaskProcessor']


TaskProcessor = _load_task_processor_class()


class SubtitleRenderingLayoutTests(unittest.TestCase):
    @staticmethod
    def _extract_ass_lines(text):
        normalized = str(text or '')
        if normalized.startswith(r'{\fs'):
            closing_index = normalized.find('}')
            if closing_index >= 0:
                normalized = normalized[closing_index + 1:]
        return [line for line in normalized.split(r'\N') if line]

    def test_landscape_ass_style_uses_clear_bottom_safe_area(self):
        style = TaskProcessor._build_streaming_ass_style(1920, 1080)

        self.assertEqual(style['PlayResX'], 1920)
        self.assertEqual(style['PlayResY'], 1080)
        self.assertGreaterEqual(style['MarginV'], 68.0)
        self.assertGreaterEqual(style['MarginL'], 96.0)
        self.assertGreaterEqual(style['MarginR'], 96.0)
        self.assertGreaterEqual(style['FontSize'], 66.0)
        self.assertGreaterEqual(style['Outline'], 2.0)
        self.assertEqual(style['Alignment'], 2)

    def test_landscape_layout_uses_wider_lines(self):
        max_line_length, max_lines = TaskProcessor._estimate_subtitle_layout_limits(1920, 1080)

        self.assertEqual(max_lines, 2)
        self.assertGreaterEqual(max_line_length, 22)

    def test_portrait_ass_style_keeps_higher_vertical_margin(self):
        style = TaskProcessor._build_streaming_ass_style(1080, 1920)

        self.assertEqual(style['PlayResX'], 1080)
        self.assertEqual(style['PlayResY'], 1920)
        self.assertGreaterEqual(style['MarginV'], 200.0)
        self.assertGreaterEqual(style['FontSize'], 66.0)
        self.assertGreaterEqual(style['Outline'], 2.0)

    def test_portrait_layout_uses_fewer_lines_and_stays_safe(self):
        max_line_length, max_lines = TaskProcessor._estimate_subtitle_layout_limits(1080, 1920)
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '竖屏字幕不应过宽或压得太低，否则会与互动区、底部贴纸发生冲突。',
            1080,
            1920,
            return_meta=True,
        )

        self.assertEqual(max_lines, 5)
        self.assertLessEqual(text.count(r'\N') + 1, 5)
        self.assertFalse(meta.get('overflow_warning'))

    def test_wrap_subtitle_text_for_ass_balances_long_cjk_text(self):
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '这是一个用于验证字幕换行能力的很长中文句子，需要保持底部居中显示并且不能溢出画面。',
            1920,
            1080,
            return_meta=True,
        )

        self.assertTrue(text)
        self.assertIn(r'\N', text)
        self.assertFalse(meta.get('overflow_warning'))

    def test_long_landscape_wrap_avoids_breaking_common_phrases(self):
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '这是一条用于测试超长字幕烧录样式的中文文案，需要在保证商业观感的前提下，兼顾信息完整、停顿自然、底部安全区充足以及整体版式的呼吸感。',
            1920,
            1080,
            return_meta=True,
        )

        self.assertTrue(text)
        self.assertNotIn('前\\N提', text)
        self.assertFalse(meta.get('overflow_warning'))

    def test_mixed_language_wrap_keeps_latin_words_intact(self):
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '如果一句字幕里同时出现 RTX 5090、YouTube Shorts 和 AI workflow，这种中英混排也要保持节奏稳定、不要切碎英文词组。',
            1920,
            1080,
            return_meta=True,
        )

        normalized = text.replace(r'\N', '')
        lines = self._extract_ass_lines(text)
        self.assertIn('workflow', normalized)
        self.assertNotIn('w\\Norkflow', text)
        self.assertNotIn('You\\NTube', text)
        self.assertLessEqual(len(lines), 4)
        self.assertFalse(meta.get('overflow_warning'))

    def test_wrap_avoids_splitting_cjk_run_mid_char(self):
        """CJK text should prefer breaking at punctuation/script boundaries
        over splitting between two consecutive CJK characters."""
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '这是我在真实PlayStation硬件上运行的自制《天际》演示版，需要保持硬件这个词完整，不能拆得七零八落。',
            1920,
            1080,
            return_meta=True,
        )

        self.assertTrue(text)
        lines = text.split('\\N')
        for line in lines:
            cjk_count = sum(1 for c in line if TaskProcessor._is_cjk_like_char(c))
            self.assertGreater(
                cjk_count, 1,
                f'Line "{line}" contains only {cjk_count} CJK char(s) — likely a broken compound',
            )
        self.assertFalse(meta.get('overflow_warning'))

    def test_portrait_mixed_language_wrap_stays_balanced(self):
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '在 1080×1920 的竖屏里，Release notes、workflow status 这类英文短语也应该完整保留，避免断开后显得很廉价。',
            1080,
            1920,
            return_meta=True,
        )

        lines = self._extract_ass_lines(text)
        self.assertTrue(text)
        self.assertGreaterEqual(len(lines), 4)
        self.assertLessEqual(len(lines), 5)
        normalized = text.replace(r'\N', '')
        self.assertIn('Release', normalized)
        self.assertIn('notes', normalized)
        self.assertIn('workflow', normalized)
        self.assertIn('status', normalized)
        self.assertFalse(any(line[:1] in '，。！？；：、)]}】）》」』' for line in lines if line))
        self.assertFalse(meta.get('overflow_warning'))

    def test_portrait_long_wrap_prefers_fewer_balanced_lines(self):
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '竖屏字幕要避开互动区和底部贴纸，长句往上收，避免视觉重心过低。',
            1080,
            1920,
            return_meta=True,
        )

        lines = self._extract_ass_lines(text)
        self.assertTrue(text)
        self.assertLessEqual(len(lines), 5)
        self.assertFalse(any(line[:1] in '，。！？；：、)]}】）》」』' for line in lines if line))
        self.assertFalse(meta.get('overflow_warning'))

    def test_single_line_priority_keeps_short_text_on_one_line(self):
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '短句应单行显示',
            1920,
            1080,
            return_meta=True,
        )

        self.assertTrue(text)
        self.assertNotIn(r'\N', text)
        self.assertFalse(meta.get('forced_wrap'))

    def test_single_line_priority_scales_font_before_wrapping(self):
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '这是一句中等长度的中文测试字幕，在横屏下默认字体可能一行放不下，但缩小字体后可以保持单行。',
            1920,
            1080,
            return_meta=True,
            prefer_single_line=True,
            single_line_min_font_scale=0.85,
        )

        self.assertTrue(text)
        if r'\N' not in text:
            self.assertIsNotNone(meta.get('font_override'))
            self.assertGreater(meta['font_override'], 0)
            self.assertLess(meta['font_override'], 100)
        else:
            self.assertFalse(meta.get('overflow_warning'))

    def test_single_line_disabled_allows_wrap(self):
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '这是一句中等长度的中文测试字幕，在横屏下默认字体一行放不下，禁用单行优先后应立即换行。',
            1920,
            1080,
            return_meta=True,
            prefer_single_line=False,
        )

        self.assertTrue(text)
        self.assertIn(r'\N', text)
        self.assertIsNone(meta.get('font_override'))
        self.assertFalse(meta.get('overflow_warning'))


if __name__ == '__main__':
    unittest.main()
