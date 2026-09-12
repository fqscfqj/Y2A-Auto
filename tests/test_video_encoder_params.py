"""video_encoder_params 单元测试。

覆盖质量推荐边界、配置解析宽容性、色彩元数据透传、四个编码器的完整参数
列表（逐项断言以锁定顺序与完整性）、自定义参数完全覆盖、音频码率阶梯、
质量增强总开关（hw_quality_boost）在各编码器上的对称行为、x264 私有参数
的色彩值改名映射，以及所有公开函数在非法输入下不抛异常。
不依赖网络、FFmpeg 或外部服务。

另有一组 `_LEGACY_*` 快照，硬编码 origin/main 上 task_manager 内联的编码参数
列表，用于抓住「默认行为悄悄偏离基线」的回归（例如 `-vsync` 改名、AMF 默认
档位由 `balanced` 变 `quality`）。真正调用 ffmpeg 的冒烟验证在
tests/test_color_metadata_smoke.py。
"""

import unittest

from modules.video_encoder_params import (
    DEFAULT_QUALITY_BY_HEIGHT,
    FALLBACK_QUALITY,
    build_audio_params,
    build_color_vui_params,
    build_encoder_params,
    format_quality_value,
    parse_encoder_config,
    recommend_quality,
    resolve_color_metadata,
)


class _Logger:
    """最小 logger stub：只记录消息，用于断言「丢弃字段时有提示」。"""

    def __init__(self):
        self.messages = []
        self.warnings = []

    def warning(self, message, *args):
        text = message % args if args else message
        self.messages.append(str(text))
        self.warnings.append(str(text))

    def info(self, message, *args):
        self.messages.append(str(message % args if args else message))

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

# CPU 的增强项（四项 x264 调参），关闭开关后必须整组消失。刻意与
# _CPU_1080_EXPECTED 分开硬编码，避免「拿实现当期望」的同义反复。
_CPU_BOOST_PARAMS = [
    '-aq-mode', '3',
    '-aq-strength', '0.8',
    '-psy-rd', '1.0:0.0',
    '-rc-lookahead', '40',
]

# 关闭增强后的 CPU 基础参数（= origin/main 的 build_cpu_params，除 -vsync 改名）。
_CPU_1080_BASE = [
    '-c:v', 'libx264',
    '-preset', 'medium',
    '-crf', '23.5',
    '-fps_mode', 'cfr',
    '-profile:v', 'high',
    '-bf', '2',
    '-g', '48',
    '-pix_fmt', 'yuv420p',
]

_CPU_ENHANCEMENT_TOKENS = ('-aq-mode', '-aq-strength', '-psy-rd', '-rc-lookahead')

# ---------------------------------------------------------------------------
# 基线快照：origin/main 上 modules/task_manager.py 内联的编码参数列表（逐字抄录）。
#
# 这些是「开启硬件质量增强之前」的原始行为，本模块的历史等价物。它们与
# `hw_quality_boost=False` 的输出应当一致 —— 但有两处**有意**的差异，单独断言：
#   AMF   `-quality balanced` -> `quality`（默认档位随 VIDEO_HW_QUALITY_LEVEL 走）
#   VAAPI 新增 `-rc_mode CQP`（与 `-qp` 配套，QP 模式下即 VAAPI 默认模式）
#   QSV    `-look_ahead 0` 位置由 -global_quality 之后移到列表末尾（顺序差异）
# 快照保留 `-vsync cfr`（基线写法），由 _legacy_canonical 归一化为 `-fps_mode cfr`。
# ---------------------------------------------------------------------------
_LEGACY_CPU_1080 = [
    '-c:v', 'libx264',
    '-preset', 'medium',
    '-crf', '23.5',
    '-vsync', 'cfr',
    '-profile:v', 'high',
    '-bf', '2',
    '-g', '48',
    '-pix_fmt', 'yuv420p',
]

