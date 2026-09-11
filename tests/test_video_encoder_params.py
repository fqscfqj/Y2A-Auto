"""video_encoder_params 单元测试。

覆盖质量推荐边界、配置解析宽容性、色彩元数据透传、四个编码器的完整参数
列表（逐项断言以锁定顺序与完整性）、自定义参数完全覆盖、音频码率阶梯，
以及所有公开函数在非法输入下不抛异常。不依赖网络、FFmpeg 或外部服务。
"""

import unittest

from modules.video_encoder_params import (
    DEFAULT_QUALITY_BY_HEIGHT,
    FALLBACK_QUALITY,
    build_audio_params,
    build_encoder_params,
    format_quality_value,
    parse_encoder_config,
    recommend_quality,
    resolve_color_metadata,
)

# 1080p 推荐质量 23.5：libx264/-cq 用 float 字符串，QSV/AMF/VAAPI 用 int(round(q))。
# int(round(23.5)) == 24 —— 与历史 max(0, min(51, int(round(target_quality)))) 一致。
_Q_1080_INT = '24'

_CPU_1080_EXPECTED = [
    '-c:v', 'libx264',
    '-preset', 'medium',
    '-crf', '23.5',
    '-fps_mode', 'cfr',
    '-profile:v', 'high',
    '-bf', '2',
    '-g', '48',
    '-pix_fmt', 'yuv420p',
    '-aq-mode', '3',
    '-aq-strength', '0.8',
    '-psy-rd', '1.0:0.0',
    '-rc-lookahead', '40',
]

_NVENC_1080_BASE = [
    '-c:v', 'hevc_nvenc',
    '-preset', 'p7',
    '-tune', 'hq',
    '-rc:v', 'vbr',
    '-b:v', '0',
    '-cq:v', '23.5',
    '-fps_mode', 'cfr',
    '-profile:v', 'main',
    '-bf', '2',
    '-g', '96',
    '-pix_fmt', 'yuv420p',
    '-tag:v', 'hvc1',
]

_NVENC_1080_BOOST = [
    '-rc-lookahead', '32',
    '-multipass', 'qres',
    '-spatial-aq', '1',
    '-temporal-aq', '1',
    '-aq-strength', '8',
    '-b_ref_mode', 'middle',
]

_QSV_1080_BASE = [
    '-c:v', 'hevc_qsv',
    '-preset', 'veryslow',
    '-global_quality', _Q_1080_INT,
    '-fps_mode', 'cfr',
    '-profile:v', 'main',
    '-bf', '2',
    '-g', '96',
    '-pix_fmt', 'nv12',
    '-tag:v', 'hvc1',
]

_QSV_1080_BOOST = [
    '-extbrc', '1',
    '-look_ahead_depth', '40',
    '-mbbrc', '1',
    '-rdo', '1',
    '-scenario', 'archive',
]

_AMF_1080_BASE = [
    '-c:v', 'hevc_amf',
    '-usage', 'transcoding',
    '-quality', 'quality',
    '-rc', 'qvbr',
    '-qvbr_quality_level', _Q_1080_INT,
    '-fps_mode', 'cfr',
    '-profile:v', 'main',
    '-g', '96',
    '-pix_fmt', 'yuv420p',
    '-tag:v', 'hvc1',
]

_VAAPI_1080_BASE = [
    '-vaapi_device', '/dev/dri/renderD128',
    '-c:v', 'hevc_vaapi',
    '-rc_mode', 'CQP',
    '-qp', _Q_1080_INT,
    '-fps_mode', 'cfr',
    '-profile:v', 'main',
    '-g', '96',
    '-tag:v', 'hvc1',
]

_ENHANCEMENT_TOKENS = (
    '-rc-lookahead', '-multipass', '-spatial-aq', '-temporal-aq', '-aq-strength',
    '-b_ref_mode', '-extbrc', '-look_ahead_depth', '-mbbrc', '-rdo', '-scenario',
    '-blbrc', '-vbaq', '-preanalysis', '-pa_caq_strength',
)


def _ctx(**overrides):
    """构造一份完整的合法 ctx，并按需覆盖字段。"""
    base = {
        'height': 1080,
        'gop': 48,
        'gop_hevc': 96,
        'quality_mode': 'auto',
        'quality_value': None,
        'cpu_preset': 'medium',
        'cpu_preset_hd': 'veryfast',
        'duration_s': None,
        'hw_quality_boost': True,
        'hw_quality_level': 'quality',
        'x264_tune': '',
        'custom_params': None,
        'amd_backend': 'amf',
    }
    base.update(overrides)
    return base


