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
  libx265：-crf/-preset/-tune/-profile/-bf/-g/-pix_fmt/-tag:v/-x265-params 合法；
    增强项**没有**独立 ffmpeg 选项，只能写进 -x265-params（键名见
    _X265_QUALITY_PAIRS）；-tune 合法域与 x264 **不同**，film 与 stillimage 会被
    直接拒绝（见 _VALID_X265_TUNES 的注释）。
  -vsync 已被 FFmpeg 标记弃用（输出 "-vsync is deprecated. Use -fps_mode"），
    本模块统一使用 -fps_mode cfr。

`-fps_mode` 是 FFmpeg 5.1 才引入的选项（5.0 及更早只认 `-vsync`）。仓库自带的
与自动下载的 ffmpeg（BtbN latest）都远高于该版本；但用户通过 `FFMPEG_LOCATION`
指向自备的旧 ffmpeg 时，`-fps_mode` 会触发 "Unrecognized option" 而直接失败 ——
该错误既不属于 `_KNOWN_HW_ENCODER_ERROR_PATTERNS`，CPU 路径的降级列表又为空
（见 task_manager._resolve_embed_retry_stages：非硬件编码器一律返回 []），
因此整任务失败且无任何重试。本模块只在文件内记录该前提，不做版本探测：
探测需要调用 subprocess，会破坏「纯函数、无副作用」的模块契约。
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

# CPU 软编码器选择。与 VIDEO_ENCODER（硬件编码器选择器）**正交**：后者取值
# auto/cpu/nvidia/intel/amd 表达「用哪块硬件」，本键表达「软编码时用哪个编码器」。
# 之所以不复用 VIDEO_ENCODER 增加一个 'cpu_x265' 取值：那会让一个键同时承担两件
# 事，而现有的 VIDEO_ENCODER 归一化分散在 config_manager 的两处校验与设置页控件
# 里，扩值要同时改三处语义，风险高于新增一个正交键。
_VALID_CPU_CODECS = ('x264', 'x265')
_DEFAULT_CPU_CODEC = 'x264'

# x265 的 -tune 合法域与 x264 **不是包含关系**：实测（N-123313）`film` 与
# `stillimage` 会让 libx265 直接失败（进程返回码非 0，整个任务失败），而它们
# 恰好在 x264 的白名单里。因此两张表必须分开，按当前 CPU 编码器取值校验。
_VALID_X265_TUNES = (
    'animation', 'grain', 'psnr', 'ssim', 'fastdecode', 'zerolatency',
)
_VALID_SOFTWARE_TUNES = {
    'x264': _VALID_X264_TUNES,
    'x265': _VALID_X265_TUNES,
}

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
# 这四项与硬件编码器的增强项一样属于「质量增强」，统一受 hw_quality_boost
# 总开关控制：关闭时回到与基线逐字一致的基础参数（见 _build_cpu）。
_X264_QUALITY_PARAMS = [
    '-aq-mode', '3',
    '-aq-strength', '0.8',
    '-psy-rd', '1.0:0.0',
    '-rc-lookahead', '40',
]

# x265 的质量增强项。与 x264 的关键差异：
#
# 1. x265 **没有**独立 ffmpeg 选项（-aq-mode / -psy-rd 之类都不存在，实测
#    "Option not found"），只能写进 -x265-params；而 VUI 补写也走同一个选项，
#    所以两者必须合并成**一条** —— 实测两次给 -x265-params 时后者完全覆盖前者
#    （`-x265-params colorprim=bt709 -x265-params transfer=bt709` 之后 primaries
#    为空），分成两条会静默丢字段。
# 2. x264 的 psy-rd 写作 "1.0:0.0"（rd:rdoq 两个子参数），但 -x265-params 用
#    冒号分隔键值对，照搬会切碎参数串（实测 psnr 编码直接失败，返回码非 0）。
#    x265 有独立的 psy-rdoq 键，因此这里拆成两项，语义与 x264 写法等价。
_X265_QUALITY_PAIRS = (
    ('aq-mode', '3'),
    ('aq-strength', '0.8'),
    ('psy-rd', '1.0'),
    ('psy-rdoq', '0.0'),
    ('rc-lookahead', '40'),
)

_VAAPI_DEVICE = '/dev/dri/renderD128'