_LEGACY_NVENC_1080 = [
    '-c:v', 'hevc_nvenc',
    '-preset', 'p7',
    '-tune', 'hq',
    '-rc:v', 'vbr',
    '-b:v', '0',
    '-cq:v', '23.5',
    '-vsync', 'cfr',
    '-profile:v', 'main',
    '-bf', '2',
    '-g', '96',
    '-pix_fmt', 'yuv420p',
    '-tag:v', 'hvc1',
]

_LEGACY_QSV_1080 = [
    '-c:v', 'hevc_qsv',
    '-preset', 'veryslow',
    '-global_quality', _Q_1080_INT,
    '-look_ahead', '0',
    '-vsync', 'cfr',
    '-profile:v', 'main',
    '-bf', '2',
    '-g', '96',
    '-pix_fmt', 'nv12',
    '-tag:v', 'hvc1',
]

_LEGACY_AMF_1080 = [
    '-c:v', 'hevc_amf',
    '-usage', 'transcoding',
    '-quality', 'balanced',
    '-rc', 'qvbr',
    '-qvbr_quality_level', _Q_1080_INT,
    '-vsync', 'cfr',
    '-profile:v', 'main',
    '-g', '96',
    '-pix_fmt', 'yuv420p',
    '-tag:v', 'hvc1',
]

_LEGACY_VAAPI_1080 = [
    '-vaapi_device', '/dev/dri/renderD128',
    '-c:v', 'hevc_vaapi',
    '-qp', _Q_1080_INT,
    '-vsync', 'cfr',
    '-profile:v', 'main',
    '-g', '96',
    '-tag:v', 'hvc1',
]


def _legacy_canonical(params):
    """把参数列表规范成 `[(选项, 值), ...]`，用于与基线快照比较。

    只归一化一处等价改名：基线的 `-vsync cfr` 与本模块的 `-fps_mode cfr` 是
    同一语义（FFmpeg 已弃用 -vsync）。其余选项保持原样，不做别名解析 ——
    这样任何多出来的选项、缺失的选项或值变化都会如实暴露。
    """
    canonical = []
    index = 0
    while index < len(params):
        token = params[index]
        if token == '-vsync':
            canonical.append(('-fps_mode', params[index + 1]))
            index += 2
            continue
        # 所有本模块与基线的选项都带一个值（无裸开关）。
        canonical.append((token, params[index + 1] if index + 1 < len(params) else None))
        index += 2
    return canonical


def _canonical_without_vsync_rename(params):
    """只做 -vsync -> -fps_mode 的字符串级替换，保留顺序（用于逐字比较）。"""
    result = []
    index = 0
    while index < len(params):
        if params[index] == '-vsync':
            result += ['-fps_mode', params[index + 1]]
            index += 2
            continue
        result.append(params[index])
        index += 1
    return result


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
            'cpu_codec': 'x264',
            'cpu_preset': 'medium',
            'cpu_preset_hd': 'veryfast',
            'quality_mode': 'auto',
            'quality_value': None,
            'hw_quality_boost': True,
            'hw_quality_level': 'quality',
            'color_metadata_mode': 'auto',
            'custom_params_enabled': False,
            'custom_params': '',
            'software_tune': '',
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

    def test_whitelist_covers_ffmpeg_canonical_names(self):
        """M-4：ffmpeg 自己的规范名不得被当成「不认识」而静默丢弃。

        `fcc`（colorspace）与 `bt2020-10` / `bt2020-12`（transfer）在
        libx264 / mpeg4 / libx265 / ffv1 下全部 rc=0，且用
        `-x264-params colorprim=bt2020:transfer=bt2020-10:colormatrix=fcc`
        造出的源文件，ffprobe 回读就是这两个名字。
        """
        self.assertEqual(
            resolve_color_metadata('auto', {'color_space': 'fcc'}),
            ['-colorspace', 'fcc'])
        self.assertEqual(
            resolve_color_metadata('auto', {'color_transfer': 'bt2020-10'}),
            ['-color_trc', 'bt2020-10'])
        self.assertEqual(
            resolve_color_metadata('auto', {'color_transfer': 'bt2020-12'}),
            ['-color_trc', 'bt2020-12'])

    def test_dropped_field_is_logged_not_silent(self):
        """被丢弃的字段必须留日志：输出的 VUI 少一项，此前一行提示都没有。"""
        logger = _Logger()
        resolved = resolve_color_metadata(
            'auto',
            {'color_space': 'smpte2085', 'color_transfer': 'bt709'},
            logger=logger)
        self.assertEqual(resolved, ['-color_trc', 'bt709'])
        self.assertTrue(
            any('color_space=smpte2085' in message for message in logger.messages),
            logger.messages)

    def test_no_drop_log_when_everything_resolves(self):
        logger = _Logger()
        resolve_color_metadata('auto', {'color_space': 'bt709'}, logger=logger)
        self.assertEqual(logger.warnings, [])


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


