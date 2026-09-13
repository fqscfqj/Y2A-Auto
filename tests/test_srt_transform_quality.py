"""字幕后处理质量回归测试。

覆盖 srt_transform_engine 的 9 项审计修复：
真实换行（含 CJK 禁则）、合并时长/字速上限、跨窗去重、
极短 cue 兜底、filler 位置约束、幻觉规则补全、CJK 拼接。
"""

import unittest

from modules.srt_transform_engine import (
    SrtTransformConfig,
    SrtTransformEngine,
    _join_texts,
)
from modules.subtitle_pipeline_types import AlignedSubtitleCue

_NO_LINE_END_CHARS = '「『（【〔［｛“‘'
_NO_LINE_START_CHARS = '、。，！？；：）」』】〕］｝”’·ー～…'


def _aligned(start_s, end_s, text, window_index, confidence=0.5):
    return AlignedSubtitleCue(
        start_s=start_s,
        end_s=end_s,
        text=text,
        source_window_index=window_index,
        alignment_confidence=confidence,
    )


class WrapTextTests(unittest.TestCase):
    def test_cjk_wrap_respects_line_start_and_line_end_kinsoku(self):
        engine = SrtTransformEngine(SrtTransformConfig(max_line_length=4, max_lines=3))
        source = '字幕测试，需要换行'

        wrapped = engine.wrap_text(source)
        lines = wrapped.split('\n')

        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertNotIn(line[0], _NO_LINE_START_CHARS, f'行首禁则被破坏: {line!r}')
            self.assertNotIn(line[-1], _NO_LINE_END_CHARS, f'行末禁则被破坏: {line!r}')
        self.assertEqual(wrapped.replace('\n', ''), source)

    def test_cjk_wrap_keeps_opening_bracket_off_line_end(self):
        engine = SrtTransformEngine(SrtTransformConfig(max_line_length=3, max_lines=4))
        source = '标题「重要」内容'

        wrapped = engine.wrap_text(source)
        lines = wrapped.split('\n')

        for line in lines:
            self.assertNotIn(line[-1], _NO_LINE_END_CHARS, f'行末禁则被破坏: {line!r}')
            self.assertNotIn(line[0], _NO_LINE_START_CHARS, f'行首禁则被破坏: {line!r}')
        self.assertEqual(wrapped.replace('\n', ''), source)

    def test_latin_wrap_never_splits_words(self):
        engine = SrtTransformEngine(SrtTransformConfig(max_line_length=20, max_lines=4))
        source = 'the quick brown fox jumps over the lazy dog'

        wrapped = engine.wrap_text(source)
        lines = wrapped.split('\n')

        self.assertGreater(len(lines), 1)
        for line in lines:
            for word in line.split(' '):
                self.assertIn(word, source.split(' '), f'单词被切断: {word!r}')

    def test_wrap_text_returns_input_unchanged_when_disabled(self):
        # task_manager 用 (999, 99) 构造引擎并要求「不要在这里切分传入的 SRT cue」。
        engine = SrtTransformEngine(SrtTransformConfig(max_line_length=999, max_lines=99))
        long_text = '这是一句非常长的中文字幕文本' * 6

        self.assertEqual(engine.wrap_text(long_text), long_text)
        self.assertEqual(engine.wrap_text('第一行\n第二行'), '第一行\n第二行')

        engine_no_lines = SrtTransformEngine(SrtTransformConfig(max_line_length=10, max_lines=0))
        self.assertEqual(engine_no_lines.wrap_text(long_text), long_text)

    def test_wrap_text_never_loses_characters(self):
        engine = SrtTransformEngine(SrtTransformConfig(max_line_length=8, max_lines=2))
        source = '这是一段很长的中文文本需要被折行但绝对不能丢字'

        wrapped = engine.wrap_text(source)
        lines = wrapped.split('\n')

        self.assertEqual(len(lines), 2)
        self.assertEqual(wrapped.replace('\n', ''), source)

    def test_mixed_language_wrap_keeps_latin_tokens_intact(self):
        engine = SrtTransformEngine(SrtTransformConfig(max_line_length=12, max_lines=3))
        source = '如果一句字幕里同时出现 RTX 5090 和 YouTube Shorts，这种中英混排也要稳定。'

        wrapped = engine.wrap_text(source)
        lines = wrapped.split('\n')

        self.assertGreater(len(lines), 1)
        for token in ('RTX', '5090', 'YouTube', 'Shorts'):
            self.assertIn(token, wrapped, f'拉丁 token 被破坏: {token}')
        for line in lines:
            self.assertNotIn(line[0], _NO_LINE_START_CHARS, f'行首禁则被破坏: {line!r}')

    def test_render_srt_writes_real_line_breaks(self):
        engine = SrtTransformEngine(SrtTransformConfig(max_line_length=6, max_lines=2))

        srt_text = engine.render_srt([{'start': 0.0, 'end': 2.0, 'text': '这是一句很长的中文字幕文本'}])

        self.assertIsNotNone(srt_text)
        body_lines = [line for line in srt_text.split('\n')[2:] if line]
        self.assertGreaterEqual(len(body_lines), 2)
        self.assertEqual(''.join(body_lines), '这是一句很长的中文字幕文本')

    def test_render_srt_keeps_cue_intact_for_parser_engine(self):
        engine = SrtTransformEngine(SrtTransformConfig(max_line_length=999, max_lines=99))
        text = '这是一句很长的中文字幕文本' * 5

        srt_text = engine.render_srt([{'start': 0.0, 'end': 2.0, 'text': text}])

        self.assertIn(text, srt_text)

    def test_render_srt_preserves_already_wrapped_cue(self):
        """流媒体 SRT 生成路径已按真实视频尺寸折好行，render_srt 不得重排。

        这是实测发现的回归：烧录前 ``_prepare_streaming_srt_cues`` 会把每行折好
        （竖屏 18 字/3 行等），若 render_srt 再调用 wrap_text，会把两行合并回一行
        再重新折，行数与断点都跟 ASS 侧不一致，成片可能出现超宽行。
        """
        engine = SrtTransformEngine(
            SrtTransformConfig(max_line_length=18, max_lines=3, split_long_cues=False)
        )
        wrapped = '你好，这是一段比较长的中文字幕\n用来验证二次换行的行为'

        srt_text = engine.render_srt([{'start': 0.0, 'end': 2.0, 'text': wrapped}])

        body_lines = [line for line in srt_text.split('\n')[2:] if line]
        self.assertEqual(body_lines, wrapped.split('\n'))

    def test_render_srt_still_wraps_single_line_cue(self):
        """单行文本（ASR 直接落盘场景）仍必须折行，修复不能把 F5 一起关掉。"""
        engine = SrtTransformEngine(SrtTransformConfig(max_line_length=18, max_lines=3))
        single = '你好，这是一段比较长的中文字幕内容用于验证二次换行行为是否正确'

        srt_text = engine.render_srt([{'start': 0.0, 'end': 2.0, 'text': single}])

        body_lines = [line for line in srt_text.split('\n')[2:] if line]
        self.assertGreater(len(body_lines), 1)
        self.assertEqual(''.join(body_lines), single)


