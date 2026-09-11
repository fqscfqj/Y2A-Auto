# -*- coding: utf-8 -*-
"""VIDEO_CPU_CODEC / libx265 软编码路径的单元测试。

本文件覆盖三块内容，每块都对应一个实测发现的真实陷阱：

1. **参数构造**：x265 的质量增强项没有独立 ffmpeg 选项，只能写进 -x265-params；
   而色彩 VUI 补写也走同一个选项，两者必须合并成**一条**。实测给两次
   -x265-params 时后者完全覆盖前者，分成两条会静默丢掉先写的那批键。
2. **psy-rd 的冒号**：x264 的 psy-rd 写作 "1.0:0.0"，但 -x265-params 用冒号分隔
   键值对，照搬会把参数串切碎（实测编码直接失败，返回码非 0）。x265 要拆成
   psy-rd 与 psy-rdoq 两项。
3. **tune 白名单不通用**：`film` / `stillimage` 对 libx265 会让编码直接失败，
   而它们在 x264 白名单里。按 CPU 编码器分流校验，非法取值丢弃而不是透传。

真实 ffmpeg 的 VUI 回读验证见 tests/test_x265_smoke.py。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.video_encoder_params import (
    build_color_vui_params,
    build_encoder_params,
    normalize_cpu_codec,
    parse_encoder_config,
)


def _ctx(**overrides):
    """构造一个接近生产调用的编码上下文。"""
    ctx = {
        'height': 1080,
        'gop': 48,
        'gop_hevc': 96,
        'quality_mode': 'auto',
        'quality_value': None,
        'cpu_preset': 'medium',
        'cpu_preset_hd': 'veryfast',
        'duration_s': 300,
        'hw_quality_boost': True,
        'hw_quality_level': 'quality',
        'cpu_codec': 'x265',
        'software_tune': '',
        'color_map': None,
        'custom_params': None,
        'amd_backend': 'amf',
    }
    ctx.update(overrides)
    return ctx


def _private_value(params, option='-x265-params'):
    """取出某个私有参数选项的值；该选项出现多次时返回全部值。"""
    values = []
    for index, token in enumerate(params):
        if token == option and index + 1 < len(params):
            values.append(params[index + 1])
    return values


class NormalizeCpuCodecTests(unittest.TestCase):
    def test_known_values(self):
        for value in ('x264', 'x265', 'X264', 'X265', ' x265 '):
            self.assertEqual(normalize_cpu_codec(value), value.strip().lower())

    def test_invalid_values_fall_back_to_x264(self):
        # x264 是历史行为，非法配置必须回到它，不能让既有用户静默换编码器。
        for bad in (None, '', 'hevc', 'libx265', 265, True, [], {}):
            self.assertEqual(normalize_cpu_codec(bad), 'x264', f'bad={bad!r}')

    def test_only_two_values_are_accepted(self):
        for value in ('x264', 'x265'):
            self.assertEqual(normalize_cpu_codec(value), value)


class ParseCpuCodecConfigTests(unittest.TestCase):
    def test_default_is_x264(self):
        parsed = parse_encoder_config({})
        self.assertEqual(parsed['cpu_codec'], 'x264')
        self.assertEqual(parsed['software_tune'], '')

    def test_uppercase_config_value_is_normalized(self):
        parsed = parse_encoder_config({'VIDEO_CPU_CODEC': 'X265'})
        self.assertEqual(parsed['cpu_codec'], 'x265')

    def test_invalid_config_value_falls_back(self):
        for bad in ('libx265', 'hevc', 'h264', '265'):
            parsed = parse_encoder_config({'VIDEO_CPU_CODEC': bad})
            self.assertEqual(parsed['cpu_codec'], 'x264', f'bad={bad!r}')

    def test_x264_tune_stays_an_alias_of_software_tune(self):
        for codec, tune in (('x264', 'film'), ('x265', 'animation')):
            parsed = parse_encoder_config({
                'VIDEO_CPU_CODEC': codec,
                'VIDEO_X264_TUNE': tune,
            })
            self.assertEqual(parsed['software_tune'], tune)
            self.assertEqual(parsed['x264_tune'], parsed['software_tune'])

    def test_x265_rejects_x264_only_tunes(self):
        """film / stillimage 在 x265 下会让编码失败，必须丢弃而不是透传。"""
        for tune in ('film', 'stillimage'):
            parsed = parse_encoder_config({
                'VIDEO_CPU_CODEC': 'x265',
                'VIDEO_X264_TUNE': tune,
            })
            self.assertEqual(parsed['software_tune'], '', f'tune={tune}')

    def test_x264_keeps_the_same_tunes(self):
        for tune in ('film', 'stillimage'):
            parsed = parse_encoder_config({
                'VIDEO_CPU_CODEC': 'x264',
                'VIDEO_X264_TUNE': tune,
            })
            self.assertEqual(parsed['software_tune'], tune, f'tune={tune}')

    def test_tunes_valid_on_both_codecs_survive_the_switch(self):
        shared = ('animation', 'grain', 'psnr', 'ssim', 'fastdecode', 'zerolatency')
        for tune in shared:
            for codec in ('x264', 'x265'):
                parsed = parse_encoder_config({
                    'VIDEO_CPU_CODEC': codec,
                    'VIDEO_X264_TUNE': tune,
                })
                self.assertEqual(parsed['software_tune'], tune, f'{codec}/{tune}')

    def test_invalid_tune_is_dropped_on_both_codecs(self):
        for codec in ('x264', 'x265'):
            parsed = parse_encoder_config({
                'VIDEO_CPU_CODEC': codec,
                'VIDEO_X264_TUNE': 'none',
            })
            self.assertEqual(parsed['software_tune'], '')


class X265ParamShapeTests(unittest.TestCase):
    def test_default_codec_is_still_libx264(self):
        """不传 cpu_codec 时输出必须仍是 libx264（既有快照的兼容前提）。"""
        params = build_encoder_params('cpu', _ctx(cpu_codec=None))
        self.assertEqual(params[0:2], ['-c:v', 'libx264'])

    def test_x265_uses_libx265(self):
        params = build_encoder_params('cpu', _ctx())
        self.assertEqual(params[0:2], ['-c:v', 'libx265'])

    def test_x265_core_options(self):
        params = build_encoder_params('cpu', _ctx())
        self.assertEqual(params[params.index('-preset') + 1], 'medium')
        self.assertEqual(params[params.index('-profile:v') + 1], 'main')
        self.assertEqual(params[params.index('-bf') + 1], '2')
        self.assertEqual(params[params.index('-pix_fmt') + 1], 'yuv420p')
        self.assertEqual(params[params.index('-fps_mode') + 1], 'cfr')
        self.assertIn('-crf', params)

    def test_x265_uses_hevc_gop_not_h264_gop(self):
        """输出是 HEVC，关键帧间隔应跟硬件 HEVC 路径一致（gop_hevc）。"""
        params = build_encoder_params('cpu', _ctx(gop=48, gop_hevc=240))
        self.assertEqual(params[params.index('-g') + 1], '240')

    def test_x265_writes_hvc1_tag(self):
        """hvc1 标签是 Safari / QuickTime 识别 HEVC 的前提，与硬编路径一致。"""
        params = build_encoder_params('cpu', _ctx())
        self.assertEqual(params[params.index('-tag:v') + 1], 'hvc1')

    def test_x265_never_emits_vsync(self):
        params = build_encoder_params('cpu', _ctx())
        self.assertNotIn('-vsync', params)

    def test_x264_path_does_not_gain_the_hevc_tag(self):
        params = build_encoder_params('cpu', _ctx(cpu_codec='x264'))
        self.assertNotIn('-tag:v', params)

    def test_x265_hd_preset_path_applies(self):
        """1440p+ 且超过 10 分钟时走 cpu_preset_hd，x265 与 x264 共用同一判定。"""
        params = build_encoder_params(
            'cpu', _ctx(height=1440, duration_s=601, cpu_preset='medium',
                        cpu_preset_hd='veryfast')
        )
        self.assertEqual(params[params.index('-preset') + 1], 'veryfast')

    def test_x265_hd_preset_boundary(self):
        params = build_encoder_params(
            'cpu', _ctx(height=1440, duration_s=600, cpu_preset='medium',
                        cpu_preset_hd='veryfast')
        )
        self.assertEqual(params[params.index('-preset') + 1], 'medium')

    def test_x265_tune_inserted_after_preset(self):
        params = build_encoder_params(
            'cpu', _ctx(software_tune='animation', cpu_codec='x265')
        )
        preset_index = params.index('-preset')
        self.assertEqual(params[preset_index + 1], 'medium')
        self.assertEqual(params[preset_index + 2], '-tune')
        self.assertEqual(params[preset_index + 3], 'animation')

    def test_x265_drops_tune_that_is_illegal_for_it(self):
        params = build_encoder_params(
            'cpu', _ctx(software_tune='film', cpu_codec='x265')
        )
        self.assertNotIn('-tune', params)


class X265QualityBoostTests(unittest.TestCase):
    def test_boost_on_emits_the_enhancement_keys(self):
        value = _private_value(build_encoder_params('cpu', _ctx()))
        self.assertEqual(len(value), 1, 'x265 的私有参数必须只有一条')
        for key in ('aq-mode=3', 'aq-strength=0.8', 'psy-rd=1.0',
                    'psy-rdoq=0.0', 'rc-lookahead=40'):
            self.assertIn(key, value[0])

    def test_boost_off_emits_no_private_params_at_all(self):
        params = build_encoder_params(
            'cpu', _ctx(hw_quality_boost=False, color_map=None)
        )
        self.assertNotIn('-x265-params', params)
        self.assertNotIn('-x264-params', params)

    def test_boost_off_keeps_the_base_params(self):
        on = build_encoder_params('cpu', _ctx(hw_quality_boost=True))
        off = build_encoder_params('cpu', _ctx(hw_quality_boost=False))
        self.assertNotEqual(on, off)
        self.assertEqual(off[0:2], ['-c:v', 'libx265'])

    def test_psy_rd_is_split_so_the_colon_separator_is_not_corrupted(self):
        """x264 的 psy-rd 写作 '1.0:0.0'，照搬进 -x265-params 会切碎参数串。"""
        value = _private_value(build_encoder_params('cpu', _ctx()))[0]
        self.assertNotIn('psy-rd=1.0:0.0', value)
        self.assertIn('psy-rd=1.0', value)
        self.assertIn('psy-rdoq=0.0', value)

    def test_every_pair_has_exactly_one_equals_sign(self):
        """参数串里每个冒号分段都必须是一个完整 key=value。"""
        value = _private_value(build_encoder_params('cpu', _ctx()))[0]
        for segment in value.split(':'):
            self.assertEqual(
                segment.count('='), 1,
                f'分段 {segment!r} 不是完整的 key=value，参数串被切碎了',
            )

    def test_boost_is_gated_the_same_way_for_both_software_codecs(self):
        for codec in ('x264', 'x265'):
            on = build_encoder_params('cpu', _ctx(cpu_codec=codec))
            off = build_encoder_params(
                'cpu', _ctx(cpu_codec=codec, hw_quality_boost=False)
            )
            self.assertNotEqual(on, off, codec)


class X265VuiMergeTests(unittest.TestCase):
    """x265 的 VUI 与质量增强必须落进同一条 -x265-params。"""

    _COLOR_MAP = {
        'colorspace': 'bt709',
        'color_primaries': 'bt709',
        'color_trc': 'bt709',
    }

    def test_vui_and_boost_share_one_option(self):
        params = build_encoder_params('cpu', _ctx(color_map=self._COLOR_MAP))
        values = _private_value(params)
        self.assertEqual(len(values), 1, '出现多条 -x265-params，后者会覆盖前者')
        self.assertIn('colorprim=bt709', values[0])
        self.assertIn('transfer=bt709', values[0])
        self.assertIn('colormatrix=bt709', values[0])
        self.assertIn('aq-mode=3', values[0])

    def test_vui_survives_with_boost_off(self):
        params = build_encoder_params(
            'cpu', _ctx(color_map=self._COLOR_MAP, hw_quality_boost=False)
        )
        values = _private_value(params)
        self.assertEqual(len(values), 1)
        self.assertIn('colorprim=bt709', values[0])
        self.assertNotIn('aq-mode', values[0])

    def test_x264_keeps_two_separate_options(self):
        """x264 的增强项是独立选项，与 -x264-params 不冲突，故仍可出现两条。"""
        params = build_encoder_params(
            'cpu', _ctx(cpu_codec='x264', color_map=self._COLOR_MAP)
        )
        self.assertEqual(len(_private_value(params, '-x264-params')), 1)
        self.assertIn('-aq-mode', params)

    def test_vui_absent_when_color_map_is_missing(self):
        params = build_encoder_params('cpu', _ctx(color_map=None))
        self.assertNotIn('colorprim=', ' '.join(params))

    def test_hardware_encoders_never_get_software_private_params(self):
        for key in ('nvidia', 'intel', 'amd'):
            params = build_encoder_params(key, _ctx(color_map=self._COLOR_MAP))
            joined = ' '.join(params)
            self.assertNotIn('-x265-params', joined, key)
            self.assertNotIn('-x264-params', joined, key)
            self.assertNotIn('colorprim=', joined, key)

    def test_custom_params_still_receive_the_vui(self):
        """与重构前一致：自定义参数不会被 VUI 追加逻辑排除。"""
        params = build_encoder_params(
            'cpu',
            _ctx(color_map=self._COLOR_MAP,
                 custom_params='-c:v libx265 -preset slow'),
        )
        self.assertEqual(params[0:4], ['-c:v', 'libx265', '-preset', 'slow'])
        self.assertIn('-x265-params', params)

    def test_custom_params_declaring_private_opts_suppress_the_vui(self):
        # `-x265-params` 与 `-x265-params=...` 两种拼写都要能识别。
        for declared in ('-x265-params', '-x265-params=x265-params'):
            params = build_encoder_params(
                'cpu',
                _ctx(color_map=self._COLOR_MAP,
                     custom_params=f'-c:v libx265 {declared} aq-mode=3'),
            )
            self.assertNotIn('colorprim=', ' '.join(params), declared)

    def test_x265_has_no_x265opts_alias(self):
        """x264 的 -x264opts 别名在 x265 侧不存在，不应被当成私有参数选项。"""
        params = build_encoder_params(
            'cpu',
            _ctx(color_map=self._COLOR_MAP, custom_params='-c:v libx265 -x265opts x'),
        )
        self.assertIn('-x265-params', params)

    def test_custom_params_without_color_map_are_returned_verbatim(self):
        custom = ['-c:v', 'libx265', '-preset', 'slow', '-crf', '20']
        params = build_encoder_params(
            'cpu', _ctx(color_map=None, custom_params=custom)
        )
        self.assertEqual(params, custom)


class X265VuiAliasTests(unittest.TestCase):
    def test_alias_output_uses_the_x265_option_name(self):
        params = build_color_vui_params(
            'cpu', {'color_trc': 'log'}, None, 'x265'
        )
        self.assertEqual(params, ['-x265-params', 'transfer=log100'])

    def test_default_cpu_codec_keeps_the_x264_option_name(self):
        params = build_color_vui_params('cpu', {'color_trc': 'log'})
        self.assertEqual(params, ['-x264-params', 'transfer=log100'])

    def test_values_without_an_x265_equivalent_are_skipped(self):
        for field, value in (('color_primaries', 'jedec-p22'),
                             ('color_primaries', 'ebu3213'),
                             ('color_trc', 'gamma22'),
                             ('color_trc', 'gamma28')):
            self.assertEqual(
                build_color_vui_params('cpu', {field: value}, None, 'x265'), [],
                f'{field}={value}',
            )

    def test_colormatrix_rgb_is_not_renamed_for_x265(self):
        """x265 直接接受 ffmpeg 规范名 rgb，与 x264 需要改写成 gbr 不同。"""
        params = build_color_vui_params(
            'cpu', {'colorspace': 'rgb'}, None, 'x265'
        )
        self.assertEqual(params, ['-x265-params', 'colormatrix=rgb'])
        x264_params = build_color_vui_params(
            'cpu', {'colorspace': 'rgb'}, None, 'x264'
        )
        self.assertEqual(x264_params, ['-x264-params', 'colormatrix=gbr'])

    def test_hardware_encoder_key_returns_empty_for_x265_too(self):
        for key in ('nvidia', 'intel', 'amd', 'auto', ' '):
            self.assertEqual(
                build_color_vui_params(key, {'color_trc': 'bt709'}, None, 'x265'), [],
                key,
            )

    def test_field_order_is_stable_for_x265(self):
        params = build_color_vui_params(
            'cpu',
            {'color_primaries': 'bt709', 'color_trc': 'smpte2084',
             'colorspace': 'bt2020nc'},
            None, 'x265',
        )
        self.assertEqual(
            params,
            ['-x265-params',
             'colorprim=bt709:transfer=smpte2084:colormatrix=bt2020nc'],
        )


class X265RobustnessTests(unittest.TestCase):
    def test_bad_ctx_never_raises(self):
        for bad in (None, [], 'ctx', 42, {'cpu_codec': object()}):
            params = build_encoder_params('cpu', bad)
            self.assertIsInstance(params, list)

    def test_hostile_color_map_never_raises(self):
        class Boom:
            def __eq__(self, other):
                raise RuntimeError('boom')

            def __hash__(self):
                raise RuntimeError('boom')

        for bad in (None, [], 'x', 3, {'color_trc': Boom()}, {'color_trc': None}):
            params = build_encoder_params('cpu', _ctx(color_map=bad))
            self.assertIsInstance(params, list)

    def test_unknown_encoder_key_with_x265_ctx_falls_back_to_libx265(self):
        params = build_encoder_params('mystery', _ctx())
        self.assertEqual(params[0:2], ['-c:v', 'libx265'])


if __name__ == '__main__':
    unittest.main()