class CpuQualityBoostGateTests(unittest.TestCase):
    """B3：libx264 的四项增强必须受 hw_quality_boost 总开关控制。

    缺陷是「开与关产出逐字相同」，所以用例必须证明**两种设置产物不同**，
    而不是只断言某个列表长度或成员存在。
    """

    def test_boost_on_and_off_produce_different_output(self):
        on = build_encoder_params('cpu', _ctx(**{'hw_quality_boost': True}))
        off = build_encoder_params('cpu', _ctx(**{'hw_quality_boost': False}))
        self.assertNotEqual(on, off, '关闭质量增强后输出必须与开启时不同')

    def test_boost_only_adds_the_four_x264_params(self):
        on = build_encoder_params('cpu', _ctx(**{'hw_quality_boost': True}))
        off = build_encoder_params('cpu', _ctx(**{'hw_quality_boost': False}))
        # 打开 = 基础参数 + 四项增强，且顺序为「基础在前、增强在后」。
        self.assertEqual(on, off + _CPU_BOOST_PARAMS)
        # 差异恰好是那四项，多一个少一个都算回归。
        self.assertEqual(on[len(off):], _CPU_BOOST_PARAMS)

    def test_boost_off_drops_every_x264_enhancement(self):
        off = build_encoder_params('cpu', _ctx(**{'hw_quality_boost': False}))
        for token in _CPU_ENHANCEMENT_TOKENS:
            self.assertNotIn(token, off, f'关闭增强后不应出现 {token}')

    def test_boost_off_equals_baseline_params(self):
        off = build_encoder_params('cpu', _ctx(**{'hw_quality_boost': False}))
        self.assertEqual(off, _CPU_1080_BASE)

    def test_hd_preset_path_follows_preset_lookahead(self):
        # B3 的核心动机：VIDEO_CPU_PRESET_HD 走 veryfast，它存在的理由是
        # 「避免字幕烧录超时」，而 -rc-lookahead 40 会把该 preset 的前瞻从
        # 默认 10 抬到 40。因此 HD 路径**两条分支都不注入** -rc-lookahead：
        # 关闭增强时它是缺陷（白拿 4 倍前瞻），默认开启时同样与该 preset 的
        # 初衷相反（此前只在关闭时甩掉，默认行为仍被放大）。
        overrides = {'height': 2160, 'duration_s': 3600}
        on = build_encoder_params('cpu', _ctx(hw_quality_boost=True, **overrides))
        off = build_encoder_params('cpu', _ctx(hw_quality_boost=False, **overrides))
        self.assertEqual(on[on.index('-preset') + 1], 'veryfast')
        self.assertEqual(off[off.index('-preset') + 1], 'veryfast')
        self.assertNotIn('-rc-lookahead', on)
        self.assertNotIn('-rc-lookahead', off)
        # 其余三项增强仍受开关控制（HD 路径也要保留可用的质量收益）
        self.assertIn('-aq-mode', on)
        self.assertIn('-aq-strength', on)
        self.assertIn('-psy-rd', on)
        self.assertIn('-rc-lookahead', build_encoder_params('cpu', _ctx(hw_quality_boost=True)))

    def test_boost_toggle_is_symmetric_across_all_encoders(self):
        # CPU 曾是唯一缺失该分支的编码器；这条把五个分支钉在一起。
        cases = (
            ('cpu', {}),
            ('nvidia', {}),
            ('intel', {}),
            ('amd', {'amd_backend': 'amf'}),
            ('amd', {'amd_backend': 'vaapi'}),
        )
        for key, overrides in cases:
            on = build_encoder_params(key, _ctx(hw_quality_boost=True, **overrides))
            off = build_encoder_params(key, _ctx(hw_quality_boost=False, **overrides))
            self.assertNotEqual(on, off, f'{key} {overrides} 的开关未生效')
            for token in _ENHANCEMENT_TOKENS + _CPU_ENHANCEMENT_TOKENS:
                self.assertNotIn(token, off, f'{key} {overrides} 关闭后仍有 {token}')