# 色彩元数据映射。
#
# 三张表**必须分开**：ffmpeg 里 colorspace / color_primaries / color_trc 是三个
# 语义不同的字段，合法取值集合并不相同（例如 `bt2020nc` 是合法的 colorspace
# 却不是合法的 primaries）。此前三者共用一张表，会把 ffmpeg 直接拒绝的值写进
# 命令，而且该失败不在 `_KNOWN_HW_ENCODER_ERROR_PATTERNS` 内、CPU 路径的降级
# 列表又为空 —— 源素材 color_space 为对应取值时整任务失败且无任何重试。
#
# 取值域由仓库自带 ffmpeg（N-123313）在「libx264 + yuv420p」输出组合下逐个实测
# 得到（tests/test_video_encoder_params.py 的冒烟断言锁定同一集合）。实测中
# 不合法取值有两类失败：`Invalid argument`（常量名不认识）与 `Conversion failed!`
# （取值在 AVCOL 枚举里有定义，但与 yuv420p 转换不兼容）—— 两类都会让转码失败，
# 因此都不得进表。
_COLORSPACE_VALUES = frozenset((
    'bt709', 'bt470bg', 'smpte170m', 'smpte240m', 'bt2020nc', 'rgb',
))
_PRIMARIES_VALUES = frozenset((
    'bt709', 'bt470m', 'bt470bg', 'smpte170m', 'smpte240m', 'film',
    'bt2020', 'smpte428', 'smpte428_1', 'smpte431', 'smpte432',
    'jedec-p22', 'ebu3213',
))
_TRC_VALUES = frozenset((
    'bt709', 'gamma22', 'gamma28', 'smpte170m', 'smpte240m', 'linear',
    'log', 'log_sqrt', 'iec61966_2_4', 'bt1361', 'iec61966_2_1',
    'smpte2084', 'smpte428', 'smpte428_1', 'arib-std-b67',
))
_COLOR_RANGE_ALIASES = {'tv': 'tv', 'limited': 'tv', 'pc': 'pc', 'full': 'pc'}
_SKIPPED_COLOR_TOKENS = frozenset(('', 'unknown', 'unspecified', 'reserved', 'n/a'))

# 字段 -> 合法取值表。resolve_color_metadata / build_color_vui_params 共用，
# 保证「校验用的集合」与「实际写入的值」永远来自同一处。
_COLOR_FIELD_WHITELISTS = {
    'colorspace': _COLORSPACE_VALUES,
    'color_primaries': _PRIMARIES_VALUES,
    'color_trc': _TRC_VALUES,
}

# 三张表里被 ffmpeg 通用选项（-colorspace/-color_primaries/-color_trc）接受的值，
# 名字却不一定被 x264 私有参数（-x264-params colorprim/transfer/colormatrix）接受：
# x264 用的是自己的枚举名。实测（N-123313 + libx264）下列取值会被 x264 以
# `Error parsing option '...'` 拒绝，但**进程返回码仍为 0**，VUI 里该字段留空 ——
# 也就是静默丢失，比直接报错更难发现（只能靠 ffprobe 回读 VUI 才能判定）。
#
#   colorprim  : smpte428_1 / jedec-p22 / ebu3213 被拒（jedec_p22 等变体同样被拒）
#   colormatrix: rgb 被拒，x264 里叫 gbr
#   transfer   : gamma22 / gamma28 / log / log_sqrt / iec61966_2_4 /
#                iec61966_2_1 / bt1361 / smpte428_1 被拒
#
# 下表给出等价改名（左边是 ffmpeg 规范名，右边是 x264 枚举名），实测改名后
# VUI 回读与目标语义一致。
_X264_PARAMS_ALIASES = {
    'colorspace': {
        'rgb': 'gbr',
    },
    'color_primaries': {
        'smpte428_1': 'smpte428',
    },
    'color_trc': {
        # ffmpeg 的 log / log_sqrt 即 AVC 的 log100 / log316。
        'log': 'log100',
        'log_sqrt': 'log316',
        # x264 的 transfer 枚举用连字符形式。
        'iec61966_2_4': 'iec61966-2-4',
        'iec61966_2_1': 'iec61966-2-1',
        # ffmpeg 的 bt1361 对应 x264 的 bt1361e。
        'bt1361': 'bt1361e',
        'smpte428_1': 'smpte428',
    },
}

# 在 x264 里没有任何等价名字的取值：跳过该键，绝不回退到错误语义的值。
# gamma22/gamma28 在 H.264 VUI 里没有独立编码，x264 未暴露对应枚举名；
# jedec-p22/ebu3213 只存在于 ffmpeg 的 AVColorPrimaries 枚举，x264 colorprim 无对应项。
_X264_PARAMS_UNSUPPORTED = {
    'color_primaries': frozenset(('jedec-p22', 'ebu3213')),
    'color_trc': frozenset(('gamma22', 'gamma28')),
}