class RecommendQualityTests(unittest.TestCase):
    def test_height_boundaries_match_legacy_table(self):
        cases = (
            (2160, 22.5),
            (4320, 22.5),
            (2159, 23.0),
            (1440, 23.0),
            (1439, 23.5),
            (1080, 23.5),
            (1079, 24.5),
            (720, 24.5),
            (719, FALLBACK_QUALITY),
            (480, FALLBACK_QUALITY),
            (1, FALLBACK_QUALITY),
        )
        for height, expected in cases:
            self.assertEqual(recommend_quality(height), expected, f'height={height}')

    def test_table_constant_is_unchanged(self):
        self.assertEqual(
            DEFAULT_QUALITY_BY_HEIGHT,
            ((2160, 22.5), (1440, 23.0), (1080, 23.5), (720, 24.5)),
        )
        self.assertEqual(FALLBACK_QUALITY, 25.5)

    def test_invalid_height_falls_back(self):
        for bad in (None, 0, -1, -1080, '', 'abc', [], {}, float('nan'), float('inf'), True, False):
            self.assertEqual(
                recommend_quality(bad), FALLBACK_QUALITY, f'bad={bad!r}'
            )

    def test_numeric_string_and_float_height_are_accepted(self):
        self.assertEqual(recommend_quality('1080'), 23.5)
        self.assertEqual(recommend_quality(1080.0), 23.5)
        self.assertEqual(recommend_quality('2160'), 22.5)

    def test_return_type_is_float(self):
        self.assertIsInstance(recommend_quality(1080), float)
        self.assertIsInstance(recommend_quality(None), float)