class LegacyBaselineSnapshotTests(unittest.TestCase):
    """全默认配置下，关闭增强的输出必须与 origin/main 的基线参数等价。

    仅有的三处差异是刻意的，逐条断言；其余任何偏差都是默认行为回归。
    """

    def test_cpu_boost_off_matches_legacy_verbatim(self):
        off = build_encoder_params('cpu', _ctx(**{'hw_quality_boost': False}))
        self.assertEqual(off, _canonical_without_vsync_rename(_LEGACY_CPU_1080))

    def test_nvenc_boost_off_matches_legacy_verbatim(self):
        off = build_encoder_params('nvidia', _ctx(**{'hw_quality_boost': False}))
        self.assertEqual(off, _canonical_without_vsync_rename(_LEGACY_NVENC_1080))

    def test_qsv_boost_off_matches_legacy_as_option_map(self):
        # 基线把 -look_ahead 0 放在 -global_quality 之后，本模块放在列表末尾；
        # 独立选项顺序不影响 ffmpeg 行为，故按 (选项, 值) 集合比较。
        off = build_encoder_params('intel', _ctx(**{'hw_quality_boost': False}))
        self.assertEqual(
            sorted(_legacy_canonical(off)), sorted(_legacy_canonical(_LEGACY_QSV_1080))
        )
        # 位置差异本身也要钉住：值必须仍是基线的 0，而不是被省掉或改了值。
        self.assertEqual(off[off.index('-look_ahead') + 1], '0')
        self.assertEqual(
            _LEGACY_QSV_1080[_LEGACY_QSV_1080.index('-look_ahead') + 1], '0'
        )
        self.assertLess(off.index('-look_ahead'), len(off))

    def test_vaapi_boost_off_differs_only_by_rc_mode(self):
        off = build_encoder_params(
            'amd', _ctx(**{'amd_backend': 'vaapi', 'hw_quality_boost': False})
        )
        canonical = _legacy_canonical(off)
        legacy = _legacy_canonical(_LEGACY_VAAPI_1080)
        added = [item for item in canonical if item not in legacy]
        removed = [item for item in legacy if item not in canonical]
        # 有意差异：新增 -rc_mode CQP（与 -qp 配套，QP 模式即 VAAPI 默认模式）。
        self.assertEqual(added, [('-rc_mode', 'CQP')])
        self.assertEqual(removed, [])

    def test_amf_boost_off_differs_only_by_default_quality_tier(self):
        # m5 的决定：-quality 档位跟随 VIDEO_HW_QUALITY_LEVEL，默认 quality。
        # 基线硬编码 balanced，因此这是一处**有意**的质量提升，快照如实记录。
        off = build_encoder_params(
            'amd', _ctx(**{'amd_backend': 'amf', 'hw_quality_boost': False})
        )
        canonical = _legacy_canonical(off)
        legacy = _legacy_canonical(_LEGACY_AMF_1080)
        added = [item for item in canonical if item not in legacy]
        removed = [item for item in legacy if item not in canonical]
        self.assertEqual(added, [('-quality', 'quality')])
        self.assertEqual(removed, [('-quality', 'balanced')])
        # 除档位外逐字相同。
        self.assertEqual(
            [item for item in canonical if item[0] != '-quality'],
            [item for item in legacy if item[0] != '-quality'],
        )

    def test_amf_baseline_tier_is_reachable_by_explicit_level(self):
        # 「有意提升」必须可退回：显式 balanced 能拿到基线档位。
        params = build_encoder_params(
            'amd', _ctx(amd_backend='amf', hw_quality_boost=False,
                        hw_quality_level='balanced')
        )
        self.assertEqual(
            _legacy_canonical(params), _legacy_canonical(_LEGACY_AMF_1080)
        )

    def test_legacy_snapshot_captures_the_vsync_to_fps_mode_rename(self):
        # 这条证明快照不是同义反复：基线写的确实是 -vsync，本模块写 -fps_mode，
        # 且不套用归一化时两者不相等。
        for legacy, key, overrides in (
            (_LEGACY_CPU_1080, 'cpu', {}),
            (_LEGACY_NVENC_1080, 'nvidia', {}),
            (_LEGACY_QSV_1080, 'intel', {}),
            (_LEGACY_AMF_1080, 'amd', {'amd_backend': 'amf'}),
            (_LEGACY_VAAPI_1080, 'amd', {'amd_backend': 'vaapi'}),
        ):
            self.assertIn('-vsync', legacy, key)
            params = build_encoder_params(
                key, _ctx(hw_quality_boost=False, **overrides)
            )
            self.assertNotIn('-vsync', params, key)
            self.assertNotEqual(params, legacy, key)

    def test_legacy_defaults_are_actually_distance_from_current_defaults(self):
        # 默认（增强开启）下每个编码器都比基线多出增强项，快照因此不会恒真。
        for key, overrides, legacy in (
            ('cpu', {}, _LEGACY_CPU_1080),
            ('nvidia', {}, _LEGACY_NVENC_1080),
            ('intel', {}, _LEGACY_QSV_1080),
            ('amd', {'amd_backend': 'amf'}, _LEGACY_AMF_1080),
            ('amd', {'amd_backend': 'vaapi'}, _LEGACY_VAAPI_1080),
        ):
            default = build_encoder_params(key, _ctx(**overrides))
            self.assertNotEqual(
                _legacy_canonical(default), _legacy_canonical(legacy), key
            )


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

    def test_whitespace_only_list_entries_are_dropped(self):
        # m1：列表分支曾保留纯空白项，['  '] 会被整份当作视频参数返回，
        # 把内置的 -c:v 顶掉且不产生任何可读错误；字符串分支则经 shlex.split
        # 天然不产生空串。两条路径必须一致。
        expected = build_encoder_params('cpu', _ctx())
        for blank in (['  '], ['', '  '], ['\t'], ['\n', ' '], ('  ',), ['   ', '']):
            self.assertEqual(
                build_encoder_params('cpu', _ctx(**{'custom_params': blank})),
                expected,
                f'blank={blank!r}',
            )
            # 字符串分支的等价输入本来就返回 None（不覆盖）。
            self.assertEqual(
                build_encoder_params('cpu', _ctx(**{'custom_params': ' '.join(blank)})),
                expected,
                f'joined={blank!r}',
            )

    def test_non_blank_list_entries_keep_their_content(self):
        params = build_encoder_params(
            'cpu', _ctx(**{'custom_params': ['-c:v', 'libx264', '-preset', 'slow', '  ']})
        )
        self.assertEqual(params, ['-c:v', 'libx264', '-preset', 'slow'])

    def test_whitespace_entry_does_not_silently_become_the_whole_param_list(self):
        # 反证式断言：修复前 build_encoder_params 会返回 ['  ']，里面没有 -c:v。
        params = build_encoder_params('cpu', _ctx(**{'custom_params': ['  ']}))
        self.assertIn('-c:v', params)
        self.assertNotEqual(params, ['  '])


