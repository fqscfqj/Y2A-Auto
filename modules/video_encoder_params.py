#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""视频转码参数纯函数模块（字幕烧录用）。

把原本内嵌在 task_manager._embed_subtitle_in_video 里的编码参数拼装逻辑抽成
一组无副作用纯函数：质量推荐、配置解析、色彩元数据透传、编码器参数构造、
音频参数构造与质量值格式化。

本模块只依赖标准库，不导入任何项目模块，不读写文件，不调用 subprocess，
所有公开函数对任意输入都不抛异常（非法输入一律回退到安全默认值）。

这些参数的选项支持情况已在本机 FFmpeg N-123313（2026-03，已编译 ffnvcodec /
libvpl / amf / vaapi）实测确认：

  hevc_nvenc：-rc vbr|cbr|vbr_hq|cbr_hq、-cq、-preset p1..p7、-tune hq|uhq、
    -rc-lookahead（默认 0）、-multipass disabled|qres|fullres、-spatial-aq、
    -temporal-aq（需与 -spatial-aq 同时开启）、-aq-strength 1..15、-b_ref_mode、
    -bf 均被选项解析器接受。
  hevc_qsv：-preset veryfast..veryslow、-extbrc、-look_ahead_depth（需 extbrc）、
    -mbbrc、-rdo、-scenario archive|livestreaming、-global_quality（AVCodecContext
    级 ICQ）。-look_ahead 属 QSV 深层通用选项，不在 -h encoder 列表但合法。
  hevc_amf：-usage transcoding、-quality quality|balanced|speed、-rc hqvbr|qvbr、
    -qvbr_quality_level -1..51、-vbaq、-preanalysis、-pa_caq_strength 均被接受。
  hevc_vaapi：-rc_mode auto|CQP|CBR|VBR|ICQ|QVBR|AVBR、-qp、-blbrc 合法；
    没有 -compression_level 这个选项。
  libx264：-tune/-crf/-aq-mode/-aq-strength/-psy-rd/-rc-lookahead/-x264-params/
    -profile/-bf/-g/-pix_fmt 合法；-tune 合法值为 film、animation、grain、
    stillimage、psnr、ssim、fastdecode、zerolatency（实测 'none' 会被 x264 拒绝）。
  -vsync 已被 FFmpeg 标记弃用（输出 "-vsync is deprecated. Use -fps_mode"），
    本模块统一使用 -fps_mode cfr。