class ParseEncoderConfigTests(unittest.TestCase):
    def test_defaults_for_empty_config(self):
        parsed = parse_encoder_config({})
        self.assertEqual(parsed, {
            'encoder_pref': 'auto',
            'cpu_preset': 'medium',
            'cpu_preset_hd': 'veryfast',
            'quality_mode': 'auto',
            'quality_value': None,
            'hw_quality_boost': True,
            'hw_quality_level': 'quality',
            'color_metadata_mode': 'auto',
            'custom_params_enabled': False,
            'custom_params': '',
            'x264_tune': '',
        })

    def test_none_and_non_dict_config(self):
        for bad in (None, [], 'config', 42, object()):
            self.assertEqual(parse_encoder_config(bad), parse_encoder_config({}))

    def test_invalid_enum_values_fall_back(self):
        parsed = parse_encoder_config({
            'VIDEO_ENCODER': 'NVIDIA-GPU',
            'VIDEO_CPU_PRESET': 'insane',
            'VIDEO_CPU_PRESET_HD': '',
            'VIDEO_QUALITY_MODE': 'half-auto',
            'VIDEO_HW_QUALITY_LEVEL': 'ultra',
            'VIDEO_COLOR_METADATA_MODE': 'srgb',
            'VIDEO_X264_TUNE': 'none',
        })
        self.assertEqual(parsed['encoder_pref'], 'auto')
        self.assertEqual(parsed['cpu_preset'], 'medium')
        self.assertEqual(parsed['cpu_preset_hd'], 'veryfast')
        self.assertEqual(parsed['quality_mode'], 'auto')
        self.assertEqual(parsed['hw_quality_level'], 'quality')
        self.assertEqual(parsed['color_metadata_mode'], 'auto')
        self.assertEqual(parsed['x264_tune'], '')

    def test_case_insensitive_enum_and_tune(self):
        parsed = parse_encoder_config({
            'VIDEO_ENCODER': 'NVENC-DUMMY-NO',
            'VIDEO_CPU_PRESET': 'VERYFAST',
            'VIDEO_QUALITY_MODE': 'Manual',
            'VIDEO_HW_QUALITY_LEVEL': 'BALANCED',
            'VIDEO_COLOR_METADATA_MODE': 'BT709',
            'VIDEO_X264_TUNE': 'FILM',
        })
        self.assertEqual(parsed['cpu_preset'], 'veryfast')
        self.assertEqual(parsed['quality_mode'], 'manual')
        self.assertEqual(parsed['hw_quality_level'], 'balanced')
        self.assertEqual(parsed['color_metadata_mode'], 'bt709')
        self.assertEqual(parsed['x264_tune'], 'film')

    def test_valid_encoder_values(self):
        for value in ('auto', 'cpu', 'nvidia', 'intel', 'amd', 'CPU', 'AMD'):
            parsed = parse_encoder_config({'VIDEO_ENCODER': value})
            self.assertEqual(parsed['encoder_pref'], value.strip().lower())

    def test_quality_value_clamp(self):
        self.assertEqual(parse_encoder_config({'VIDEO_QUALITY_VALUE': 23.5})['quality_value'], 23.5)
        self.assertEqual(parse_encoder_config({'VIDEO_QUALITY_VALUE': '18'})['quality_value'], 18.0)
        self.assertEqual(parse_encoder_config({'VIDEO_QUALITY_VALUE': 99})['quality_value'], 51.0)
        self.assertEqual(parse_encoder_config({'VIDEO_QUALITY_VALUE': -5})['quality_value'], 0.0)

    def test_quality_value_invalid_returns_none(self):
        for bad in (None, '', 'abc', [], {}, True, float('nan')):
            parsed = parse_encoder_config({'VIDEO_QUALITY_VALUE': bad})
            self.assertIsNone(parsed['quality_value'], f'bad={bad!r}')

    def test_bool_parsing_is_lenient(self):
        for truthy in ('true', 'TRUE', '1', 'yes', 'On', 1, 2, True):
            parsed = parse_encoder_config({'VIDEO_HW_QUALITY_BOOST': truthy})
            self.assertIs(parsed['hw_quality_boost'], True, f'value={truthy!r}')
        for falsy in ('false', 'FALSE', '0', 'no', 'oFF', 0, False):
            parsed = parse_encoder_config({'VIDEO_HW_QUALITY_BOOST': falsy})
            self.assertIs(parsed['hw_quality_boost'], False, f'value={falsy!r}')

    def test_bool_invalid_value_uses_default_not_bool_str(self):
        # 关键回归：'false' 绝不能被 bool(str) 解析成 True。
        self.assertIs(parse_encoder_config({'VIDEO_HW_QUALITY_BOOST': 'false'})['hw_quality_boost'], False)
        self.assertIs(parse_encoder_config({'VIDEO_CUSTOM_PARAMS_ENABLED': 'false'})['custom_params_enabled'], False)
        # 非法值回退各自默认：BOOST 默认 True、ENABLED 默认 False。
        for bad in ('maybe', '', [], {}):
            self.assertIs(parse_encoder_config({'VIDEO_HW_QUALITY_BOOST': bad})['hw_quality_boost'], True)
            self.assertIs(parse_encoder_config({'VIDEO_CUSTOM_PARAMS_ENABLED': bad})['custom_params_enabled'], False)

    def test_custom_params_text_is_stripped(self):
        parsed = parse_encoder_config({'VIDEO_CUSTOM_PARAMS': '  -c:v libx264  '})
        self.assertEqual(parsed['custom_params'], '-c:v libx264')

    def test_never_raises_on_hostile_config(self):
        class _Boom:
            def __str__(self):
                raise RuntimeError('boom')

            def __eq__(self, other):
                raise RuntimeError('boom')

            def __hash__(self):
                raise RuntimeError('boom')

        hostile = {key: _Boom() for key in (
            'VIDEO_ENCODER', 'VIDEO_CPU_PRESET', 'VIDEO_CPU_PRESET_HD',
            'VIDEO_QUALITY_MODE', 'VIDEO_QUALITY_VALUE', 'VIDEO_HW_QUALITY_BOOST',
            'VIDEO_HW_QUALITY_LEVEL', 'VIDEO_COLOR_METADATA_MODE',
            'VIDEO_CUSTOM_PARAMS_ENABLED', 'VIDEO_CUSTOM_PARAMS', 'VIDEO_X264_TUNE',
        )}
        parsed = parse_encoder_config(hostile)
        self.assertEqual(parsed['encoder_pref'], 'auto')
        self.assertEqual(parsed['quality_value'], None)