# libx264 接受 x264 私有参数的两种选项名（`-x264opts` 是 `-x264-params` 的历史别名）。
# build_color_vui_params 用它判断用户是否已经自己指定了 x264 参数；只匹配
# `-x264-params` 会漏掉 `-x264opts`，导致两处同时给 x264 参数、后写的覆盖先写的。
_X264_PARAMS_OPTS = ('-x264-params', '-x264opts')

# x265 侧的三张表。取值由仓库自带 ffmpeg（N-123313）在「libx265 + yuv420p」输出
# 组合下逐个**回读 VUI** 得到（tests/test_x265_smoke.py 锁定同一集合）。
#
# 与 x264 的差异（实测逐条确认）：
#
#   colorprim  : 与 x264 一致 —— smpte428_1 / jedec-p22 / ebu3213 被静默丢弃
#                （返回码仍为 0，VUI 该字段留空）
#   transfer   : 与 x264 一致 —— gamma22 / gamma28 / log / log_sqrt /
#                iec61966_2_4 / iec61966_2_1 / bt1361 / smpte428_1 被静默丢弃
#   colormatrix: **与 x264 不同** —— x265 同时接受 ffmpeg 规范名 `rgb` 与
#                x264 的枚举名 `gbr`（两者回读都是 gbr），故此处不需要改名映射
#
# 所以 x265 的别名表比 x264 少一条 colormatrix 项；直接复用 x264 的表虽结果等价，
# 但会让映射表的注释与实际不符。
_X265_PARAMS_ALIASES = {
    'color_primaries': {
        'smpte428_1': 'smpte428',
    },
    'color_trc': {
        'log': 'log100',
        'log_sqrt': 'log316',
        'iec61966_2_4': 'iec61966-2-4',
        'iec61966_2_1': 'iec61966-2-1',
        'bt1361': 'bt1361e',
        'smpte428_1': 'smpte428',
    },
}

# 在 x265 里同样没有任何等价名字的取值。集合与 x264 一致（实测确认），但仍各自
# 成表：两者的枚举域独立演进，共用一张表会让「改了一边忘了另一边」变成静默丢字段。
_X265_PARAMS_UNSUPPORTED = {
    'color_primaries': frozenset(('jedec-p22', 'ebu3213')),
    'color_trc': frozenset(('gamma22', 'gamma28')),
}

# x265 私有参数的选项名。**没有** `-x265opts` 这个别名：实测该选项触发
# "Error splitting the argument list: Option not found"（返回码非 0），
# 与 x264 的 `-x264opts` 不是一回事，因此只登记一个。
_X265_PARAMS_OPTS = ('-x265-params',)

# 各软件编码器写私有参数的选项名，以及（选项名表、VUI 映射表）的归属。
_SOFTWARE_PRIVATE_OPT = {
    'x264': '-x264-params',
    'x265': '-x265-params',
}
_SOFTWARE_PRIVATE_OPTS = {
    'x264': _X264_PARAMS_OPTS,
    'x265': _X265_PARAMS_OPTS,
}
_SOFTWARE_VUI_TABLES = {
    'x264': (_X264_PARAMS_ALIASES, _X264_PARAMS_UNSUPPORTED),
    'x265': (_X265_PARAMS_ALIASES, _X265_PARAMS_UNSUPPORTED),
}

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
    """把任意值转换为整数；无法可靠转换返回 None。

    与 task_manager._coerce_int 存在两处**有意**的语义差异（更严格，避免静默错值）：

    - bool 直接判失败。bool 是 int 的子类，原实现里 `_coerce_int(True)` 会得到 1，
      使 `channels=True` 悄悄变成单声道、`gop=True` 变成 1 帧 GOP。
    - 浮点改为 `round()` 取最接近的整数，而非 `int()` 截断。截断对「2.0 声道输入」
      这类合法浮点是向下偏的（1.9 -> 1，把立体声写成单声道），round 语义更贴近
      「这个数最接近哪个整数」。

    调用点与受影响范围（实测）：

    - `channels`（build_audio_params）：1.9 由 `-ac 1` 变为 `-ac 2` —— 修复方向，
      双声道输入不再被降为单声道；2.4 两版都是 `-ac 2`。
    - `gop` / `gop_hevc`（_resolve_context）：47.9 由 47 变为 48；task_manager 传入的
      本就是整数，无实际影响。`gop=True` 由 1 变为回退默认 48。
    - `bit_rate`（_select_audio_target_bitrate）：191999.6 由 191999（160k 档）
      变为 192000（192k 档），会跨越码率阶梯边界；ffprobe 的 bit_rate 恒为整数字符串，
      现实中不触发。
    - `sample_rate`（build_audio_params）：48000.7 由 48000 变为 48001。同上，
      ffprobe 恒为整数；该差异只在人为构造的浮点输入下可见。

    异常边界：本模块声明「公开函数对任意输入都不抛异常」，而 `value == ''` 这类
    比较本身就可能被恶意对象触发任意异常（`__eq__` 抛 RuntimeError 是既有测试
    tests/test_video_encoder_params.py::ParseEncoderConfigTests 里就在用的手法）。
    原实现只捕获 (TypeError, ValueError, OverflowError)，`build_encoder_params` /
    `build_audio_params` 会因此把 RuntimeError 泄露给调用方 —— 这里放宽到
    Exception（不拦 BaseException，Ctrl-C 等仍正常传播）。
    """
    try:
        if isinstance(value, bool):
            return None
        if value is None or value == '':
            return None
        if isinstance(value, float):
            return int(round(value)) if math.isfinite(value) else None
        return int(value)
    except Exception:
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