class FinalizeCuesLimitTests(unittest.TestCase):
    def test_max_cue_duration_blocks_over_long_merge(self):
        engine = SrtTransformEngine(SrtTransformConfig(merge_gap_s=0.3))
        cues = [
            {'start': 0.0, 'end': 5.0, 'text': 'hello world'},
            {'start': 5.1, 'end': 12.0, 'text': 'world again'},
        ]

        finalized = engine.finalize_cues(cues, total_duration_s=12.0)

        self.assertEqual(len(finalized), 2)
        for cue in finalized:
            self.assertLessEqual(cue['end'] - cue['start'], 8.0)

    def test_merge_still_happens_when_under_duration_cap(self):
        engine = SrtTransformEngine(SrtTransformConfig(merge_gap_s=0.3, max_cue_duration_s=20.0))
        cues = [
            {'start': 0.0, 'end': 5.0, 'text': 'hello world'},
            {'start': 5.1, 'end': 12.0, 'text': 'world again'},
        ]

        finalized = engine.finalize_cues(cues, total_duration_s=12.0)

        self.assertEqual(len(finalized), 1)
        self.assertEqual(finalized[0]['text'], 'hello world again')

    def test_max_chars_per_second_blocks_dense_merge(self):
        engine = SrtTransformEngine(
            SrtTransformConfig(merge_gap_s=0.3, max_cue_duration_s=60.0, max_chars_per_second=6.0)
        )
        cues = [
            {'start': 0.0, 'end': 1.0, 'text': 'hello world'},
            {'start': 1.0, 'end': 1.4, 'text': 'world again'},
        ]

        finalized = engine.finalize_cues(cues, total_duration_s=4.0)

        self.assertEqual(len(finalized), 2)

        baseline = SrtTransformEngine(SrtTransformConfig(merge_gap_s=0.3))
        merged = baseline.finalize_cues(
            [
                {'start': 0.0, 'end': 1.0, 'text': 'hello world'},
                {'start': 1.0, 'end': 1.4, 'text': 'world again'},
            ],
            total_duration_s=4.0,
        )
        self.assertEqual(len(merged), 1)

    def test_short_duration_alone_is_no_longer_a_merge_reason(self):
        engine = SrtTransformEngine(SrtTransformConfig(merge_gap_s=0.3))
        cues = [
            {'start': 0.0, 'end': 0.6, 'text': 'alpha beta'},
            {'start': 0.7, 'end': 1.3, 'text': 'gamma delta'},
        ]

        finalized = engine.finalize_cues(cues, total_duration_s=5.0)

        self.assertEqual(len(finalized), 2)