class X264ParamsConflictTests(unittest.TestCase):
    """m2：色彩 VUI 补写必须让位给用户已显式指定的 x264 私有参数。"""

    def test_conflict_detected_for_all_x264_option_spellings(self):
        # `-x264opts` 是 `-x264-params` 的历史别名；只匹配后者会让两处同时
        # 输出 x264 参数，后写的覆盖先写的。
        spellings = (
            ['-x264-params', 'aq-mode=3'],
            ['-x264-params=aq-mode=3'],
            ['-x264opts', 'aq-mode=3'],
            ['-x264opts=aq-mode=3'],
            ['-X264OPTS', 'aq-mode=3'],
            ['-c:v', 'libx264', '-x264opts', 'aq-mode=3'],
        )
        for custom in spellings:
            self.assertEqual(
                build_color_vui_params('cpu', {'color_primaries': 'bt709'}, custom),
                [],
                f'custom={custom!r}',
            )

    def test_unrelated_custom_params_do_not_suppress_vui(self):
        for custom in (
            ['-c:v', 'libx264'],
            ['-tune', 'film'],
            ['-aq-mode', '3'],
            [],
            None,
        ):
            self.assertEqual(
                build_color_vui_params('cpu', {'color_primaries': 'bt709'}, custom),
                ['-x264-params', 'colorprim=bt709'],
                f'custom={custom!r}',
            )

    def test_hardware_encoder_never_emits_x264_params(self):
        for key in ('nvidia', 'intel', 'amd', 'bogus', ''):
            self.assertEqual(
                build_color_vui_params(key, {'color_primaries': 'bt709'}),
                [],
                f'key={key!r}',
            )