def normalize_cpu_codec(value):
    """归一化 CPU 软编码器取值（'x264' / 'x265'），非法值回退默认。

    公开函数：task_manager 的降级链需要按它判断「是否还有 x265 -> x264 这一级」，
    跨模块复用同一处归一化，避免两边各写一份判断而分叉。
    """
    return _normalize_choice(value, _VALID_CPU_CODECS, _DEFAULT_CPU_CODEC)


def _normalize_preset(value, default):
    """归一化软编码 preset。

    x264 与 x265 接受同一组 9 个取值（实测逐个确认）。x265 还额外接受
    `placebo`，但该项未开放为可配置值：它比 veryslow 更慢而收益有限，且
    _VALID_CPU_PRESETS 与设置页控件、config_manager 校验共用同一集合，
    开放它需要同步改三处，超出本次范围。
    """
    return _normalize_choice(value, _VALID_CPU_PRESETS, default)


def _normalize_x264_tune(value):
    """归一化 x264 tune，非法或缺失一律为空字符串（表示不传 -tune）。"""
    return _normalize_software_tune(value, 'x264')


def _normalize_software_tune(value, cpu_codec):
    """按 CPU 编码器归一化 tune，非法或缺失一律为空字符串（表示不传 -tune）。

    两张白名单必须分开取值：`film` 与 `stillimage` 在 x264 里合法，在 x265 里会
    让编码直接失败（实测返回码非 0）。因此用户把 CPU 编码器从 x264 切到 x265 时，
    原本合法的 tune 会被这里**丢弃**而不是透传 —— 宁可少一个调优参数，也不能让
    整条烧录链因为一个 -tune 取值而失败（CPU 路径没有更下一级的降级可用）。
    调用方（task_manager）会把这种丢弃记进任务日志，不让它无声发生。
    """
    codec = normalize_cpu_codec(cpu_codec)
    try:
        text = str(value).strip().lower() if value is not None else ''
    except Exception:
        return ''
    return text if text in _VALID_SOFTWARE_TUNES[codec] else ''


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
    """把自定义参数规范成 list[str]；无效或为空返回 None。

    列表分支按 `str(item).strip()` 过滤空项（含纯空白项），与字符串分支
    （经 shlex.split 天然不产生空串）保持一致 —— 否则 `['  ']` 会被整份当作
    视频参数返回，把 `-c:v ...` 全部顶掉且不产生任何可读的错误。
    非空白项保持原样，不改写用户配置的字符。
    """
    if isinstance(value, (list, tuple)):
        try:
            items = [
                str(item) for item in value
                if item is not None and str(item).strip()
            ]
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
    """把质量值格式化为字符串；非数值输入返回空字符串。

    整数值不带小数（23.0 -> '23'），否则最多保留 2 位小数并去掉尾随 0
    （23.5 -> '23.5'，23.456 -> '23.46'）。

    非数值（None / 布尔 / 无法解析的字符串 / 容器 / NaN / Inf）**一律返回 ''**，
    绝不把原文回显进 `-crf` / `-cq:v` 的参数位 —— 原实现返回 `str(q)`，会让
    `-crf abc` 这种命令被 ffmpeg 以 "Invalid argument" 拒绝，且该错误不在
    `_KNOWN_HW_ENCODER_ERROR_PATTERNS` 内，硬件路径的降级重试救不回来。

    布尔单独拦掉：True/False 是 int 子类，`_coerce_number` 已排除它们，但若只依赖
    该排除，`str(True)` 仍会落到字符串回显分支。

    模块内两个调用点（_build_cpu 的 `-crf`、_build_nvidia 的 `-cq:v`）传入的都是
    _resolve_context 归一化后的有限 float，必然非空；两处仍各留一层回退，
    保证任何未来改动都不会写出空参数值。
    """
    if isinstance(q, bool):
        return ''
    number = _coerce_number(q)
    if number is None:
        return ''
    if float(number).is_integer():
        return str(int(number))
    text = f'{number:.2f}'.rstrip('0').rstrip('.')
    return text or '0'


