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
    def test_landscape_ass_style_uses_clear_bottom_safe_area(self):
        style = TaskProcessor._build_streaming_ass_style(1920, 1080)

        self.assertEqual(style['PlayResX'], 1920)
        self.assertEqual(style['PlayResY'], 1080)
        self.assertGreaterEqual(style['MarginV'], 50.0)
        self.assertLessEqual(style['MarginL'], 70.0)
        self.assertLessEqual(style['MarginR'], 70.0)
        self.assertGreaterEqual(style['FontSize'], 68.0)
        self.assertGreaterEqual(style['Outline'], 1.9)
        self.assertEqual(style['Alignment'], 2)

    def test_landscape_layout_uses_wider_lines(self):
        max_line_length, max_lines = TaskProcessor._estimate_subtitle_layout_limits(1920, 1080)

        self.assertEqual(max_lines, 2)
        self.assertGreaterEqual(max_line_length, 24)

    def test_portrait_ass_style_keeps_higher_vertical_margin(self):
        style = TaskProcessor._build_streaming_ass_style(1080, 1920)

        self.assertEqual(style['PlayResX'], 1080)
        self.assertEqual(style['PlayResY'], 1920)
        self.assertGreaterEqual(style['MarginV'], 150.0)
        self.assertGreaterEqual(style['Outline'], 2.0)

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


if __name__ == '__main__':
    unittest.main()