class ResolveColorMetadataTests(unittest.TestCase):
    def test_off_returns_empty(self):
        self.assertEqual(resolve_color_metadata('off', {'color_space': 'bt709'}), [])
        self.assertEqual(resolve_color_metadata('OFF', None), [])

    def test_bt709_forces_full_tag_set(self):
        self.assertEqual(
            resolve_color_metadata('bt709', {}),
            [
                '-colorspace', 'bt709',
                '-color_primaries', 'bt709',
                '-color_trc', 'bt709',
                '-color_range', 'tv',
            ],
        )

    def test_auto_passthrough_normalizes_case_and_range(self):
        info = {
            'color_space': 'BT709',
            'color_primaries': 'Bt2020',
            'color_transfer': 'SMPTE2084',
            'color_range': 'FULL',
        }
        self.assertEqual(
            resolve_color_metadata('auto', info),
            [
                '-colorspace', 'bt709',
                '-color_primaries', 'bt2020',
                '-color_trc', 'smpte2084',
                '-color_range', 'pc',
            ],
        )

    def test_auto_maps_limited_and_full_to_tv_and_pc(self):
        self.assertEqual(
            resolve_color_metadata('auto', {'color_range': 'limited'}),
            ['-color_range', 'tv'],
        )
        self.assertEqual(
            resolve_color_metadata('auto', {'color_range': 'full'}),
            ['-color_range', 'pc'],
        )
        self.assertEqual(
            resolve_color_metadata('auto', {'color_range': 'tv'}),
            ['-color_range', 'tv'],
        )

    def test_auto_skips_unknown_none_and_missing(self):
        skipped = ('unknown', 'UNKNOWN', 'unspecified', 'reserved', '', None, 'n/a', 123)
        for token in skipped:
            self.assertEqual(
                resolve_color_metadata('auto', {
                    'color_space': token,
                    'color_primaries': token,
                    'color_transfer': token,
                    'color_range': token,
                }),
                [],
                f'token={token!r}',
            )
        self.assertEqual(resolve_color_metadata('auto', {}), [])
        self.assertEqual(resolve_color_metadata('auto', None), [])
        self.assertEqual(resolve_color_metadata('auto', 'bt709'), [])

    def test_auto_partial_fields(self):
        self.assertEqual(
            resolve_color_metadata('auto', {'color_space': 'bt470bg', 'color_transfer': 'bogus'}),
            ['-colorspace', 'bt470bg'],
        )
        self.assertEqual(
            resolve_color_metadata('auto', {'color_transfer': 'arib-std-b67'}),
            ['-color_trc', 'arib-std-b67'],
        )

    def test_fixed_ordering(self):
        params = resolve_color_metadata('auto', {
            'color_range': 'tv',
            'color_transfer': 'bt709',
            'color_primaries': 'bt709',
            'color_space': 'bt709',
        })
        self.assertEqual(params, [
            '-colorspace', 'bt709',
            '-color_primaries', 'bt709',
            '-color_trc', 'bt709',
            '-color_range', 'tv',
        ])
        self.assertIsInstance(params, list)

    def test_invalid_mode_falls_back_to_auto(self):
        info = {'color_space': 'bt709'}
        self.assertEqual(
            resolve_color_metadata('bogus', info),
            resolve_color_metadata('auto', info),
        )
        self.assertEqual(
            resolve_color_metadata(None, info),
            resolve_color_metadata('auto', info),
        )


class BuildEncoderParamsCpuTests(unittest.TestCase):
    def test_cpu_full_param_list_1080p(self):
        self.assertEqual(build_encoder_params('cpu', _ctx()), _CPU_1080_EXPECTED)

    def test_cpu_never_emits_vsync(self):
        self.assertNotIn('-vsync', build_encoder_params('cpu', _ctx()))

    def test_cpu_x264_tune_inserted_after_preset(self):
        params = build_encoder_params('cpu', _ctx(**{'x264_tune': 'film'}))
        self.assertEqual(params[:5], ['-c:v', 'libx264', '-preset', 'medium', '-tune'])
        self.assertEqual(
            params,
            [
                '-c:v', 'libx264',
                '-preset', 'medium',
                '-tune', 'film',
                '-crf', '23.5',
                '-fps_mode', 'cfr',
                '-profile:v', 'high',
                '-bf', '2',
                '-g', '48',
                '-pix_fmt', 'yuv420p',
                '-aq-mode', '3',
                '-aq-strength', '0.8',
                '-psy-rd', '1.0:0.0',
                '-rc-lookahead', '40',
            ],
        )

    def test_cpu_invalid_tune_ignored(self):
        for bad in ('bogus', 'none', '', None, 42, ['film']):
            self.assertNotIn('-tune', build_encoder_params('cpu', _ctx(**{'x264_tune': bad})))

    def test_cpu_hd_preset_only_for_long_high_resolution(self):
        long_1080p = build_encoder_params('cpu', _ctx(**{'height': 1080, 'duration_s': 3600}))
        self.assertEqual(long_1080p[3], 'medium')

        long_1440p = build_encoder_params('cpu', _ctx(**{'height': 1440, 'duration_s': 601}))
        self.assertEqual(long_1440p[3], 'veryfast')

        boundary = build_encoder_params('cpu', _ctx(**{'height': 1440, 'duration_s': 600}))
        self.assertEqual(boundary[3], 'medium')

        short_4k = build_encoder_params('cpu', _ctx(**{'height': 2160, 'duration_s': 300}))
        self.assertEqual(short_4k[3], 'medium')

        no_duration = build_encoder_params('cpu', _ctx(**{'height': 2160, 'duration_s': None}))
        self.assertEqual(no_duration[3], 'medium')

    def test_cpu_quality_by_height(self):
        self.assertEqual(
            build_encoder_params('cpu', _ctx(**{'height': 2160}))[5], '22.5'
        )
        self.assertEqual(
            build_encoder_params('cpu', _ctx(**{'height': 1440}))[5], '23'
        )
        self.assertEqual(
            build_encoder_params('cpu', _ctx(**{'height': 720}))[5], '24.5'
        )
        self.assertEqual(
            build_encoder_params('cpu', _ctx(**{'height': 480}))[5], '25.5'
        )

    def test_cpu_gop_follows_ctx(self):
        params = build_encoder_params('cpu', _ctx(**{'gop': 240}))
        self.assertEqual(params[params.index('-g') + 1], '240')

    def test_unknown_encoder_key_falls_back_to_cpu(self):
        expected = build_encoder_params('cpu', _ctx())
        for key in ('bogus', '', None, 42, 'CPU', ' cpu '):
            self.assertEqual(build_encoder_params(key, _ctx()), expected, f'key={key!r}')