def _format_quality_or_recommended(quality, height):
    """格式化质量值，万一为空则回退到按高度推荐值，永不返回空串。"""
    text = format_quality_value(quality)
    if text:
        return text
    return format_quality_value(recommend_quality(height)) or str(FALLBACK_QUALITY)


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
    VIDEO_CPU_CODEC、VIDEO_CPU_PRESET、VIDEO_CPU_PRESET_HD、VIDEO_QUALITY_MODE、
    VIDEO_QUALITY_VALUE、VIDEO_HW_QUALITY_BOOST、VIDEO_HW_QUALITY_LEVEL、
    VIDEO_COLOR_METADATA_MODE、VIDEO_CUSTOM_PARAMS_ENABLED、VIDEO_CUSTOM_PARAMS、
    VIDEO_X264_TUNE。返回键：encoder_pref、cpu_codec、cpu_preset、cpu_preset_hd、
    quality_mode、quality_value、hw_quality_boost、hw_quality_level、
    color_metadata_mode、custom_params_enabled、custom_params、software_tune、
    x264_tune（= software_tune 的旧名，恒等）。

    布尔解析宽容（'true'/'false'/'1'/'0'/'yes'/'no'/'on'/'off'，大小写不敏感），
    非法值回退默认。本函数绝不抛异常。
    """
    cfg = config if isinstance(config, dict) else {}

    quality_mode = _normalize_choice(
        cfg.get('VIDEO_QUALITY_MODE'), _VALID_QUALITY_MODES, 'auto'
    )
    cpu_codec = normalize_cpu_codec(cfg.get('VIDEO_CPU_CODEC'))
    # tune 按 CPU 编码器各自的白名单校验：x264 的 film/stillimage 在 x265 上会让
    # 编码直接失败，见 _normalize_software_tune。
    software_tune = _normalize_software_tune(cfg.get('VIDEO_X264_TUNE'), cpu_codec)

    return {
        'encoder_pref': _normalize_choice(cfg.get('VIDEO_ENCODER'), _VALID_ENCODERS, 'auto'),
        'cpu_codec': cpu_codec,
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
        'software_tune': software_tune,
        # `x264_tune` 是 `software_tune` 的旧名（历史调用点与测试仍读它）。
        # 两者恒等，新代码请用 software_tune。
        'x264_tune': software_tune,
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
    if space in _COLORSPACE_VALUES:
        resolved['colorspace'] = space

    primaries = _normalize_color_token(info.get('color_primaries'))
    if primaries in _PRIMARIES_VALUES:
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


def _custom_params_declare_private_opts(custom_params, opts):
    """自定义参数里是否已经出现某个私有参数选项（含 `-opt=value` 连写形式）。"""
    tokens = custom_params if isinstance(custom_params, (list, tuple)) else []
    for token in tokens:
        try:
            text = str(token).strip().lower()
        except Exception:
            continue
        if any(opt in text for opt in opts):
            return True
    return False


def _vui_param_pairs(cpu_codec, color_map, custom_params=None):
    """算出软件编码要写进私有参数的 VUI 键值对 list[(key, value)]。

    返回 [] 的三种情形：用户自定义参数里已经指定了该编码器的私有参数选项
    （不与用户的显式配置互相覆盖）；color_map 为空；字段在该编码器里没有等价
    枚举名（跳过该键，绝不改写成语义不符的值）。

    键顺序固定为 colorprim、transfer、colormatrix；该顺序被
    tests/test_video_encoder_params.py::X264ColorValueMappingTests 锁定。
    """
    codec = normalize_cpu_codec(cpu_codec)
    aliases, unsupported = _SOFTWARE_VUI_TABLES[codec]
    if _custom_params_declare_private_opts(custom_params, _SOFTWARE_PRIVATE_OPTS[codec]):
        return []

    resolved = color_map if isinstance(color_map, dict) else {}
    pairs = []
    for field, entry_key in (
        ('color_primaries', 'colorprim'),
        ('color_trc', 'transfer'),
        ('colorspace', 'colormatrix'),
    ):
        try:
            value = resolved.get(field)
        except Exception:
            continue
        # 只接受非空字符串。这里必须显式做类型判断，不能只写 `if not value`：
        # `value in frozenset(...)` 会先调用 value.__hash__，`dict.get(value)` 同样
        # 需要 hashable —— 一个自定义 __eq__/__hash__ 的对象能让本模块的公开函数
        # （build_encoder_params / build_color_vui_params）抛异常，违背
        # 「公开函数对任意输入都不抛异常」的契约。顺带也挡掉把数字/容器当成
        # 枚举名写进命令行的情况。
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value:
            continue
        if value in unsupported.get(field, ()):
            continue
        pairs.append((entry_key, aliases.get(field, {}).get(value, value)))
    return pairs


def _private_param_option(cpu_codec, pairs):
    """把键值对拼成 `['-x26?-params', 'a=b:c=d']`；pairs 为空时返回 []。"""
    if not pairs:
        return []
    codec = normalize_cpu_codec(cpu_codec)
    return [
        _SOFTWARE_PRIVATE_OPT[codec],
        ':'.join(f'{key}={value}' for key, value in pairs),
    ]


def build_color_vui_params(encoder_key, color_map, custom_params=None, cpu_codec=None):
    """返回编码器私有参数，确保色彩原色/传递特性真正写进码流 VUI。

    实测（FFmpeg N-123313）：libx264 与 libx265 的包装器都只把 avctx->colorspace
    转发给 VUI，`-color_primaries` / `-color_trc` 会被静默丢弃 —— 输出文件里
    色域正确但原色与传递特性缺失，播放器仍可能按默认值解释。因此软件编码路径
    必须用编码器私有参数补写：

        -x264-params colorprim=bt709:transfer=bt709:colormatrix=bt709

    写进去的值必须是**该编码器自己的枚举名**，与三张白名单里的 ffmpeg 规范名
    不完全一致；别名表负责改名，无等价名的取值直接跳过该键（编码器对未知名只打印
    "Error parsing option" 并**继续返回 0**，静默丢字段，比报错更隐蔽）。

    cpu_codec 指定目标编码器（'x264' / 'x265'），默认 'x264'，输出选项名随之改变
    （-x264-params / -x265-params）。两张 VUI 表并不相同，见
    _X265_PARAMS_ALIASES 的注释。

    **x265 的调用方注意**：x265 没有独立的质量增强选项，增强项与 VUI 必须写进
    同一条 -x265-params（实测两条选项时后者完全覆盖前者）。单独调用本函数再另行
    追加增强参数会静默丢字段；x265 的完整参数请走 build_encoder_params 的 cpu
    分支，它内部把两者合并成一条。本函数保留给「只要 VUI 片段」的测试与诊断场景。

    同时 range 无法通过私有参数生效（实测 x264 的 range=pc 被忽略；x265 的
    range=pc 被当作非法值回退成 tv），故色彩范围仍由 resolve_color_metadata 的
    通用 `-color_range` 负责，这里不重复输出。

    硬件编码器由通用选项负责，返回 []。
    """
    key = encoder_key.strip().lower() if isinstance(encoder_key, str) else ''
    if key != 'cpu':
        # 只有软件编码器需要私有参数补写；其他值（含非法值）按非软件编码处理
        return []
    codec = normalize_cpu_codec(cpu_codec)
    return _private_param_option(codec, _vui_param_pairs(codec, color_map, custom_params))


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

    cpu_codec = normalize_cpu_codec(context.get('cpu_codec'))
    # tune 兼容旧字段名 `x264_tune`；新代码传 `software_tune`。
    raw_tune = context.get('software_tune')
    if raw_tune is None:
        raw_tune = context.get('x264_tune')

    return {
        'height': height,
        'gop': gop,
        'gop_hevc': gop_hevc,
        'quality': quality,
        'duration_s': duration,
        'cpu_codec': cpu_codec,
        'cpu_preset': _normalize_preset(context.get('cpu_preset'), _DEFAULT_CPU_PRESET),
        'cpu_preset_hd': _normalize_preset(
            context.get('cpu_preset_hd'), _DEFAULT_CPU_PRESET_HD
        ),
        'hw_quality_boost': _coerce_bool(context.get('hw_quality_boost', True), True),
        'hw_quality_level': _normalize_choice(
            context.get('hw_quality_level'), _VALID_HW_QUALITY_LEVELS, 'quality'
        ),
        'software_tune': _normalize_software_tune(raw_tune, cpu_codec),
        'amd_backend': _normalize_choice(
            context.get('amd_backend'), ('amf', 'vaapi', 'none'), 'amf'
        ),
    }


def _resolve_cpu_preset(settings):
    """按分辨率与时长选择软编码 preset。

    1440p 及以上且超过 10 分钟时走 cpu_preset_hd（默认 veryfast），该档位存在的
    理由是让长视频的字幕烧录不至于超时。x264 与 x265 共用这同一套判定。
    """
    if (
        settings['height'] >= _HD_PRESET_MIN_HEIGHT
        and settings['duration_s'] is not None
        and settings['duration_s'] > _HD_PRESET_MIN_DURATION_S
    ):
        return settings['cpu_preset_hd']
    return settings['cpu_preset']


def _build_cpu_x264(settings, vui_pairs=()):
    """libx264 参数。

    四项 x264 质量增强（-aq-mode/-aq-strength/-psy-rd/-rc-lookahead）与
    NVENC/QSV/AMF/VAAPI 的增强项一样，受 hw_quality_boost 总开关控制：关闭时
    回到与基线（origin/main 的 build_cpu_params）逐字一致的基础参数。

    为什么关闭开关必须真的去掉这四项（实测 N-123313 + libx264）：

    - `-preset veryfast`（VIDEO_CPU_PRESET_HD 路径）下 x264 默认 rc_lookahead=10，
      `-rc-lookahead 40` 会把它抬到 40；而该 preset 存在的理由正是「1440p+ 长视频
      避免字幕烧录超时」，前瞻翻 4 倍与初衷相反 —— 用户关掉增强时应当能甩掉它。
    - `-preset medium` 下 rc_lookahead 默认已是 40，新增项不改变该值，
      所以这项影响只在 veryfast 路径可见；aq-mode=3 两条路径都生效。

    这四项用的是**独立 ffmpeg 选项**，与 VUI 的 `-x264-params` 不是同一个选项，
    因此两者可以并存、互不覆盖（x265 没这个便利，见 _build_cpu_x265）。
    """
    params = ['-c:v', 'libx264', '-preset', _resolve_cpu_preset(settings)]
    if settings['software_tune']:
        params += ['-tune', settings['software_tune']]
    params += [
        '-crf', _format_quality_or_recommended(settings['quality'], settings['height']),
        '-fps_mode', 'cfr',
        '-profile:v', 'high',
        '-bf', '2',
        '-g', str(settings['gop']),
        '-pix_fmt', 'yuv420p',
    ]
    if settings['hw_quality_boost']:
        params += list(_X264_QUALITY_PARAMS)
    params += _private_param_option('x264', list(vui_pairs))
    return params


def _build_cpu_x265(settings, vui_pairs=()):
    """libx265 参数（HEVC 软编码）。

    与 _build_cpu_x264 的三处结构性差异：

    1. 质量增强项**没有**独立 ffmpeg 选项，只能写进 -x265-params；而 VUI 补写也走
       同一个选项，所以两者必须合并成一条字符串 —— 实测给两次 -x265-params 时后者
       完全覆盖前者，分成两条会静默丢掉先写的那批键。这就是本函数不把 VUI 交给
       build_color_vui_params 单独输出的原因。
    2. `-g` 用 gop_hevc：输出是 HEVC，关键帧间隔与硬件 HEVC 路径保持一致（96），
       而 x264 路径用 gop（48）。
    3. 多出 `-tag:v hvc1`：与硬件 HEVC 路径一致，保证 mp4 里的 hvc1 标签，这是
       Safari / QuickTime 系播放器识别 HEVC 的前提。

    `-profile:v main` 与 yuv420p 组合下实测输出 profile 为 Main；x265 默认档位在
    yuv420p 下同样是 Main，显式写出是为了不让默认值随 FFmpeg 版本漂移。

    **CRF 不做偏移**：x265 的 CRF 标度与 x264 不可直接比较，但实测（N-123313，
    testsrc2 合成图文素材，SSIM 匹配，veryfast 与 medium 两个 preset）并不支持
    「加一个固定偏移即可等价」：veryfast 下同 CRF 的体积是 x264 的 1.6~1.8 倍，
    medium 下约 1.0 倍。趋势随 preset 与素材大幅变化，故不引入任何自造偏移量 ——
    那只会把一个未经证实的常数固化进行为。需要更小的体积时调 VIDEO_QUALITY_VALUE。
    """
    params = ['-c:v', 'libx265', '-preset', _resolve_cpu_preset(settings)]
    if settings['software_tune']:
        params += ['-tune', settings['software_tune']]
    params += [
        '-crf', _format_quality_or_recommended(settings['quality'], settings['height']),
        '-fps_mode', 'cfr',
        '-profile:v', 'main',
        '-bf', '2',
        '-g', str(settings['gop_hevc']),
        '-pix_fmt', 'yuv420p',
        '-tag:v', 'hvc1',
    ]
    pairs = list(_X265_QUALITY_PAIRS) if settings['hw_quality_boost'] else []
    pairs += list(vui_pairs)
    params += _private_param_option('x265', pairs)
    return params


def _build_cpu(settings, vui_pairs=()):
    """按 settings['cpu_codec'] 分派到对应的软件编码器。

    保留本入口是为了让「CPU 软编码」在调用方看来仍是单一分支，避免 task_manager
    里到处出现 codec 判断。
    """
    if settings['cpu_codec'] == 'x265':
        return _build_cpu_x265(settings, vui_pairs)
    return _build_cpu_x264(settings, vui_pairs)


def _build_nvidia(settings):
    """hevc_nvenc 参数。"""
    params = [
        '-c:v', 'hevc_nvenc',
        '-preset', _NVENC_PRESET_BY_LEVEL[settings['hw_quality_level']],
        '-tune', 'hq',
        '-rc:v', 'vbr',
        '-b:v', '0',
        '-cq:v', _format_quality_or_recommended(settings['quality'], settings['height']),
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
    hw_quality_level、cpu_codec、software_tune（兼容旧名 x264_tune）、color_map、
    custom_params（list[str]|None）、amd_backend。

    custom_params 非空时覆盖内置编码参数并原样返回其内容副本（保持历史优先关系），
    但**仍会**追加软件编码的色彩 VUI 私有参数 —— 与重构前的行为一致（那时 VUI 由
    task_manager 在 build_encoder_params 之外追加）；用户在自己的自定义参数里已经
    写了 `-x264-params` / `-x265-params`（含 `-x264opts`）时不追加，不覆盖用户的
    显式配置。硬件编码器不会收到任何软件私有参数。

    color_map 为 resolve_color_metadata 的产物：软件编码（cpu）据此补写 VUI
    （libx264/libx265 都只转发 colorspace，会静默丢弃 primaries/trc），x265 的
    质量增强与 VUI 在本函数内合并成同一条 -x265-params（两条会互相覆盖）。

    ctx 缺失字段使用默认值（height<=0 -> 1080、gop<=0 -> 48、gop_hevc<=0 -> 96、
    quality_mode='auto'、preset='medium'、hw_quality_boost=True、
    hw_quality_level='quality'、cpu_codec='x264'）。amd_backend 为 'none' 或未知值时
    统一回退到 'amf' 分支（调用方负责不可用回退）。本函数绝不抛异常。

    hw_quality_boost 是**唯一**的质量增强总开关，六个分支（cpu-x264 / cpu-x265 /
    nvidia / intel / amd-amf / amd-vaapi）一律受它控制：关闭时回到与基线逐字一致的
    基础参数。默认 True 且 cpu_codec='x264' 的输出与历史（含 _CPU_1080_EXPECTED
    快照）保持一致。
    """
    context = ctx if isinstance(ctx, dict) else {}
    settings = _resolve_context(context)
    color_map = context.get('color_map')
    custom_params = _normalize_custom_params(context.get('custom_params'))

    key = encoder_key.strip().lower() if isinstance(encoder_key, str) else ''
    if key not in ('cpu', 'nvidia', 'intel', 'amd'):
        key = 'cpu'

    if custom_params:
        params = list(custom_params)
        if key == 'cpu':
            params += _private_param_option(
                settings['cpu_codec'],
                _vui_param_pairs(settings['cpu_codec'], color_map, custom_params),
            )
        return params

    if key == 'nvidia':
        return _build_nvidia(settings)
    if key == 'intel':
        return _build_intel(settings)
    if key == 'amd':
        if settings['amd_backend'] == 'vaapi':
            return _build_amd_vaapi(settings)
        return _build_amd_amf(settings)
    return _build_cpu(
        settings, _vui_param_pairs(settings['cpu_codec'], color_map, custom_params)
    )


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