class CoerceIntTests(unittest.TestCase):
    """m3：_coerce_int 不得接受 bool，也不得截断浮点。"""

    def test_bool_is_rejected(self):
        from modules.video_encoder_params import _coerce_int

        self.assertIsNone(_coerce_int(True))
        self.assertIsNone(_coerce_int(False))

    def test_float_is_rounded_not_truncated(self):
        from modules.video_encoder_params import _coerce_int

        self.assertEqual(_coerce_int(1.9), 2)
        self.assertEqual(_coerce_int(2.4), 2)
        self.assertEqual(_coerce_int(1.5), 2)
        self.assertEqual(_coerce_int(0.4), 0)
        self.assertEqual(_coerce_int(-1.6), -2)

    def test_non_finite_float_is_rejected(self):
        from modules.video_encoder_params import _coerce_int

        for bad in (float('nan'), float('inf'), float('-inf')):
            self.assertIsNone(_coerce_int(bad), f'bad={bad!r}')

    def test_integers_and_numeric_strings_unchanged(self):
        from modules.video_encoder_params import _coerce_int

        self.assertEqual(_coerce_int(48), 48)
        self.assertEqual(_coerce_int('48'), 48)
        self.assertEqual(_coerce_int('-3'), -3)
        for bad in (None, '', 'abc', '1.9', [], {}, object()):
            self.assertIsNone(_coerce_int(bad), f'bad={bad!r}')

    def test_bool_channels_no_longer_becomes_mono(self):
        # channels=True 曾静默产出 -ac 1。
        params = build_audio_params({'codec_name': 'vorbis', 'channels': True})
        self.assertEqual(params[params.index('-ac') + 1], '2')

    def test_float_channels_rounds_to_stereo(self):
        # channels=1.9 是双声道输入的浮点表示，曾被截断成 -ac 1。
        params = build_audio_params({'codec_name': 'vorbis', 'channels': 1.9})
        self.assertEqual(params[params.index('-ac') + 1], '2')
        params = build_audio_params({'codec_name': 'vorbis', 'channels': 1.4})
        self.assertEqual(params[params.index('-ac') + 1], '1')
        params = build_audio_params({'codec_name': 'vorbis', 'channels': 1})
        self.assertEqual(params[params.index('-ac') + 1], '1')

    def test_bool_gop_falls_back_to_default(self):
        # gop=True 曾产出 1 帧 GOP。
        params = build_encoder_params('cpu', _ctx(**{'gop': True, 'gop_hevc': True}))
        self.assertEqual(params[params.index('-g') + 1], '48')
        nvenc = build_encoder_params(
            'nvidia', _ctx(**{'gop': True, 'gop_hevc': True})
        )
        self.assertEqual(nvenc[nvenc.index('-g') + 1], '96')

    def test_float_gop_rounds(self):
        params = build_encoder_params('cpu', _ctx(**{'gop': 47.9}))
        self.assertEqual(params[params.index('-g') + 1], '48')
        params = build_encoder_params('cpu', _ctx(**{'gop': 48.6}))
        self.assertEqual(params[params.index('-g') + 1], '49')

    def test_audio_bitrate_ladder_unchanged_for_integer_inputs(self):
        # bit_rate 的取整语义变化只在人为浮点输入下可见（ffprobe 恒给整数字符串）；
        # 这里锁定整数输入下阶梯不受影响。
        ladder = (
            (192000, '192k'), (191999, '160k'), (160000, '160k'),
            (159999, '128k'), (128000, '128k'), (127999, '96k'),
            (96000, '96k'), (95999, '64k'),
        )
        for bit_rate, expected in ladder:
            params = build_audio_params(
                {'codec_name': 'vorbis', 'bit_rate': bit_rate}
            )
            self.assertEqual(
                params[params.index('-b:a') + 1], expected, f'bit_rate={bit_rate}'
            )

    def test_hostile_objects_never_escape_as_exceptions(self):
        # 模块 docstring 声明「公开函数对任意输入都不抛异常」。`value == ''`
        # 这类比较本身可能被恶意对象触发任意异常，_coerce_int 必须全部吞掉，
        # 否则 build_encoder_params / build_audio_params 会把它泄露给调用方。
        class _Boom:
            def __eq__(self, other):
                raise RuntimeError('boom')

            def __hash__(self):
                raise RuntimeError('boom')

            def __str__(self):
                raise RuntimeError('boom')

        from modules.video_encoder_params import _coerce_int

        self.assertIsNone(_coerce_int(_Boom()))
        for field in ('gop', 'gop_hevc', 'height'):
            params = build_encoder_params('cpu', _ctx(**{field: _Boom()}))
            self.assertIn('-c:v', params, field)
        for field in ('channels', 'sample_rate', 'bit_rate'):
            params = build_audio_params({'codec_name': 'vorbis', field: _Boom()})
            self.assertEqual(params[:3], ['-c:a', 'aac', '-b:a'], field)