class CrossWindowDedupTests(unittest.TestCase):
    def test_identical_text_across_windows_collapses_to_one_cue(self):
        engine = SrtTransformEngine(SrtTransformConfig())
        cues = [
            _aligned(0.0, 1.0, 'hello world again', 0),
            _aligned(1.5, 2.5, 'hello world again', 1),
        ]

        stitched = engine.stitch_aligned_cues(cues, total_duration_s=10.0)

        self.assertEqual(len(stitched), 1)
        self.assertEqual(stitched[0]['text'] if isinstance(stitched[0], dict) else stitched[0].text, 'hello world again')

    def test_similar_text_across_windows_collapses_to_one_cue(self):
        engine = SrtTransformEngine(SrtTransformConfig())
        cues = [
            _aligned(0.0, 1.0, '这是一段测试文本', 0),
            _aligned(1.5, 2.5, '这是一段测试文字', 1),
        ]

        stitched = engine.stitch_aligned_cues(cues, total_duration_s=10.0)

        self.assertEqual(len(stitched), 1)

    def test_same_window_duplicates_are_not_collapsed_by_cross_window_rule(self):
        engine = SrtTransformEngine(SrtTransformConfig())
        cues = [
            _aligned(0.0, 1.0, 'hello world again', 0),
            _aligned(1.5, 2.5, 'hello world again', 0),
        ]

        stitched = engine.stitch_aligned_cues(cues, total_duration_s=10.0)

        self.assertEqual(len(stitched), 2)

    def test_far_apart_cross_window_cues_are_kept(self):
        engine = SrtTransformEngine(SrtTransformConfig())
        cues = [
            _aligned(0.0, 1.0, 'hello world again', 0),
            _aligned(6.0, 7.0, 'hello world again', 1),
        ]

        stitched = engine.stitch_aligned_cues(cues, total_duration_s=10.0)

        self.assertEqual(len(stitched), 2)

    def test_legacy_cues_without_window_index_are_not_cross_window_dupes(self):
        # source_window_index 为 -1（单窗/未知来源）时跨窗规则必须完全失效。
        engine = SrtTransformEngine(SrtTransformConfig())
        left = AlignedSubtitleCue(start_s=0.0, end_s=1.0, text='hello world again')
        right = AlignedSubtitleCue(start_s=1.5, end_s=2.5, text='hello world again')

        stitched = engine.stitch_aligned_cues([left, right], total_duration_s=10.0)

        self.assertEqual(len(stitched), 2)
        self.assertEqual(engine._window_index_of(left), -1)
        self.assertEqual(engine._window_index_of({'start': 0.0, 'end': 1.0, 'text': 'x'}), -1)
        self.assertFalse(engine._is_cross_window_dup(left, right))

    def test_source_window_index_survives_dict_coercion(self):
        engine = SrtTransformEngine(SrtTransformConfig())
        cues = [{'start': 0.0, 'end': 1.0, 'text': '一段正常字幕', 'source_window_index': 2}]

        coerced = engine._coerce_cue_dicts(cues)
        self.assertEqual(coerced[0]['source_window_index'], 2)

        cleaned = engine.clean_hallucinations(cues)
        self.assertEqual(cleaned[0]['source_window_index'], 2)

        legacy = engine.clean_hallucinations([{'start': 0.0, 'end': 1.0, 'text': '一段正常字幕'}])
        self.assertEqual(legacy[0]['source_window_index'], -1)