class BuildEncoderParamsNvidiaTests(unittest.TestCase):
    def test_nvenc_full_param_list_with_boost(self):
        expected = _NVENC_1080_BASE + _NVENC_1080_BOOST
        self.assertEqual(build_encoder_params('nvidia', _ctx()), expected)

    def test_nvenc_temporal_aq_paired_with_spatial_aq(self):
        params = build_encoder_params('nvidia', _ctx())
        self.assertIn('-spatial-aq', params)
        self.assertIn('-temporal-aq', params)
        self.assertEqual(params[params.index('-spatial-aq') + 1], '1')
        self.assertEqual(params[params.index('-temporal-aq') + 1], '1')

    def test_nvenc_boost_disabled_drops_all_enhancements(self):
        params = build_encoder_params('nvidia', _ctx(**{'hw_quality_boost': False}))
        self.assertEqual(params, _NVENC_1080_BASE)
        for token in _ENHANCEMENT_TOKENS:
            self.assertNotIn(token, params, f'unexpected {token}')

    def test_nvenc_preset_by_quality_level(self):
        for level, preset in (('fast', 'p4'), ('balanced', 'p5'), ('quality', 'p7')):
            params = build_encoder_params('nvidia', _ctx(**{'hw_quality_level': level}))
            self.assertEqual(params[params.index('-preset') + 1], preset, level)

    def test_nvenc_invalid_quality_level_defaults_to_p7(self):
        params = build_encoder_params('nvidia', _ctx(**{'hw_quality_level': 'ultra'}))
        self.assertEqual(params[params.index('-preset') + 1], 'p7')

    def test_nvenc_manual_quality_used_as_float_string(self):
        params = build_encoder_params(
            'nvidia', _ctx(**{'quality_mode': 'manual', 'quality_value': 18})
        )
        self.assertEqual(params[params.index('-cq:v') + 1], '18')

    def test_nvenc_manual_quality_clamped(self):
        params = build_encoder_params(
            'nvidia', _ctx(**{'quality_mode': 'manual', 'quality_value': 999})
        )
        self.assertEqual(params[params.index('-cq:v') + 1], '51')
        params = build_encoder_params(
            'nvidia', _ctx(**{'quality_mode': 'manual', 'quality_value': -10})
        )
        self.assertEqual(params[params.index('-cq:v') + 1], '0')

    def test_nvenc_manual_invalid_quality_falls_back_to_recommendation(self):
        for bad in (None, 'abc', [], {}, float('nan')):
            params = build_encoder_params(
                'nvidia', _ctx(**{'quality_mode': 'manual', 'quality_value': bad})
            )
            self.assertEqual(params[params.index('-cq:v') + 1], '23.5', f'bad={bad!r}')

    def test_nvenc_uses_hevc_gop(self):
        params = build_encoder_params('nvidia', _ctx(**{'gop': 48, 'gop_hevc': 120}))
        self.assertEqual(params[params.index('-g') + 1], '120')