"""

import math
import shlex

# 按视频高度推荐的固定质量值（CRF/CQ/QP，越小质量越高），须与历史 get_recommended_quality 一致。
DEFAULT_QUALITY_BY_HEIGHT = ((2160, 22.5), (1440, 23.0), (1080, 23.5), (720, 24.5))

# 低于 720p 时的兜底质量。
FALLBACK_QUALITY = 25.5

# 质量值合法区间（CRF/CQ/QP 语义）。
_MIN_QUALITY = 0.0
_MAX_QUALITY = 51.0

_VALID_ENCODERS = ('auto', 'cpu', 'nvidia', 'intel', 'amd')
_VALID_QUALITY_MODES = ('auto', 'manual')
_VALID_HW_QUALITY_LEVELS = ('fast', 'balanced', 'quality')
_VALID_COLOR_MODES = ('auto', 'bt709', 'off')
_VALID_X264_TUNES = (
    'film', 'animation', 'grain', 'stillimage',
    'psnr', 'ssim', 'fastdecode', 'zerolatency',
)

# 与 config_manager._VIDEO_CPU_PRESETS 保持同一集合（仅标准库，故此处复刻常量）。
_VALID_CPU_PRESETS = (
    'ultrafast', 'superfast', 'veryfast', 'faster', 'fast',
    'medium', 'slow', 'slower', 'veryslow',
)
_DEFAULT_CPU_PRESET = 'medium'
_DEFAULT_CPU_PRESET_HD = 'veryfast'

# 编码器默认参数
_DEFAULT_HEIGHT = 1080
_DEFAULT_GOP = 48
_DEFAULT_GOP_HEVC = 96
_HD_PRESET_MIN_HEIGHT = 1440
_HD_PRESET_MIN_DURATION_S = 600

# 硬件质量等级 → 各编码器档位
_NVENC_PRESET_BY_LEVEL = {'fast': 'p4', 'balanced': 'p5', 'quality': 'p7'}
_QSV_PRESET_BY_LEVEL = {'fast': 'veryfast', 'balanced': 'medium', 'quality': 'veryslow'}
_AMF_QUALITY_BY_LEVEL = {'fast': 'speed', 'balanced': 'balanced', 'quality': 'quality'}

# NVENC 质量增强项：-temporal-aq 必须与 -spatial-aq 成组出现。
_NVENC_BOOST_PARAMS = [
    '-rc-lookahead', '32',
    '-multipass', 'qres',
    '-spatial-aq', '1',
    '-temporal-aq', '1',
    '-aq-strength', '8',
    '-b_ref_mode', 'middle',
]
_QSV_BOOST_PARAMS = [
    '-extbrc', '1',
    '-look_ahead_depth', '40',
    '-mbbrc', '1',
    '-rdo', '1',
    '-scenario', 'archive',
]
# QSV 未开启增强时保持历史行为：显式关闭 look_ahead。
_QSV_BASELINE_LOOKAHEAD = ['-look_ahead', '0']
_AMF_BOOST_PARAMS = ['-vbaq', '1', '-preanalysis', '1', '-pa_caq_strength', 'high']
_VAAPI_BOOST_PARAMS = ['-blbrc', '1']

# x264 高质量调参（独立一等选项，非 -x264-params）。
_X264_QUALITY_PARAMS = [
    '-aq-mode', '3',
    '-aq-strength', '0.8',
    '-psy-rd', '1.0:0.0',
    '-rc-lookahead', '40',
]

_VAAPI_DEVICE = '/dev/dri/renderD128'

# 色彩元数据映射
_YUV_SPACE_VALUES = frozenset((
    'bt709', 'bt470bg', 'bt470m', 'smpte170m', 'smpte240m', 'bt2020nc',
    'bt2020c', 'bt2020', 'smpte2085', 'film', 'ictcp', 'ycgco',
))
_TRC_VALUES = frozenset((
    'bt709', 'gamma22', 'gamma28', 'smpte170m', 'smpte240m', 'linear',
    'log100', 'log316', 'iec61966-2-4', 'bt1361e', 'iec61966-2-1',
    'bt2020-10', 'bt2020-12', 'smpte2084', 'smpte428', 'arib-std-b67',
))
_COLOR_RANGE_ALIASES = {'tv': 'tv', 'limited': 'tv', 'pc': 'pc', 'full': 'pc'}
_SKIPPED_COLOR_TOKENS = frozenset(('', 'unknown', 'unspecified', 'reserved', 'n/a'))

_TRUE_TOKENS = frozenset(('true', '1', 'yes', 'on'))
_FALSE_TOKENS = frozenset(('false', '0', 'no', 'off'))


def _coerce_number(value):
    """把任意值宽容转换为有限浮点数；无法转换返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None
    return None