class ShortCueFallbackTests(unittest.TestCase):
    def test_no_cue_shorter_than_visible_threshold_reaches_output(self):
        engine = SrtTransformEngine(SrtTransformConfig())
        cues = [
            {'start': 0.0, 'end': 2.0, 'text': '第一句内容'},
            {'start': 2.0, 'end': 2.02, 'text': '补充'},
            {'start': 2.03, 'end': 4.0, 'text': '第三句内容'},
            {'start': 9.95, 'end': 10.0, 'text': '末尾'},
        ]

        finalized = engine.finalize_cues(cues, total_duration_s=10.0)

        self.assertTrue(finalized)
        for cue in finalized:
            self.assertGreaterEqual(cue['end'] - cue['start'], 0.3)

    def test_terminal_fragment_is_absorbed_instead_of_emitted(self):
        engine = SrtTransformEngine(SrtTransformConfig())
        cues = [
            {'start': 0.0, 'end': 5.0, 'text': 'hello there'},
            {'start': 9.98, 'end': 10.0, 'text': 'bye'},
        ]

        finalized = engine.finalize_cues(cues, total_duration_s=10.0)

        self.assertEqual(len(finalized), 1)
        self.assertIn('bye', finalized[0]['text'])
        self.assertGreaterEqual(finalized[0]['end'] - finalized[0]['start'], 0.3)


class FillerFilterTests(unittest.TestCase):
    def test_english_wh_filler_words_are_preserved(self):
        engine = SrtTransformEngine(SrtTransformConfig())

        self.assertEqual(engine.normalize_text('I like you'), 'I like you')
        self.assertEqual(engine.normalize_text('you know what I mean'), 'you know what I mean')

    def test_standalone_cjk_filler_cue_is_removed(self):
        engine = SrtTransformEngine(SrtTransformConfig())

        self.assertEqual(engine.normalize_text('嗯'), '')
        self.assertEqual(engine.apply_text_processing([{'start': 0.0, 'end': 1.0, 'text': '嗯'}]), [])

    def test_cjk_word_repetition_is_not_collapsed(self):
        engine = SrtTransformEngine(SrtTransformConfig())

        self.assertEqual(engine.normalize_text('好好好'), '好好好')
        processed = engine.apply_text_processing([{'start': 0.0, 'end': 1.0, 'text': '好好好'}])
        self.assertEqual([cue['text'] for cue in processed], ['好好好'])

    def test_cjk_filler_inside_sentence_is_preserved(self):
        engine = SrtTransformEngine(SrtTransformConfig())

        self.assertEqual(engine.normalize_text('嗯这个很有意思'), '嗯这个很有意思')
        self.assertEqual(engine.normalize_text('我知道了嗯'), '我知道了')


class HallucinationRuleTests(unittest.TestCase):
    def test_short_subscription_line_is_removed(self):
        engine = SrtTransformEngine(SrtTransformConfig())

        cleaned = engine.clean_hallucinations([{'start': 0.0, 'end': 1.0, 'text': '请点赞订阅我们的频道'}])

        self.assertEqual(cleaned, [])

    def test_symbol_only_line_is_removed(self):
        engine = SrtTransformEngine(SrtTransformConfig())

        cleaned = engine.clean_hallucinations([{'start': 0.0, 'end': 1.0, 'text': '♪♪♪'}])

        self.assertEqual(cleaned, [])

    def test_long_sentence_mentioning_subscription_keyword_is_kept(self):
        engine = SrtTransformEngine(SrtTransformConfig())
        text = '他昨天在直播里说希望大家多多点赞支持这个频道并且记得分享给朋友'

        cleaned = engine.clean_hallucinations([{'start': 0.0, 'end': 8.0, 'text': text}])

        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]['text'], text)

    def test_repeat_during_silence_is_removed_when_speech_spans_given(self):
        engine = SrtTransformEngine(SrtTransformConfig())
        cues = [
            {'start': 0.0, 'end': 1.0, 'text': '这是正常的一句话'},
            {'start': 30.0, 'end': 31.0, 'text': '这是正常的一句话'},
        ]

        kept = engine.clean_hallucinations(cues, speech_spans=[(0.0, 1.0)])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['text'], '这是正常的一句话')

        without_spans = engine.clean_hallucinations(list(cues))
        self.assertEqual(len(without_spans), 2)

    def test_speech_spans_keeps_cues_with_actual_speech_coverage(self):
        engine = SrtTransformEngine(SrtTransformConfig())
        cues = [
            {'start': 0.0, 'end': 1.0, 'text': '这是正常的一句话'},
            {'start': 30.0, 'end': 31.0, 'text': '今天阳光很好'},
        ]

        kept = engine.clean_hallucinations(cues, speech_spans=[(0.0, 1.0)])

        self.assertEqual(len(kept), 2)