class BuildEncoderParamsIntelTests(unittest.TestCase):
    def test_qsv_full_param_list_with_boost(self):
        expected = _QSV_1080_BASE + _QSV_1080_BOOST
        self.assertEqual(build_encoder_params('intel', _ctx()), expected)

    def test_qsv_boost_disabled_keeps_legacy_look_ahead_zero(self):
        params = build_encoder_params('intel', _ctx(**{'hw_quality_boost': False}))
        self.assertEqual(params, _QSV_1080_BASE + ['-look_ahead', '0'])
        self.assertEqual(params[params.index('-look_ahead') + 1], '0')
        for token in ('-extbrc', '-look_ahead_depth', '-mbbrc', '-rdo', '-scenario'):
            self.assertNotIn(token, params, f'unexpected {token}')

    def test_qsv_preset_by_quality_level(self):
        for level, preset in (('fast', 'veryfast'), ('balanced', 'medium'), ('quality', 'veryslow')):
            params = build_encoder_params('intel', _ctx(**{'hw_quality_level': level}))
            self.assertEqual(params[params.index('-preset') + 1], preset, level)

    def test_qsv_quality_is_rounded_integer(self):
        for height, expected in ((2160, '22'), (1440, '23'), (1080, '24'), (720, '24'), (480, '26')):
            params = build_encoder_params('intel', _ctx(**{'height': height}))
            self.assertEqual(
                params[params.index('-global_quality') + 1], expected, f'height={height}'
            )

    def test_qsv_manual_quality_rounded(self):
        params = build_encoder_params(
            'intel', _ctx(**{'quality_mode': 'manual', 'quality_value': 22.6})
        )
        self.assertEqual(params[params.index('-global_quality') + 1], '23')

    def test_qsv_pix_fmt_nv12(self):
        params = build_encoder_params('intel', _ctx())
        self.assertEqual(params[params.index('-pix_fmt') + 1], 'nv12')


class BuildEncoderParamsAmdTests(unittest.TestCase):
    def test_amf_full_param_list_with_boost(self):
        expected = [
            '-c:v', 'hevc_amf',
            '-usage', 'transcoding',
            '-quality', 'quality',
            '-rc', 'hqvbr',
            '-qvbr_quality_level', _Q_1080_INT,
            '-fps_mode', 'cfr',
            '-profile:v', 'main',
            '-g', '96',
            '-pix_fmt', 'yuv420p',
            '-tag:v', 'hvc1',
            '-vbaq', '1',
            '-preanalysis', '1',
            '-pa_caq_strength', 'high',
        ]
        self.assertEqual(
            build_encoder_params('amd', _ctx(**{'amd_backend': 'amf'})), expected
        )

    def test_amf_boost_disabled_uses_legacy_qvbr(self):
        params = build_encoder_params(
            'amd', _ctx(**{'amd_backend': 'amf', 'hw_quality_boost': False})
        )
        self.assertEqual(params, _AMF_1080_BASE)
        for token in ('-vbaq', '-preanalysis', '-pa_caq_strength'):
            self.assertNotIn(token, params, f'unexpected {token}')

    def test_amf_quality_tier_by_level(self):
        for level, quality in (('fast', 'speed'), ('balanced', 'balanced'), ('quality', 'quality')):
            params = build_encoder_params(
                'amd', _ctx(**{'amd_backend': 'amf', 'hw_quality_level': level})
            )
            self.assertEqual(params[params.index('-quality') + 1], quality, level)

    def test_unknown_or_none_backend_falls_back_to_amf(self):
        expected = build_encoder_params('amd', _ctx(**{'amd_backend': 'amf'}))
        for backend in ('none', 'bogus', None, 42, ''):
            self.assertEqual(
                build_encoder_params('amd', _ctx(**{'amd_backend': backend})),
                expected,
                f'backend={backend!r}',
            )

    def test_vaapi_full_param_list_with_boost(self):
        expected = [
            '-vaapi_device', '/dev/dri/renderD128',
            '-c:v', 'hevc_vaapi',
            '-rc_mode', 'CQP',
            '-qp', _Q_1080_INT,
            '-fps_mode', 'cfr',
            '-profile:v', 'main',
            '-g', '96',
            '-tag:v', 'hvc1',
            '-blbrc', '1',
        ]
        self.assertEqual(
            build_encoder_params('amd', _ctx(**{'amd_backend': 'vaapi'})), expected
        )

    def test_vaapi_boost_disabled_drops_blbrc(self):
        params = build_encoder_params(
            'amd', _ctx(**{'amd_backend': 'vaapi', 'hw_quality_boost': False})
        )
        self.assertEqual(params, _VAAPI_1080_BASE)
        self.assertNotIn('-blbrc', params)

    def test_vaapi_never_emits_pix_fmt_and_has_no_bf(self):
        for boost in (True, False):
            params = build_encoder_params(
                'amd', _ctx(**{'amd_backend': 'vaapi', 'hw_quality_boost': boost})
            )
            self.assertNotIn('-pix_fmt', params)
            self.assertNotIn('-bf', params)
            self.assertIn('-tag:v', params)
            self.assertIn('-profile:v', params)