class X264ColorValueMappingTests(unittest.TestCase):
    """m6：写进 -x264-params 的值必须是 x264 自己的枚举名。

    x264 对不认识的枚举名只打印 "Error parsing option" 并**继续返回 0**，
    VUI 里该字段留空 —— 静默丢失。真实 ffmpeg 校验见
    tests/test_color_metadata_smoke.py。
    """

    def test_equivocal_names_are_renamed(self):
        cases = (
            ({'colorspace': 'rgb'}, 'colormatrix=gbr'),
            ({'color_primaries': 'smpte428_1'}, 'colorprim=smpte428'),
            ({'color_trc': 'log'}, 'transfer=log100'),
            ({'color_trc': 'log_sqrt'}, 'transfer=log316'),
            ({'color_trc': 'iec61966_2_4'}, 'transfer=iec61966-2-4'),
            ({'color_trc': 'iec61966_2_1'}, 'transfer=iec61966-2-1'),
            ({'color_trc': 'bt1361'}, 'transfer=bt1361e'),
            ({'color_trc': 'smpte428_1'}, 'transfer=smpte428'),
        )
        for color_map, expected in cases:
            self.assertEqual(
                build_color_vui_params('cpu', color_map),
                ['-x264-params', expected],
                f'color_map={color_map!r}',
            )

    def test_values_without_x264_equivalent_are_skipped(self):
        # x264 的 colorprim 没有 jedec-p22 / ebu3213，transfer 没有 gamma22 / gamma28；
        # 跳过该键，其余键照常输出，绝不回退到语义不同的值。
        self.assertEqual(
            build_color_vui_params('cpu', {'color_primaries': 'jedec-p22'}), []
        )
        self.assertEqual(
            build_color_vui_params('cpu', {'color_primaries': 'ebu3213'}), []
        )
        self.assertEqual(build_color_vui_params('cpu', {'color_trc': 'gamma22'}), [])
        self.assertEqual(build_color_vui_params('cpu', {'color_trc': 'gamma28'}), [])
        self.assertEqual(
            build_color_vui_params(
                'cpu',
                {'color_primaries': 'jedec-p22', 'color_trc': 'bt709',
                 'colorspace': 'bt709'},
            ),
            ['-x264-params', 'transfer=bt709:colormatrix=bt709'],
        )

    def test_same_named_values_pass_through_unmapped(self):
        entries = []
        for value in ('bt709', 'bt470m', 'bt470bg', 'smpte170m', 'smpte240m',
                      'film', 'bt2020', 'smpte428', 'smpte431', 'smpte432'):
            entries.append(({'color_primaries': value}, f'colorprim={value}'))
        for value in ('bt709', 'smpte170m', 'smpte240m', 'linear', 'log100',
                      'log316', 'iec61966-2-4', 'iec61966-2-1', 'bt1361e',
                      'smpte2084', 'smpte428', 'arib-std-b67'):
            entries.append(({'color_trc': value}, f'transfer={value}'))
        for value in ('bt709', 'bt470bg', 'smpte170m', 'smpte240m', 'bt2020nc'):
            entries.append(({'colorspace': value}, f'colormatrix={value}'))
        for color_map, expected in entries:
            self.assertEqual(
                build_color_vui_params('cpu', color_map),
                ['-x264-params', expected],
                f'color_map={color_map!r}',
            )

    def test_field_order_is_stable(self):
        self.assertEqual(
            build_color_vui_params('cpu', {
                'colorspace': 'bt709',
                'color_primaries': 'bt709',
                'color_trc': 'bt709',
            }),
            ['-x264-params', 'colorprim=bt709:transfer=bt709:colormatrix=bt709'],
        )

    def test_empty_or_partial_color_map(self):
        for bad in (None, {}, 'bt709', [], 42):
            self.assertEqual(build_color_vui_params('cpu', bad), [], f'bad={bad!r}')
        self.assertEqual(
            build_color_vui_params('cpu', {'color_trc': ''}),
            [],
        )


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

    def test_non_numeric_returns_empty_string(self):
        # 契约：非数值一律 ''，绝不把原文回显进 -crf / -cq:v 的参数位。
        # 旧实现返回 str(q)，会产生 `-crf abc` 这种被 ffmpeg 直接拒绝的命令。
        for bad in (None, 'abc', [], {}, (), object(), ' ', 'x1', '1.2.3'):
            self.assertEqual(format_quality_value(bad), '', f'bad={bad!r}')

    def test_bool_is_not_accepted_as_quality(self):
        # bool 是 int 子类，绝不能让 True 变成 '-crf 1'。
        self.assertEqual(format_quality_value(True), '')
        self.assertEqual(format_quality_value(False), '')

    def test_nan_and_inf_are_rejected(self):
        for bad in (float('nan'), float('inf'), float('-inf')):
            self.assertEqual(format_quality_value(bad), '', f'bad={bad!r}')

    def test_numeric_strings_still_accepted(self):
        self.assertEqual(format_quality_value('23.50'), '23.5')
        self.assertEqual(format_quality_value(' 18 '), '18')

    def test_encoder_quality_slots_never_receive_empty_value(self):
        # 端到端兜底：即使 ctx 被投毒，-crf / -cq:v 后面也必须是数字。
        poisoned = (
            {'quality_mode': 'manual', 'quality_value': 'abc'},
            {'quality_mode': 'manual', 'quality_value': True},
            {'quality_mode': 'manual', 'quality_value': float('nan')},
            {'quality_mode': 'manual', 'quality_value': [1]},
        )
        for overrides in poisoned:
            for key, option in (('cpu', '-crf'), ('nvidia', '-cq:v')):
                params = build_encoder_params(key, _ctx(**overrides))
                value = params[params.index(option) + 1]
                self.assertRegex(value, r'^\d+(\.\d+)?$', f'{key} {overrides}')


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