def _coerce_int(value):
    """与 task_manager._coerce_int 语义一致的严格整数转换。"""
    try:
        if value is None or value == '':
            return None
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _coerce_bool(value, default):
    """宽容布尔解析，非法值回退 default；绝不用 bool(str) 强转。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_TOKENS:
            return True
        if text in _FALSE_TOKENS:
            return False
    return default


def _normalize_choice(value, valid_values, default):
    """大小写不敏感的枚举归一化，非法值回退 default。"""
    try:
        text = str(value).strip().lower() if value is not None else ''
    except Exception:
        return default
    return text if text in valid_values else default


def _normalize_preset(value, default):
    """归一化 x264 preset。"""
    return _normalize_choice(value, _VALID_CPU_PRESETS, default)


def _normalize_x264_tune(value):
    """归一化 x264 tune，非法或缺失一律为空字符串（表示不传 -tune）。"""
    try:
        text = str(value).strip().lower() if value is not None else ''
    except Exception:
        return ''
    return text if text in _VALID_X264_TUNES else ''


def _normalize_text(value):
    """把配置值规范成去掉首尾空白的字符串。"""
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        try:
            return ' '.join(str(item) for item in value).strip()
        except Exception:
            return ''
    try:
        return str(value).strip()
    except Exception:
        return ''


def _parse_quality_value(value):
    """解析手动质量值，clamp 到 0~51，非法返回 None。"""
    number = _coerce_number(value)
    if number is None:
        return None
    return max(_MIN_QUALITY, min(_MAX_QUALITY, number))


def _normalize_custom_params(value):
    """把自定义参数规范成 list[str]；无效或为空返回 None。"""
    if isinstance(value, (list, tuple)):
        try:
            items = [str(item) for item in value if item is not None and str(item) != '']
        except Exception:
            return None
        return items or None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            items = shlex.split(text)
        except ValueError:
            return None
        return items or None
    return None


def format_quality_value(q):
    """把质量值格式化为字符串。

    整数值不带小数（23.0 -> '23'），否则最多保留 2 位小数并去掉尾随 0
    （23.5 -> '23.5'，23.456 -> '23.46'）。非数值输入退化为其字符串形式。
    """
    number = _coerce_number(q)
    if number is None:
        if q is None:
            return ''
        try:
            return str(q).strip()
        except Exception:
            return ''
    if float(number).is_integer():
        return str(int(number))
    text = f'{number:.2f}'.rstrip('0').rstrip('.')
    return text or '0'


def recommend_quality(height):
    """按视频高度返回推荐质量值（CRF/CQ/QP，越小质量越高）。

    语义与历史 get_recommended_quality 完全一致（height >= 2160 -> 22.5，
    >= 1440 -> 23.0，>= 1080 -> 23.5，>= 720 -> 24.5，其余 -> 25.5）。
    height 非法（None / 非数字 / 布尔 / <= 0）时返回 FALLBACK_QUALITY。返回 float。
    """
    number = _coerce_number(height)
    if number is None or number <= 0:
        return float(FALLBACK_QUALITY)
    for threshold, quality in DEFAULT_QUALITY_BY_HEIGHT:
        if number >= threshold:
            return float(quality)
    return float(FALLBACK_QUALITY)


def parse_encoder_config(config):
    """从配置 dict 解析编码相关设置，返回规范化 dict。

    config 为 None/非 dict 时按空 dict 处理。读取键：VIDEO_ENCODER、
    VIDEO_CPU_PRESET、VIDEO_CPU_PRESET_HD、VIDEO_QUALITY_MODE、
    VIDEO_QUALITY_VALUE、VIDEO_HW_QUALITY_BOOST、VIDEO_HW_QUALITY_LEVEL、
    VIDEO_COLOR_METADATA_MODE、VIDEO_CUSTOM_PARAMS_ENABLED、VIDEO_CUSTOM_PARAMS、
    VIDEO_X264_TUNE。返回键：encoder_pref、cpu_preset、cpu_preset_hd、
    quality_mode、quality_value、hw_quality_boost、hw_quality_level、
    color_metadata_mode、custom_params_enabled、custom_params、x264_tune。

    布尔解析宽容（'true'/'false'/'1'/'0'/'yes'/'no'/'on'/'off'，大小写不敏感），
    非法值回退默认。本函数绝不抛异常。
    """
    cfg = config if isinstance(config, dict) else {}

    quality_mode = _normalize_choice(
        cfg.get('VIDEO_QUALITY_MODE'), _VALID_QUALITY_MODES, 'auto'
    )

    return {
        'encoder_pref': _normalize_choice(cfg.get('VIDEO_ENCODER'), _VALID_ENCODERS, 'auto'),
        'cpu_preset': _normalize_preset(cfg.get('VIDEO_CPU_PRESET'), _DEFAULT_CPU_PRESET),
        'cpu_preset_hd': _normalize_preset(
            cfg.get('VIDEO_CPU_PRESET_HD'), _DEFAULT_CPU_PRESET_HD
        ),
        'quality_mode': quality_mode,
        'quality_value': _parse_quality_value(cfg.get('VIDEO_QUALITY_VALUE')),
        'hw_quality_boost': _coerce_bool(cfg.get('VIDEO_HW_QUALITY_BOOST', True), True),
        'hw_quality_level': _normalize_choice(
            cfg.get('VIDEO_HW_QUALITY_LEVEL'), _VALID_HW_QUALITY_LEVELS, 'quality'
        ),
        'color_metadata_mode': _normalize_choice(
            cfg.get('VIDEO_COLOR_METADATA_MODE'), _VALID_COLOR_MODES, 'auto'
        ),
        'custom_params_enabled': _coerce_bool(
            cfg.get('VIDEO_CUSTOM_PARAMS_ENABLED', False), False
        ),
        'custom_params': _normalize_text(cfg.get('VIDEO_CUSTOM_PARAMS')),
        'x264_tune': _normalize_x264_tune(cfg.get('VIDEO_X264_TUNE')),
    }


def _normalize_color_token(value):
    """归一化单个色彩元数据 token；unknown/空/None 统一返回空字符串。"""
    if not isinstance(value, str):
        return ''
    token = value.strip().lower()
    return '' if token in _SKIPPED_COLOR_TOKENS else token


def normalize_color_metadata(mode, source_color_info):
    """解析色彩元数据，返回规范化映射 dict。

    键固定为 colorspace / color_primaries / color_trc / color_range，只包含可识别
    且非 unknown 的项（缺失的键不出现）。mode='off' 一律返回 {}，mode='bt709'
    返回强制 bt709 四项，mode='auto' 从 source_color_info 透传。
    """
    normalized_mode = _normalize_choice(mode, _VALID_COLOR_MODES, 'auto')

    if normalized_mode == 'off':
        return {}
    if normalized_mode == 'bt709':
        return {
            'colorspace': 'bt709',
            'color_primaries': 'bt709',
            'color_trc': 'bt709',
            'color_range': 'tv',
        }

    info = source_color_info if isinstance(source_color_info, dict) else {}
    resolved = {}

    space = _normalize_color_token(info.get('color_space'))
    if space in _YUV_SPACE_VALUES:
        resolved['colorspace'] = space

    primaries = _normalize_color_token(info.get('color_primaries'))
    if primaries in _YUV_SPACE_VALUES:
        resolved['color_primaries'] = primaries

    trc = _normalize_color_token(info.get('color_transfer'))
    if trc in _TRC_VALUES:
        resolved['color_trc'] = trc

    color_range = _normalize_color_token(info.get('color_range'))
    if color_range in _COLOR_RANGE_ALIASES:
        resolved['color_range'] = _COLOR_RANGE_ALIASES[color_range]

    return resolved


def resolve_color_metadata(mode, source_color_info):
    """解析要写入输出文件的色彩元数据，返回 list[str]（可直接展开进 ffmpeg 命令）。

    mode='off' -> []；mode='bt709' -> 强制 bt709 + tv 范围；mode='auto' ->
    从 source_color_info（ffprobe 流信息 dict）透传可识别值，无法识别/缺失/
    unknown/unspecified/reserved 的键一律跳过。映射关系：color_space ->
    -colorspace、color_primaries -> -color_primaries、color_transfer ->
    -color_trc、color_range -> -color_range（'limited'/'full' 归一化为 'tv'/'pc'）。
    输出顺序固定为 colorspace、color_primaries、color_trc、color_range。

    注意：这些是 AVCodecContext 级通用选项，硬件编码器（NVENC/QSV/AMF/VAAPI）
    会据此写入码流 VUI；但 libx264/libx265 只转发 colorspace，会**静默忽略**
    color_primaries/color_trc，软件编码路径必须再配合 build_color_vui_params。
    """
    resolved = normalize_color_metadata(mode, source_color_info)
    params = []
    for option, key in (
        ('-colorspace', 'colorspace'),
        ('-color_primaries', 'color_primaries'),
        ('-color_trc', 'color_trc'),
        ('-color_range', 'color_range'),
    ):
        if key in resolved:
            params += [option, resolved[key]]
    return params


def build_color_vui_params(encoder_key, color_map, custom_params=None):
    """返回编码器私有参数，确保色彩原色/传递特性真正写进码流 VUI。

    实测（FFmpeg N-123313）：libx264 与 libx265 的包装器只把 avctx->colorspace
    转发给 VUI，`-color_primaries` / `-color_trc` 会被静默丢弃 —— 输出文件里
    色域正确但原色与传递特性缺失，播放器仍可能按默认值解释。因此软件编码路径
    必须用编码器私有参数补写：

        -x264-params colorprim=bt709:transfer=bt709:colormatrix=bt709

    同时 range 无法通过 x264-params 生效（实测 range=pc 被忽略），故色彩范围
    仍由 resolve_color_metadata 的通用 `-color_range` 负责，这里不重复输出。

    硬件编码器由通用选项负责，返回 []。custom_params 中已出现 x264-params 时
    同样返回 []，避免与用户的显式配置互相覆盖。
    """
    resolved = color_map if isinstance(color_map, dict) else {}
    key = encoder_key.strip().lower() if isinstance(encoder_key, str) else ''
    if key != 'cpu':
        # 只有 libx264 需要私有参数补写；其他值（含非法值）按非软件编码处理
        return []

    custom_tokens = custom_params if isinstance(custom_params, (list, tuple)) else []
    for token in custom_tokens:
        try:
            if 'x264-params' in str(token):
                return []
        except Exception:
            continue

    entries = []
    if 'color_primaries' in resolved:
        entries.append(f"colorprim={resolved['color_primaries']}")
    if 'color_trc' in resolved:
        entries.append(f"transfer={resolved['color_trc']}")
    if 'colorspace' in resolved:
        entries.append(f"colormatrix={resolved['colorspace']}")
    if not entries:
        return []
    return ['-x264-params', ':'.join(entries)]


def _resolve_context(ctx):
    """把 ctx 归一化成编码参数构造所需的内部上下文。"""
    context = ctx if isinstance(ctx, dict) else {}

    height = _coerce_number(context.get('height'))
    if height is None or height <= 0:
        height = _DEFAULT_HEIGHT

    gop = _coerce_int(context.get('gop'))
    if gop is None or gop <= 0:
        gop = _DEFAULT_GOP

    gop_hevc = _coerce_int(context.get('gop_hevc'))
    if gop_hevc is None or gop_hevc <= 0:
        gop_hevc = _DEFAULT_GOP_HEVC

    quality_mode = _normalize_choice(
        context.get('quality_mode'), _VALID_QUALITY_MODES, 'auto'
    )
    quality = None
    if quality_mode == 'manual':
        manual_value = _coerce_number(context.get('quality_value'))
        if manual_value is not None:
            quality = max(_MIN_QUALITY, min(_MAX_QUALITY, manual_value))
    if quality is None:
        quality = recommend_quality(height)

    duration = _coerce_number(context.get('duration_s'))
    if duration is not None and duration < 0:
        duration = None

    return {
        'height': height,
        'gop': gop,
        'gop_hevc': gop_hevc,
        'quality': quality,
        'duration_s': duration,
        'cpu_preset': _normalize_preset(context.get('cpu_preset'), _DEFAULT_CPU_PRESET),
        'cpu_preset_hd': _normalize_preset(
            context.get('cpu_preset_hd'), _DEFAULT_CPU_PRESET_HD
        ),
        'hw_quality_boost': _coerce_bool(context.get('hw_quality_boost', True), True),
        'hw_quality_level': _normalize_choice(
            context.get('hw_quality_level'), _VALID_HW_QUALITY_LEVELS, 'quality'
        ),
        'x264_tune': _normalize_x264_tune(context.get('x264_tune')),
        'amd_backend': _normalize_choice(
            context.get('amd_backend'), ('amf', 'vaapi', 'none'), 'amf'
        ),
    }


def _build_cpu(settings):
    """libx264 参数。"""
    preset = settings['cpu_preset']
    if (
        settings['height'] >= _HD_PRESET_MIN_HEIGHT
        and settings['duration_s'] is not None
        and settings['duration_s'] > _HD_PRESET_MIN_DURATION_S
    ):
        preset = settings['cpu_preset_hd']

    params = ['-c:v', 'libx264', '-preset', preset]
    if settings['x264_tune']:
        params += ['-tune', settings['x264_tune']]
    params += [
        '-crf', format_quality_value(settings['quality']),
        '-fps_mode', 'cfr',
        '-profile:v', 'high',
        '-bf', '2',
        '-g', str(settings['gop']),
        '-pix_fmt', 'yuv420p',
    ]
    params += list(_X264_QUALITY_PARAMS)
    return params


def _build_nvidia(settings):
    """hevc_nvenc 参数。"""
    params = [
        '-c:v', 'hevc_nvenc',
        '-preset', _NVENC_PRESET_BY_LEVEL[settings['hw_quality_level']],
        '-tune', 'hq',
        '-rc:v', 'vbr',
        '-b:v', '0',
        '-cq:v', format_quality_value(settings['quality']),
        '-fps_mode', 'cfr',
        '-profile:v', 'main',
        '-bf', '2',
        '-g', str(settings['gop_hevc']),
        '-pix_fmt', 'yuv420p',
        '-tag:v', 'hvc1',
    ]
    if settings['hw_quality_boost']:
        params += list(_NVENC_BOOST_PARAMS)
    return params


def _build_intel(settings):
    """hevc_qsv 参数。"""
    params = [
        '-c:v', 'hevc_qsv',
        '-preset', _QSV_PRESET_BY_LEVEL[settings['hw_quality_level']],
        '-global_quality', str(int(round(settings['quality']))),
        '-fps_mode', 'cfr',
        '-profile:v', 'main',
        '-bf', '2',
        '-g', str(settings['gop_hevc']),
        '-pix_fmt', 'nv12',
        '-tag:v', 'hvc1',
    ]
    if settings['hw_quality_boost']:
        params += list(_QSV_BOOST_PARAMS)
    else:
        # 未开启增强时保持历史行为：显式传 -look_ahead 0。
        params += list(_QSV_BASELINE_LOOKAHEAD)
    return params


def _build_amd_amf(settings):
    """hevc_amf 参数。"""
    params = [
        '-c:v', 'hevc_amf',
        '-usage', 'transcoding',
        '-quality', _AMF_QUALITY_BY_LEVEL[settings['hw_quality_level']],
        '-rc', 'hqvbr' if settings['hw_quality_boost'] else 'qvbr',
        '-qvbr_quality_level', str(int(round(settings['quality']))),
        '-fps_mode', 'cfr',
        '-profile:v', 'main',
        '-g', str(settings['gop_hevc']),
        '-pix_fmt', 'yuv420p',
        '-tag:v', 'hvc1',
    ]
    if settings['hw_quality_boost']:
        params += list(_AMF_BOOST_PARAMS)
    return params


def _build_amd_vaapi(settings):
    """hevc_vaapi 参数（不输出 -pix_fmt，走 hwupload 滤镜链）。"""
    params = [
        '-vaapi_device', _VAAPI_DEVICE,
        '-c:v', 'hevc_vaapi',
        '-rc_mode', 'CQP',
        '-qp', str(int(round(settings['quality']))),
        '-fps_mode', 'cfr',
        '-profile:v', 'main',
        '-g', str(settings['gop_hevc']),
        '-tag:v', 'hvc1',
    ]
    if settings['hw_quality_boost']:
        params += list(_VAAPI_BOOST_PARAMS)
    return params


def build_encoder_params(encoder_key, ctx):
    """构造视频编码参数 list[str]。

    encoder_key 取 'cpu' | 'nvidia' | 'intel' | 'amd'，其他值（含 None/非字符串）
    一律按 'cpu' 处理。ctx 字段：height、gop、gop_hevc、quality_mode、
    quality_value、cpu_preset、cpu_preset_hd、duration_s、hw_quality_boost、
    hw_quality_level、x264_tune、custom_params（list[str]|None）、amd_backend。

    custom_params 非空时完全覆盖内置参数并原样返回其内容副本（保持历史优先关系）。
    ctx 缺失字段使用默认值（height<=0 -> 1080、gop<=0 -> 48、gop_hevc<=0 -> 96、
    quality_mode='auto'、preset='medium'、hw_quality_boost=True、
    hw_quality_level='quality'）。amd_backend 为 'none' 或未知值时统一回退到
    'amf' 分支（调用方负责不可用回退）。本函数绝不抛异常。
    """
    custom_params = _normalize_custom_params(
        ctx.get('custom_params') if isinstance(ctx, dict) else None
    )
    if custom_params:
        return list(custom_params)

    settings = _resolve_context(ctx)

    key = encoder_key.strip().lower() if isinstance(encoder_key, str) else ''
    if key not in ('cpu', 'nvidia', 'intel', 'amd'):
        key = 'cpu'

    if key == 'nvidia':
        return _build_nvidia(settings)
    if key == 'intel':
        return _build_intel(settings)
    if key == 'amd':
        if settings['amd_backend'] == 'vaapi':
            return _build_amd_vaapi(settings)
        return _build_amd_amf(settings)
    return _build_cpu(settings)


def _select_audio_target_bitrate(input_bit_rate):
    """按源码率上限选择目标 AAC 码率（与历史 _select_audio_target_bitrate 一致）。"""
    bit_rate = _coerce_int(input_bit_rate)
    if bit_rate is None or bit_rate <= 0:
        return '128k'
    if bit_rate >= 192000:
        return '192k'
    if bit_rate >= 160000:
        return '160k'
    if bit_rate >= 128000:
        return '128k'
    if bit_rate >= 96000:
        return '96k'
    return '64k'


def build_audio_params(audio_info):
    """构造音频参数，与 task_manager._build_audio_transcode_params 行为一致。

    codec_name=='aac' -> ['-c:a', 'copy']；否则 ['-c:a', 'aac', '-b:a', <目标码率>]
    加声道数（channels==1 -> '1'，否则 '2'）以及有 sample_rate 时的 '-ar'。
    audio_info 非 dict 时按空 dict 处理，本函数绝不抛异常。
    """
    info = audio_info if isinstance(audio_info, dict) else {}

    try:
        codec_name = str(info.get('codec_name') or '').strip().lower()
    except Exception:
        codec_name = ''
    if codec_name == 'aac':
        return ['-c:a', 'copy']

    params = ['-c:a', 'aac', '-b:a', _select_audio_target_bitrate(info.get('bit_rate'))]

    channels = _coerce_int(info.get('channels'))
    if channels == 1:
        params += ['-ac', '1']
    else:
        params += ['-ac', '2']

    sample_rate = _coerce_int(info.get('sample_rate'))
    if sample_rate:
        params += ['-ar', str(sample_rate)]

    return params