class CustomParamsOverrideTests(unittest.TestCase):
    def test_non_empty_custom_params_returned_as_is(self):
        custom = ['-c:v', 'libx264', '-preset', 'slow', '-crf', '18', '-tune', 'film']
        for key, backend in (
            ('cpu', 'amf'), ('nvidia', 'amf'), ('intel', 'vaapi'), ('amd', 'amf'),
            ('bogus', 'vaapi'),
        ):
            self.assertEqual(
                build_encoder_params(key, _ctx(**{'custom_params': custom, 'amd_backend': backend})),
                custom,
            )

    def test_custom_params_fully_override_builtin_params(self):
        custom = ['-c:v', 'hevc_nvenc', '-cq', '30']
        params = build_encoder_params('nvidia', _ctx(**{'custom_params': custom}))
        self.assertEqual(params, custom)
        for token in _ENHANCEMENT_TOKENS:
            self.assertNotIn(token, params, f'unexpected {token}')
        self.assertNotIn('-fps_mode', params)

    def test_empty_custom_params_do_not_override(self):
        expected_cpu = build_encoder_params('cpu', _ctx())
        for empty in (None, [], (), '', '   '):
            self.assertEqual(
                build_encoder_params('cpu', _ctx(**{'custom_params': empty})),
                expected_cpu,
                f'empty={empty!r}',
            )

    def test_string_custom_params_are_split(self):
        params = build_encoder_params(
            'cpu', _ctx(**{'custom_params': '-c:v libx264 -preset slow -crf 20'})
        )
        self.assertEqual(params, ['-c:v', 'libx264', '-preset', 'slow', '-crf', '20'])

    def test_unparsable_string_custom_params_ignored(self):
        params = build_encoder_params('cpu', _ctx(**{'custom_params': '"-c:v'}))
        self.assertNotEqual(params, ['"-c:v'])
        self.assertIn('libx264', params)


class FormatQualityValueTests(unittest.TestCase):
    def test_integers_have_no_decimal(self):
        self.assertEqual(format_quality_value(23.0), '23')
        self.assertEqual(format_quality_value(23), '23')
        self.assertEqual(format_quality_value('25'), '25')
        self.assertEqual(format_quality_value(0), '0')
        self.assertEqual(format_quality_value(51.0), '51')

    def test_fraction_keeps_at_most_two_decimals(self):
        self.assertEqual(format_quality_value(23.5), '23.5')
        self.assertEqual(format_quality_value(22.5), '22.5')
        self.assertEqual(format_quality_value(24.5), '24.5')
        self.assertEqual(format_quality_value(23.456), '23.46')
        self.assertEqual(format_quality_value(23.5), '23.5')

    def test_trailing_zeros_are_trimmed(self):
        self.assertEqual(format_quality_value('23.50'), '23.5')
        self.assertEqual(format_quality_value(23.5000), '23.5')

    def test_non_numeric_is_never_fatal(self):
        self.assertEqual(format_quality_value(None), '')
        self.assertEqual(format_quality_value('abc'), 'abc')
        self.assertEqual(format_quality_value([]), '[]')
        self.assertEqual(format_quality_value(True), 'True')


class BuildAudioParamsTests(unittest.TestCase):
    def test_aac_is_copied(self):
        self.assertEqual(build_audio_params({'codec_name': 'aac'}), ['-c:a', 'copy'])
        self.assertEqual(build_audio_params({'codec_name': 'AAC'}), ['-c:a', 'copy'])
        self.assertEqual(build_audio_params({'codec_name': ' aac '}), ['-c:a', 'copy'])

    def test_non_aac_transcodes_with_channels_and_rate(self):
        self.assertEqual(
            build_audio_params({
                'codec_name': 'opus',
                'bit_rate': 200000,
                'channels': 2,
                'sample_rate': 48000,
            }),
            ['-c:a', 'aac', '-b:a', '192k', '-ac', '2', '-ar', '48000'],
        )

    def test_mono_channel_maps_to_single_ac(self):
        self.assertEqual(
            build_audio_params({'codec_name': 'vorbis', 'channels': 1, 'sample_rate': 44100}),
            ['-c:a', 'aac', '-b:a', '128k', '-ac', '1', '-ar', '44100'],
        )
        self.assertEqual(
            build_audio_params({'codec_name': 'vorbis', 'channels': '1'}),
            ['-c:a', 'aac', '-b:a', '128k', '-ac', '1'],
        )

    def test_missing_channels_defaults_to_stereo(self):
        self.assertEqual(
            build_audio_params({'codec_name': 'vorbis'}),
            ['-c:a', 'aac', '-b:a', '128k', '-ac', '2'],
        )
        self.assertEqual(
            build_audio_params({'codec_name': 'vorbis', 'channels': 0}),
            ['-c:a', 'aac', '-b:a', '128k', '-ac', '2'],
        )

    def test_bitrate_ladder(self):
        ladder = (
            (192000, '192k'),
            (320000, '192k'),
            (191999, '160k'),
            (160000, '160k'),
            (159999, '128k'),
            (128000, '128k'),
            (127999, '96k'),
            (96000, '96k'),
            (95999, '64k'),
            (64000, '64k'),
        )
        for bit_rate, expected in ladder:
            params = build_audio_params({'codec_name': 'vorbis', 'bit_rate': bit_rate})
            self.assertEqual(params[params.index('-b:a') + 1], expected, f'bit_rate={bit_rate}')

    def test_invalid_bitrate_falls_back_to_128k(self):
        for bad in (None, '', 'abc', 0, -1, [], {}, float('nan')):
            params = build_audio_params({'codec_name': 'vorbis', 'bit_rate': bad})
            self.assertEqual(params[params.index('-b:a') + 1], '128k', f'bad={bad!r}')

    def test_sample_rate_omitted_when_missing_or_invalid(self):
        for bad in (None, '', 'abc', 0, [], {}):
            params = build_audio_params({'codec_name': 'vorbis', 'sample_rate': bad})
            self.assertNotIn('-ar', params, f'bad={bad!r}')
        params = build_audio_params({'codec_name': 'vorbis', 'sample_rate': '48000'})
        self.assertEqual(params[params.index('-ar') + 1], '48000')

    def test_non_dict_audio_info(self):
        for bad in (None, [], 'aac', 42, object()):
            params = build_audio_params(bad)
            self.assertEqual(params[:3], ['-c:a', 'aac', '-b:a'])


