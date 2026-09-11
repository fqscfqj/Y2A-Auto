# -*- coding: utf-8 -*-
"""字幕外观与编码参数的接线测试。

覆盖三层：
1. 外观覆盖层接入 ASS 渲染链路（样式 dict / ASS 文档 / force_style）；
2. 编码参数与色彩元数据接入 FFmpeg 命令构造；
3. 硬件编码失败的降级阶段决策与错误识别。

这些测试不依赖网络、FFmpeg 二进制或 GPU：只验证参数装配，不真正转码。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.subtitle_style import parse_style_overrides
from modules.task_manager import TaskProcessor
from modules.video_encoder_params import (
    build_color_vui_params,
    normalize_color_metadata,
    resolve_color_metadata,
)


class _Logger:
    def __init__(self):
        self.messages = []

    def _record(self, level, message):
        self.messages.append((level, str(message)))

    def info(self, message):
        self._record('info', message)

    def warning(self, message):
        self._record('warning', message)

    def error(self, message):
        self._record('error', message)

    def debug(self, message):
        pass

    def text(self):
        return '\n'.join(message for _, message in self.messages)


def _make_processor(config=None):
    processor = TaskProcessor.__new__(TaskProcessor)
    processor.config = config or {}
    return processor


DEFAULT_STYLE_CONFIG = {
    'SUBTITLE_FONT_SIZE_SCALE': 1.0,
    'SUBTITLE_MARGIN_V_SCALE': 1.0,
    'SUBTITLE_FONT_COLOR': '#FFFFFF',
    'SUBTITLE_OUTLINE_COLOR': '#000000',
    'SUBTITLE_OUTLINE_ENABLED': True,
    'SUBTITLE_OUTLINE_SCALE': 1.0,
    'SUBTITLE_SHADOW_ENABLED': True,
    'SUBTITLE_SHADOW_SCALE': 1.0,
    'SUBTITLE_TEXT_BOLD': True,
    'SUBTITLE_BACKGROUND_ENABLED': False,
    'SUBTITLE_BACKGROUND_COLOR': '#000000',
    'SUBTITLE_BACKGROUND_OPACITY': 0.5,
}


class StyleOverrideWiringTests(unittest.TestCase):
    """外观覆盖层必须真正影响渲染结果，而不只是被解析出来。"""

    def test_no_overrides_keeps_legacy_style(self):
        style = TaskProcessor._build_streaming_ass_style(1920, 1080)
        self.assertEqual(style['PrimaryColour'], '&H00FFFFFF')
        self.assertEqual(style['OutlineColour'], '&HB2000000')
        self.assertEqual(style['BorderStyle'], 1)
        self.assertEqual(style['Bold'], 1)

    def test_default_overrides_preserve_legacy_style(self):
        base = TaskProcessor._build_streaming_ass_style(1920, 1080)
        processor = _make_processor(DEFAULT_STYLE_CONFIG)
        overrides = processor._resolve_subtitle_style_overrides()
        styled = TaskProcessor._build_streaming_ass_style(1920, 1080, overrides)
        for key in ('FontSize', 'Outline', 'Shadow', 'MarginL', 'MarginR', 'Alignment'):
            self.assertAlmostEqual(float(styled[key]), float(base[key]), places=4, msg=key)
        self.assertEqual(int(styled['MarginV']), int(base['MarginV']))
        self.assertEqual(styled['PrimaryColour'], '&H00FFFFFF')
        self.assertEqual(styled['OutlineColour'], '&HB2000000')
        self.assertEqual(styled['BorderStyle'], 1)
        self.assertEqual(styled['Bold'], 1)

    def test_font_size_and_margin_scale_applied(self):
        base = TaskProcessor._build_streaming_ass_style(1920, 1080)
        processor = _make_processor(dict(
            DEFAULT_STYLE_CONFIG,
            SUBTITLE_FONT_SIZE_SCALE=1.5,
            SUBTITLE_MARGIN_V_SCALE=2.0,
        ))
        overrides = processor._resolve_subtitle_style_overrides()
        styled = TaskProcessor._build_streaming_ass_style(1920, 1080, overrides)
        self.assertAlmostEqual(styled['FontSize'], round(base['FontSize'] * 1.5, 2), places=2)
        self.assertEqual(styled['MarginV'], int(round(base['MarginV'] * 2.0)))

    def test_font_color_converted_to_ass_bgr(self):
        processor = _make_processor(dict(DEFAULT_STYLE_CONFIG, SUBTITLE_FONT_COLOR='#FF0000'))
        overrides = processor._resolve_subtitle_style_overrides()
        styled = TaskProcessor._build_streaming_ass_style(1920, 1080, overrides)
        # ASS 是 BGR：红 = &H000000FF
        self.assertEqual(styled['PrimaryColour'], '&H000000FF')

    def test_outline_disabled_zeroes_outline_and_keeps_color(self):
        processor = _make_processor(dict(DEFAULT_STYLE_CONFIG, SUBTITLE_OUTLINE_ENABLED=False))
        overrides = processor._resolve_subtitle_style_overrides()
        styled = TaskProcessor._build_streaming_ass_style(1920, 1080, overrides)
        self.assertEqual(float(styled['Outline']), 0.0)
        # 关闭描边时不应改动描边色，保持上游默认值
        self.assertEqual(styled['OutlineColour'], '&HB2000000')

    def test_background_switches_border_style(self):
        processor = _make_processor(dict(
            DEFAULT_STYLE_CONFIG,
            SUBTITLE_BACKGROUND_ENABLED=True,
            SUBTITLE_BACKGROUND_COLOR='#FFFFFF',
            SUBTITLE_BACKGROUND_OPACITY=1.0,
        ))
        overrides = processor._resolve_subtitle_style_overrides()
        styled = TaskProcessor._build_streaming_ass_style(1920, 1080, overrides)
        self.assertEqual(styled['BorderStyle'], 4)
        # 不透明度 1.0 -> alpha 00，白底 BGR 反相后仍是 FFFFFF
        self.assertEqual(styled['BackColour'], '&H00FFFFFF')

    def test_bold_toggle(self):
        processor = _make_processor(dict(DEFAULT_STYLE_CONFIG, SUBTITLE_TEXT_BOLD=False))
        overrides = processor._resolve_subtitle_style_overrides()
        styled = TaskProcessor._build_streaming_ass_style(1920, 1080, overrides)
        self.assertEqual(styled['Bold'], 0)

    def test_non_dict_config_is_tolerated(self):
        # self.config 意外不是 dict 时按空配置处理，走默认观感而不是崩掉
        processor = _make_processor(['not', 'a', 'dict'])
        overrides = processor._resolve_subtitle_style_overrides(_Logger())
        self.assertEqual(overrides['font_size_scale'], 1.0)
        self.assertEqual(overrides['font_color'], '#FFFFFF')

    def test_parse_failure_falls_back_to_none(self):
        from unittest import mock

        import modules.task_manager as task_manager

        processor = _make_processor(DEFAULT_STYLE_CONFIG)
        logger = _Logger()
        with mock.patch.object(
            task_manager, 'parse_style_overrides', side_effect=RuntimeError('boom')
        ):
            self.assertIsNone(processor._resolve_subtitle_style_overrides(logger))
        self.assertIn('回退默认观感', logger.text())

    def test_force_style_reflects_overrides(self):
        processor = _make_processor(dict(
            DEFAULT_STYLE_CONFIG,
            SUBTITLE_FONT_SIZE_SCALE=1.25,
            SUBTITLE_OUTLINE_ENABLED=False,
            SUBTITLE_SHADOW_ENABLED=False,
        ))
        overrides = processor._resolve_subtitle_style_overrides()
        force_style = TaskProcessor._build_subtitle_force_style(
            'Noto Sans CJK SC', 1920, 1080, overrides
        )
        self.assertIn('FontSize=67.5', force_style)
        self.assertIn('Outline=0', force_style)
        self.assertIn('Shadow=0', force_style)


class AssDocumentWiringTests(unittest.TestCase):
    """ASS 文档必须使用覆盖后的样式，否则烧录观感与设置页不符。"""

    def _build(self, config, tmpdir):
        processor = _make_processor(config)
        logger = _Logger()
        srt_path = os.path.join(tmpdir, 'sub.srt')
        ass_path = os.path.join(tmpdir, 'sub.ass')
        with open(srt_path, 'w', encoding='utf-8') as handle:
            handle.write('1\n00:00:00,200 --> 00:00:02,000\n测试字幕内容\n')
        ok = processor._convert_srt_to_ass(
            srt_path, ass_path, logger,
            video_width=1920, video_height=1080,
            font_family='Noto Sans CJK SC',
            style_overrides=processor._resolve_subtitle_style_overrides(logger),
        )
        self.assertTrue(ok, logger.text())
        with open(ass_path, 'r', encoding='utf-8') as handle:
            return handle.read()

    def test_default_config_keeps_legacy_colors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ass_text = self._build(DEFAULT_STYLE_CONFIG, tmpdir)
        style_line = [ln for ln in ass_text.splitlines() if ln.startswith('Style: Default')][0]
        self.assertIn('&H00FFFFFF', style_line)
        self.assertIn('&HB2000000', style_line)

    def test_custom_colors_and_border_style_reach_ass(self):
        config = dict(
            DEFAULT_STYLE_CONFIG,
            SUBTITLE_FONT_COLOR='#00FF00',
            SUBTITLE_OUTLINE_COLOR='#0000FF',
            SUBTITLE_BACKGROUND_ENABLED=True,
            SUBTITLE_BACKGROUND_COLOR='#101010',
            SUBTITLE_BACKGROUND_OPACITY=0.5,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            ass_text = self._build(config, tmpdir)
        style_line = [ln for ln in ass_text.splitlines() if ln.startswith('Style: Default')][0]
        # 绿字：BGR 反相 -> 00FF00；蓝描边：BGR -> FF0000，alpha 固定 B2
        self.assertIn('&H0000FF00', style_line)
        self.assertIn('&HB2FF0000', style_line)
        fields = style_line.split(',')
        # 字段顺序: Name,Fontname,Fontsize,Primary,Secondary,Outline,Back,Bold,...
        self.assertEqual(fields[7], '1')
        # BorderStyle 位于 Outline/Shadow 之后
        self.assertIn(',4,', style_line)
        # 不透明度 0.5 -> alpha 80，底框色 #101010 反相为 101010
        self.assertIn('&H80101010', style_line)


class EncoderWiringTests(unittest.TestCase):
    """编码参数与色彩元数据在命令构造层的接线。"""

    def test_embed_cmd_includes_color_params(self):
        cmd = TaskProcessor._build_embed_ffmpeg_cmd(
            ffmpeg_bin='ffmpeg',
            input_video='in.mp4',
            vf_filter='subtitles=sub.ass',
            vparams=['-c:v', 'libx264'],
            aparams=['-c:a', 'copy'],
            output_video='out.mp4',
            color_params=['-colorspace', 'bt709'],
        )
        joined = ' '.join(cmd)
        self.assertIn('-colorspace bt709', joined)
        # 色彩参数必须在音频参数之前，且输出文件在最后
        self.assertLess(cmd.index('-colorspace'), cmd.index('-c:a'))
        self.assertEqual(cmd[-1], 'out.mp4')

    def test_embed_cmd_without_color_params(self):
        cmd = TaskProcessor._build_embed_ffmpeg_cmd(
            ffmpeg_bin='ffmpeg',
            input_video='in.mp4',
            vf_filter='subtitles=sub.ass',
            vparams=['-c:v', 'libx264'],
            aparams=['-c:a', 'copy'],
            output_video='out.mp4',
        )
        self.assertNotIn('-colorspace', cmd)
        self.assertEqual(cmd[-1], 'out.mp4')

    def test_normalize_and_resolve_agree(self):
        info = {
            'color_space': 'bt709',
            'color_primaries': 'bt709',
            'color_transfer': 'bt709',
            'color_range': 'tv',
        }
        color_map = normalize_color_metadata('auto', info)
        params = resolve_color_metadata('auto', info)
        self.assertEqual(color_map['colorspace'], 'bt709')
        self.assertIn('bt709', params)
        for value in color_map.values():
            self.assertIn(value, params)

    def test_color_vui_only_for_cpu(self):
        color_map = {'colorspace': 'bt709', 'color_primaries': 'bt709', 'color_trc': 'bt709'}
        cpu_params = build_color_vui_params('cpu', color_map, None)
        self.assertEqual(cpu_params[0], '-x264-params')
        self.assertIn('colorprim=bt709', cpu_params[1])
        self.assertIn('transfer=bt709', cpu_params[1])
        self.assertIn('colormatrix=bt709', cpu_params[1])
        # 硬件编码器由通用选项负责，不应再注入 x264 私有参数
        for encoder in ('nvidia', 'intel', 'amd', '', None):
            self.assertEqual(build_color_vui_params(encoder, color_map, None), [])

    def test_color_vui_range_not_duplicated(self):
        # range 无法通过 x264-params 生效，必须只由通用 -color_range 负责
        color_map = {'colorspace': 'bt709', 'color_range': 'tv'}
        params = build_color_vui_params('cpu', color_map, None)
        self.assertNotIn('range', params[1])

    def test_color_vui_skipped_when_user_sets_x264_params(self):
        color_map = {'color_primaries': 'bt709'}
        custom = ['-c:v', 'libx264', '-x264-params', 'aq-mode=3']
        self.assertEqual(build_color_vui_params('cpu', color_map, custom), [])

    def test_color_vui_empty_maps(self):
        self.assertEqual(build_color_vui_params('cpu', {}, None), [])
        self.assertEqual(build_color_vui_params('cpu', None, None), [])

    def test_color_metadata_off_mode(self):
        info = {'color_space': 'bt709'}
        self.assertEqual(normalize_color_metadata('off', info), {})
        self.assertEqual(resolve_color_metadata('off', info), [])


class AssInputStyleTests(unittest.TestCase):
    """ASS/SSA 输入的对外观配置处理。

    缺陷：该分支此前一律「保留源样式」，用户设置的字号/颜色/描边/背景全部
    静默失效，但日志照样打印「字幕外观提示」，让人误以为配置已应用。
    """

    def test_default_config_is_detected_as_unchanged(self):
        # parse_style_overrides 总返回完整字典，不能靠「非空」判断用户意图
        self.assertFalse(TaskProcessor._style_overrides_differ_from_defaults(
            parse_style_overrides({})))

    def test_explicit_default_value_is_not_a_change(self):
        self.assertFalse(TaskProcessor._style_overrides_differ_from_defaults(
            parse_style_overrides({'SUBTITLE_FONT_SIZE_SCALE': 1.0})))

    def test_real_changes_are_detected(self):
        for config in (
            {'SUBTITLE_FONT_SIZE_SCALE': 1.5},
            {'SUBTITLE_FONT_COLOR': '#FF0000'},
            {'SUBTITLE_OUTLINE_ENABLED': False},
            {'SUBTITLE_BACKGROUND_ENABLED': True},
            {'SUBTITLE_MARGIN_V_SCALE': 0.6},
        ):
            self.assertTrue(
                TaskProcessor._style_overrides_differ_from_defaults(
                    parse_style_overrides(config)),
                config)

    def test_non_dict_is_not_a_change(self):
        for value in (None, '', 123, []):
            self.assertFalse(TaskProcessor._style_overrides_differ_from_defaults(value))

    def test_custom_appearance_produces_force_style_covering_user_keys(self):
        """确实改过配置时，force_style 必须带出用户改动的键。"""
        overrides = parse_style_overrides(
            {'SUBTITLE_FONT_SIZE_SCALE': 1.5, 'SUBTITLE_OUTLINE_ENABLED': False})
        forced = TaskProcessor._build_subtitle_force_style(
            'Noto Sans CJK SC', 1920, 1080, overrides)
        self.assertIn('force_style=', forced)
        self.assertIn('FontSize=', forced)
        # 关闭描边后 Outline 必须为 0，否则用户设置等于没生效
        self.assertIn('Outline=0', forced)


class RetryStageTests(unittest.TestCase):
    """硬件编码失败的降级阶段决策。"""

    def test_hw_with_boost_and_option_error_retries_boost_off_then_cpu(self):
        """参数类错误：关掉质量增强有可能治好，值得保留硬件加速再试一次。"""
        stages = TaskProcessor._resolve_embed_retry_stages(
            'nvidia', True, True, hw_option_error=True)
        self.assertEqual(stages, ['hw_no_boost', 'cpu'])

    def test_hw_with_boost_and_device_error_goes_straight_to_cpu(self):
        """设备/驱动类错误：关掉增强没有任何作用，直接 CPU。

        此前 `hw_error_detected` 形参在函数体内从未被读取，真硬编失败
        （设备被占用、驱动崩溃、显存不足）仍会先按同一个硬编器把整部视频
        重跑一遍才轮到 CPU —— 长视频上这是纯浪费。
        """
        stages = TaskProcessor._resolve_embed_retry_stages(
            'nvidia', True, True, hw_option_error=False)
        self.assertEqual(stages, ['cpu'])

    def test_hw_with_boost_and_unknown_error_still_tries_boost_off(self):
        """错误文本完全未知：无法断定是设备问题，仍先试关增强。"""
        stages = TaskProcessor._resolve_embed_retry_stages(
            'amd', True, False, hw_option_error=False)
        self.assertEqual(stages, ['hw_no_boost', 'cpu'])

    def test_hw_without_boost_goes_straight_to_cpu(self):
        stages = TaskProcessor._resolve_embed_retry_stages('intel', False, True)
        self.assertEqual(stages, ['cpu'])

    def test_legacy_signature_still_works(self):
        """未传 hw_option_error 时按旧语义（已知硬件错误不关增强重试）。"""
        self.assertEqual(
            TaskProcessor._resolve_embed_retry_stages('nvidia', True, True), ['cpu'])
        self.assertEqual(
            TaskProcessor._resolve_embed_retry_stages('nvidia', True, False),
            ['hw_no_boost', 'cpu'])

    def test_option_error_detection_is_a_strict_subset(self):
        """参数类模式必须是已知硬件错误模式的子集，否则分流会漏掉错误。"""
        known = set(TaskProcessor._KNOWN_HW_ENCODER_ERROR_PATTERNS)
        option = set(TaskProcessor._HW_OPTION_ERROR_PATTERNS)
        self.assertTrue(option, '参数类模式集合不得为空')
        self.assertTrue(option.issubset(known), option - known)

    def test_option_and_device_errors_are_classified_differently(self):
        """端到端分流：同一组真实错误文本必须落到相反的两支。"""
        option_messages = (
            "Unrecognized option 'spatial-aq'.",
            'Option not found',
            'Error setting option rc-lookahead to value 32',
        )
        device_messages = (
            'No NVENC capable devices found',
            'CUDA_ERROR_OUT_OF_MEMORY',
            'Error creating a VAAPI device',
            'Error initializing an internal MFX session',
        )
        for message in option_messages:
            self.assertTrue(TaskProcessor._is_hw_option_error(message), message)
            self.assertTrue(TaskProcessor._is_known_hw_encoder_error(message), message)
        for message in device_messages:
            self.assertFalse(TaskProcessor._is_hw_option_error(message), message)
            self.assertTrue(TaskProcessor._is_known_hw_encoder_error(message), message)

    def test_cpu_encoder_does_not_retry_itself(self):
        # 已是 CPU 编码时重跑同一命令必然同样失败，不再浪费一次完整转码
        self.assertEqual(TaskProcessor._resolve_embed_retry_stages('cpu', True, True), [])
        self.assertEqual(TaskProcessor._resolve_embed_retry_stages('cpu', False, False), [])

    def test_unknown_encoder_values_are_safe(self):
        for value in (None, '', 'gpu', 123):
            self.assertEqual(TaskProcessor._resolve_embed_retry_stages(value, True, True), [])

    def test_unknown_option_errors_trigger_hw_fallback(self):
        # 老版本 FFmpeg / 老驱动不认识新增的质量增强参数时，必须能识别为硬件错误
        for message in (
            "Unrecognized option 'spatial-aq'.",
            'Option not found',
            'Error setting option rc-lookahead to value 32',
            'Error applying encoder options',
            'Error parsing options',
            'Invalid option',
        ):
            self.assertTrue(
                TaskProcessor._is_known_hw_encoder_error(message), message
            )

    def test_unrelated_errors_not_treated_as_hw(self):
        self.assertFalse(TaskProcessor._is_known_hw_encoder_error('No space left on device'))
        self.assertFalse(TaskProcessor._is_known_hw_encoder_error(''))
        self.assertFalse(TaskProcessor._is_known_hw_encoder_error(None))


class AudioParamsTests(unittest.TestCase):
    def test_aac_is_copied(self):
        params = TaskProcessor._build_audio_transcode_params({'codec_name': 'aac'})
        self.assertEqual(params, ['-c:a', 'copy'])

    def test_bitrate_ladder(self):
        cases = {
            192000: '192k', 160000: '160k', 128000: '128k', 96000: '96k', 64000: '64k',
        }
        for source, expected in cases.items():
            params = TaskProcessor._build_audio_transcode_params({
                'codec_name': 'opus', 'bit_rate': source, 'channels': 2,
            })
            self.assertIn(expected, params, source)

    def test_missing_info_defaults(self):
        params = TaskProcessor._build_audio_transcode_params(None)
        self.assertEqual(params[:2], ['-c:a', 'aac'])
        self.assertIn('128k', params)


if __name__ == '__main__':
    unittest.main()