class MergedDurationCapTests(unittest.TestCase):
    def test_max_expected_duration_is_clamped(self):
        engine = SrtTransformEngine(SrtTransformConfig(max_chars_per_second=2.0))

        self.assertEqual(engine._max_expected_duration(''), 1.0)
        self.assertEqual(engine._max_expected_duration('ab'), 1.0)
        self.assertEqual(engine._max_expected_duration('x' * 100), 6.0)

    def test_pick_better_duplicate_caps_end_time(self):
        engine = SrtTransformEngine(SrtTransformConfig(max_chars_per_second=2.0))
        left = _aligned(0.0, 1.0, 'hello world again', 0)
        right = _aligned(0.5, 12.0, 'hello world again', 0)

        merged = engine._pick_better_duplicate(left, right)

        self.assertEqual(merged.start_s, 0.0)
        self.assertLessEqual(merged.end_s, 6.0)

    def test_merge_if_continuation_caps_end_time(self):
        engine = SrtTransformEngine(SrtTransformConfig(max_chars_per_second=2.0))
        left = _aligned(0.0, 1.0, 'hello world again', 0)
        right = _aligned(0.5, 12.0, 'hello world again', 0)

        merged = engine._merge_if_continuation(left, right)

        self.assertIsNotNone(merged)
        self.assertLessEqual(merged.end_s, 6.0)
        self.assertGreaterEqual(merged.end_s, merged.start_s)


class SplitAndJoinTests(unittest.TestCase):
    def test_split_long_cue_caps_each_part_duration(self):
        engine = SrtTransformEngine(SrtTransformConfig(max_line_length=6, max_lines=2))
        cue = {
            'start': 0.0,
            'end': 30.0,
            'text': '这是第一句话。这是第二句话。这是第三句话。',
        }

        split = engine.split_long_cue(cue)

        self.assertGreater(len(split), 1)
        self.assertEqual(split[0]['start'], 0.0)
        for part in split:
            self.assertLessEqual(part['end'] - part['start'], 8.0 + 1e-9)
        for previous, current in zip(split, split[1:]):
            self.assertLessEqual(previous['end'], current['start'] + 1e-9)

    def test_split_long_cue_keeps_existing_behaviour_under_cap(self):
        engine = SrtTransformEngine(SrtTransformConfig(max_line_length=6, max_lines=2))
        cue = {
            'start': 0.0,
            'end': 6.0,
            'text': '这是第一句话。这是第二句话。这是第三句话。',
        }

        split = engine.split_long_cue(cue)

        self.assertGreater(len(split), 1)
        self.assertEqual(split[0]['start'], 0.0)
        self.assertEqual(split[-1]['end'], 6.0)

    def test_join_texts_omits_space_between_cjk(self):
        self.assertEqual(_join_texts('第一句', '第二句'), '第一句第二句')
        self.assertEqual(_join_texts('hello', 'world'), 'hello world')
        self.assertEqual(_join_texts('', 'world'), 'world')
        self.assertEqual(_join_texts('hello', ''), 'hello')

    def test_finalize_merges_cjk_texts_without_inserting_spaces(self):
        engine = SrtTransformEngine(SrtTransformConfig(merge_gap_s=0.3, max_cue_duration_s=60.0))
        # 无字符重叠但时间上重叠（gap < 0）时走 _join_texts 回退拼接。
        cues = [
            {'start': 0.0, 'end': 2.0, 'text': '第一句内容'},
            {'start': 1.5, 'end': 4.0, 'text': '另一段文字'},
        ]

        finalized = engine.finalize_cues(cues, total_duration_s=4.0)

        self.assertEqual(len(finalized), 1)
        self.assertEqual(finalized[0]['text'], '第一句内容另一段文字')
        self.assertNotIn(' ', finalized[0]['text'])

    def test_finalize_joins_latin_texts_with_space(self):
        engine = SrtTransformEngine(SrtTransformConfig(merge_gap_s=0.3, max_cue_duration_s=60.0))
        cues = [
            {'start': 0.0, 'end': 2.0, 'text': 'alpha beta'},
            {'start': 1.5, 'end': 4.0, 'text': 'gamma delta'},
        ]

        finalized = engine.finalize_cues(cues, total_duration_s=4.0)

        self.assertEqual(len(finalized), 1)
        self.assertEqual(finalized[0]['text'], 'alpha beta gamma delta')


if __name__ == '__main__':
    unittest.main()