class RobustnessTests(unittest.TestCase):
    def test_build_encoder_params_tolerates_bad_ctx(self):
        for bad_ctx in (None, [], 'ctx', 42, object(), (), {'height': 'tall'}, {'gop': 'x'}):
            for key in ('cpu', 'nvidia', 'intel', 'amd', None, 'bogus'):
                params = build_encoder_params(key, bad_ctx)
                self.assertIsInstance(params, list)
                self.assertTrue(params)
                self.assertIn('-c:v', params)

    def test_default_ctx_values(self):
        params = build_encoder_params('cpu', {})
        self.assertEqual(params[params.index('-preset') + 1], 'medium')
        self.assertEqual(params[params.index('-crf') + 1], '23.5')
        self.assertEqual(params[params.index('-g') + 1], '48')

        nvenc = build_encoder_params('nvidia', {})
        self.assertEqual(nvenc[nvenc.index('-preset') + 1], 'p7')
        self.assertEqual(nvenc[nvenc.index('-g') + 1], '96')
        self.assertIn('-temporal-aq', nvenc)

    def test_invalid_gop_values_use_defaults(self):
        for bad in (0, -5, None, 'x', [], {}):
            cpu = build_encoder_params('cpu', _ctx(**{'gop': bad, 'gop_hevc': bad}))
            self.assertEqual(cpu[cpu.index('-g') + 1], '48', f'gop={bad!r}')
            nvenc = build_encoder_params('nvidia', _ctx(**{'gop': bad, 'gop_hevc': bad}))
            self.assertEqual(nvenc[nvenc.index('-g') + 1], '96', f'gop_hevc={bad!r}')

    def test_invalid_height_uses_1080_default(self):
        for bad in (0, -1, None, 'x', [], {}):
            params = build_encoder_params('cpu', _ctx(**{'height': bad}))
            self.assertEqual(params[params.index('-crf') + 1], '23.5', f'height={bad!r}')

    def test_all_branches_start_with_codec_and_never_contain_vsync(self):
        cases = (
            ('cpu', {'amd_backend': 'amf'}),
            ('nvidia', {'amd_backend': 'amf'}),
            ('intel', {'amd_backend': 'vaapi'}),
            ('amd', {'amd_backend': 'amf'}),
            ('amd', {'amd_backend': 'vaapi'}),
        )
        for key, overrides in cases:
            params = build_encoder_params(key, _ctx(**overrides))
            self.assertIn('-c:v', params)
            self.assertNotIn('-vsync', params)
            self.assertIn('-fps_mode', params)
            self.assertEqual(params[params.index('-fps_mode') + 1], 'cfr')
            # -fps_mode 必须出现在 -profile:v 之前
            self.assertLess(params.index('-fps_mode'), params.index('-profile:v'))

    def test_vsync_is_never_emitted_anywhere(self):
        for key, overrides in (
            ('cpu', {}), ('nvidia', {}), ('intel', {}),
            ('amd', {'amd_backend': 'amf'}), ('amd', {'amd_backend': 'vaapi'}),
        ):
            for boost in (True, False):
                params = build_encoder_params(key, _ctx(hw_quality_boost=boost, **overrides))
                self.assertNotIn('-vsync', params)


if __name__ == '__main__':
    unittest.main()
