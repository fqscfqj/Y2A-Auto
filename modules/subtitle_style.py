#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""字幕外观解析与应用（纯函数）。

职责：把用户可配置的字幕外观（字号倍率、颜色、描边、阴影、底框）解析为
规范化字典，应用到 ``task_manager._build_streaming_ass_style`` 产出的基础
样式 dict 上，并序列化为 FFmpeg 的 ``force_style`` 字面量。

约束：
- 只依赖标准库，不导入任何项目内模块（避免与 task_manager 循环依赖）；
- 所有函数均为纯函数且不抛异常，非法输入一律回退默认值；
- 默认配置下 ``apply_style_overrides`` 的输出与输入样式在语义上完全一致。
"""

from typing import Any, Dict, Iterable, List, Optional

# ASS alpha：00=完全不透明，FF=完全透明
ASS_HEX_ALPHA_OPAQUE = '00'
# 描边固定半透明，保持现状 &HB2000000 的观感（用户只改色相不改透明度）
ASS_OUTLINE_ALPHA = 'B2'

DEFAULT_FONT_COLOR = '#FFFFFF'
DEFAULT_OUTLINE_COLOR = '#000000'
DEFAULT_BACKGROUND_COLOR = '#000000'

# 任何归一化失败时的最终兜底颜色
_FALLBACK_COLOR = '#FFFFFF'

_HEX_DIGITS = '0123456789ABCDEF'

FONT_SIZE_SCALE_RANGE = (0.5, 2.0)
MARGIN_V_SCALE_RANGE = (0.5, 2.0)
OUTLINE_SCALE_RANGE = (0.0, 3.0)
SHADOW_SCALE_RANGE = (0.0, 3.0)
BACKGROUND_OPACITY_RANGE = (0.0, 1.0)

# force_style 唯一允许的键顺序（稳定可测）
FORCE_STYLE_KEY_ORDER = (
    'FontName',
    'FontSize',
    'Outline',
    'Shadow',
    'MarginL',
    'MarginR',
    'MarginV',
    'Alignment',
    'BorderStyle',
    'PrimaryColour',
    'OutlineColour',
    'BackColour',
)

# 需要按 ASS 数字规则格式化的键，以及需要取整的键
_FLOAT_STYLE_KEYS = frozenset({'FontSize', 'Outline', 'Shadow'})
_INT_STYLE_KEYS = frozenset({'MarginL', 'MarginR', 'MarginV'})

_TRUE_TOKENS = frozenset({'true', '1', 'yes', 'on'})
_FALSE_TOKENS = frozenset({'false', '0', 'no', 'off'})

# 外观覆盖项的原始配置键 -> 返回值键
_COLOR_CONFIG_KEYS = (
    ('SUBTITLE_FONT_COLOR', '字体颜色', DEFAULT_FONT_COLOR),
    ('SUBTITLE_OUTLINE_COLOR', '描边颜色', DEFAULT_OUTLINE_COLOR),
    ('SUBTITLE_BACKGROUND_COLOR', '底框颜色', DEFAULT_BACKGROUND_COLOR),
)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _is_finite(number: float) -> bool:
    # NaN 自身不相等；无穷用比较拦截
    return number == number and number not in (float('inf'), float('-inf'))


def _normalize_color_candidate(value: Any) -> Optional[str]:
    """把单个候选值归一化为 '#RRGGBB'，非法返回 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith('#'):
        text = text[1:]
    if len(text) == 3:
        text = ''.join(char * 2 for char in text)
    if len(text) != 6:
        # 8 位（AARRGGBB）按规格一律视为非法，避免 alpha 语义歧义
        return None
    upper = text.upper()
    for char in upper:
        if char not in _HEX_DIGITS:
            return None
    return '#' + upper


def _coerce_float(
    value: Any,
    default: float,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    """宽容解析数值并夹紧到区间；非数字/布尔回退默认值。"""
    if isinstance(value, bool) or value is None:
        number = float(default)
    else:
        try:
            number = float(value)
        except Exception:
            number = float(default)
    if not _is_finite(number):
        number = float(default)
    if minimum is not None and maximum is not None:
        number = _clamp(number, minimum, maximum)
    return number


def _optional_float(value: Any) -> Optional[float]:
    """解析样式 dict 里的原始数值；不可用时返回 None（表示保持原值）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except Exception:
            return None
    else:
        return None
    if not _is_finite(number):
        return None
    return number


def _coerce_bool(value: Any, default: bool) -> bool:
    """宽容解析布尔；不做 bool(value) 强转（'false' 必须为 False）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
        return default
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
    return default


def _format_number(value: Any) -> str:
    """整数不带小数，否则最多 2 位小数并去掉尾随 0。"""
    try:
        number = float(value)
    except Exception:
        return str(value)
    if not _is_finite(number):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f'{number:.2f}'.rstrip('0').rstrip('.')


def _opacity_to_ass_alpha(opacity: Any) -> str:
    """底框不透明度换算为 ASS alpha（1.0 不透明 -> '00'，0.0 -> 'FF'）。"""
    opacity_value = _clamp(_coerce_float(opacity, 0.5), *BACKGROUND_OPACITY_RANGE)
    level = int(round((1.0 - opacity_value) * 255))
    level = int(_clamp(level, 0, 255))
    return f'{level:02X}'


def normalize_hex_color(value: Any, default: Any = DEFAULT_FONT_COLOR) -> str:
    """把用户输入的颜色归一化为 '#RRGGBB' 大写形式。

    接受 '#RRGGBB' / 'RRGGBB' / '#RGB' / 'RGB'，允许首尾空白与任意大小写；
    8 位 AARRGGBB 视为非法（返回 default 的归一化结果）；非法输入、
    None、空串、非字符串、含非法字符同样回退 default；default 也非法时
    兜底 '#FFFFFF'。绝不抛异常。
    """
    normalized = _normalize_color_candidate(value)
    if normalized is not None:
        return normalized
    fallback = _normalize_color_candidate(default)
    if fallback is not None:
        return fallback
    return _FALLBACK_COLOR


def hex_to_ass_color(
    value: Any,
    default: Any = DEFAULT_FONT_COLOR,
    alpha: Any = ASS_HEX_ALPHA_OPAQUE,
) -> str:
    """把 '#RRGGBB' 转成 ASS 颜色字面量 '&HAABBGGRR'。

    ASS 是 BGR 排列且 alpha 反相（00 不透明、FF 全透明）；alpha 仅接受
    2 位 hex（大小写任意），非法时回退 ASS_HEX_ALPHA_OPAQUE；颜色非法时
    使用 default。返回值的 alpha 与 RGB 均为大写。
    """
    color = normalize_hex_color(value, default)
    red = color[1:3]
    green = color[3:5]
    blue = color[5:7]

    alpha_text = ''
    if isinstance(alpha, str):
        alpha_text = alpha.strip().upper()
    valid_alpha = len(alpha_text) == 2 and all(char in _HEX_DIGITS for char in alpha_text)
    if not valid_alpha:
        alpha_text = ASS_HEX_ALPHA_OPAQUE

    return f'&H{alpha_text}{blue}{green}{red}'


_CANONICAL_OVERRIDE_KEYS = (
    'font_size_scale',
    'margin_v_scale',
    'font_color',
    'outline_color',
    'outline_enabled',
    'outline_scale',
    'shadow_enabled',
    'shadow_scale',
    'text_bold',
    'background_enabled',
    'background_color',
    'background_opacity',
)


def _collect_overrides(overrides: Any) -> Dict[str, Any]:
    """统一解析入口：同时接受原始 SUBTITLE_* 配置与 parse_style_overrides 的结果。

    规范键与原始键混用时以规范键为准，解析规则与 parse_style_overrides 一致，
    使 parse 结果成为不动点。
    """
    source = overrides if isinstance(overrides, dict) else {}
    if not any(key in source for key in _CANONICAL_OVERRIDE_KEYS):
        return parse_style_overrides(source)

    merged = parse_style_overrides({})
    merged['font_size_scale'] = _coerce_float(
        source.get('font_size_scale'), 1.0, *FONT_SIZE_SCALE_RANGE
    )
    merged['margin_v_scale'] = _coerce_float(
        source.get('margin_v_scale'), 1.0, *MARGIN_V_SCALE_RANGE
    )
    merged['font_color'] = normalize_hex_color(source.get('font_color'), DEFAULT_FONT_COLOR)
    merged['outline_color'] = normalize_hex_color(
        source.get('outline_color'), DEFAULT_OUTLINE_COLOR
    )
    merged['outline_enabled'] = _coerce_bool(source.get('outline_enabled'), True)
    merged['outline_scale'] = _coerce_float(
        source.get('outline_scale'), 1.0, *OUTLINE_SCALE_RANGE
    )
    merged['shadow_enabled'] = _coerce_bool(source.get('shadow_enabled'), True)
    merged['shadow_scale'] = _coerce_float(
        source.get('shadow_scale'), 1.0, *SHADOW_SCALE_RANGE
    )
    merged['text_bold'] = _coerce_bool(source.get('text_bold'), True)
    merged['background_enabled'] = _coerce_bool(source.get('background_enabled'), False)
    merged['background_color'] = normalize_hex_color(
        source.get('background_color'), DEFAULT_BACKGROUND_COLOR
    )
    merged['background_opacity'] = _coerce_float(
        source.get('background_opacity'), 0.5, *BACKGROUND_OPACITY_RANGE
    )
    return merged


def parse_style_overrides(config: Any) -> Dict[str, Any]:
    """从配置 dict 解析字幕外观覆盖项，返回规范化字典。

    config 为 None 或非 dict 时按空配置处理；缺省/非法一律回退默认值，
    绝不抛异常。颜色返回归一化后的 '#RRGGBB'，数值返回 float，
    布尔返回真正的 bool。
    """
    source = config if isinstance(config, dict) else {}

    return {
        'font_size_scale': _coerce_float(
            source.get('SUBTITLE_FONT_SIZE_SCALE'), 1.0, *FONT_SIZE_SCALE_RANGE
        ),
        'margin_v_scale': _coerce_float(
            source.get('SUBTITLE_MARGIN_V_SCALE'), 1.0, *MARGIN_V_SCALE_RANGE
        ),
        'font_color': normalize_hex_color(source.get('SUBTITLE_FONT_COLOR'), DEFAULT_FONT_COLOR),
        'outline_color': normalize_hex_color(
            source.get('SUBTITLE_OUTLINE_COLOR'), DEFAULT_OUTLINE_COLOR
        ),
        'outline_enabled': _coerce_bool(source.get('SUBTITLE_OUTLINE_ENABLED'), True),
        'outline_scale': _coerce_float(
            source.get('SUBTITLE_OUTLINE_SCALE'), 1.0, *OUTLINE_SCALE_RANGE
        ),
        'shadow_enabled': _coerce_bool(source.get('SUBTITLE_SHADOW_ENABLED'), True),
        'shadow_scale': _coerce_float(
            source.get('SUBTITLE_SHADOW_SCALE'), 1.0, *SHADOW_SCALE_RANGE
        ),
        'text_bold': _coerce_bool(source.get('SUBTITLE_TEXT_BOLD'), True),
        'background_enabled': _coerce_bool(source.get('SUBTITLE_BACKGROUND_ENABLED'), False),
        'background_color': normalize_hex_color(
            source.get('SUBTITLE_BACKGROUND_COLOR'), DEFAULT_BACKGROUND_COLOR
        ),
        'background_opacity': _coerce_float(
            source.get('SUBTITLE_BACKGROUND_OPACITY'), 0.5, *BACKGROUND_OPACITY_RANGE
        ),
    }


def apply_style_overrides(style: Any, overrides: Any) -> Dict[str, Any]:
    """把覆盖项应用到基础样式 dict，返回新的 dict（不修改入参）。

    style 不是 dict 时返回 {}；overrides 为 None 或非 dict 时返回 style 的
    浅拷贝（overrides 既可以是 parse_style_overrides 的返回值，也可以是原始
    SUBTITLE_* 配置 dict）。默认覆盖项下输出与输入语义完全一致
    （字号/描边/阴影/边距不变，
    PrimaryColour='&H00FFFFFF'、OutlineColour='&HB2000000'、Bold=1、
    BorderStyle=1、BackColour 保持原值）。
    """
    if not isinstance(style, dict):
        return {}
    result = dict(style)
    if not isinstance(overrides, dict):
        return result

    # 同时接受原始 SUBTITLE_* 配置与 parse_style_overrides 的结果
    values = _collect_overrides(overrides)

    font_size_scale = values['font_size_scale']
    margin_v_scale = values['margin_v_scale']
    font_color = values['font_color']
    outline_color = values['outline_color']
    outline_enabled = values['outline_enabled']
    outline_scale = values['outline_scale']
    shadow_enabled = values['shadow_enabled']
    shadow_scale = values['shadow_scale']
    text_bold = values['text_bold']
    background_enabled = values['background_enabled']
    background_color = values['background_color']
    background_opacity = values['background_opacity']

    base_font_size = _optional_float(style.get('FontSize'))
    if base_font_size is not None:
        result['FontSize'] = round(base_font_size * font_size_scale, 2)

    # MarginV 取整；MarginL/MarginR 维持原值不动
    base_margin_v = _optional_float(style.get('MarginV'))
    if base_margin_v is not None:
        result['MarginV'] = int(round(base_margin_v * margin_v_scale))

    base_outline = _optional_float(style.get('Outline'))
    if base_outline is not None:
        result['Outline'] = 0.0 if not outline_enabled else base_outline * outline_scale

    base_shadow = _optional_float(style.get('Shadow'))
    if base_shadow is not None:
        result['Shadow'] = 0.0 if not shadow_enabled else base_shadow * shadow_scale

    result['PrimaryColour'] = hex_to_ass_color(font_color)
    if outline_enabled:
        # 描边保留固定半透明 alpha，仅替换色相
        result['OutlineColour'] = hex_to_ass_color(outline_color, alpha=ASS_OUTLINE_ALPHA)
    result['Bold'] = 1 if text_bold else 0

    if background_enabled:
        # BorderStyle=4（libass 圆角底框）时只改这两项，不额外放大描边
        result['BorderStyle'] = 4
        result['BackColour'] = hex_to_ass_color(
            background_color, alpha=_opacity_to_ass_alpha(background_opacity)
        )

    return result


def build_force_style(style: Any, override_keys: Optional[Iterable[str]] = None) -> str:
    """把样式 dict 序列化为 ffmpeg 的 force_style 字面量。

    键顺序固定为 FORCE_STYLE_KEY_ORDER，只输出 style 中实际存在的键；
    override_keys 为 None 时输出全部存在键，否则只输出相交部分（顺序不变）。
    FontSize/Outline/Shadow 按 ASS 数字规则格式化，Margin* 取整，值内单引号
    转义为 \\'，整体用单引号包裹。style 不是 dict、或没有任何可输出键时返回 ''。
    """
    if not isinstance(style, dict):
        return ''

    allowed = None
    if override_keys is not None and not isinstance(override_keys, str):
        try:
            allowed = set(override_keys)
        except Exception:
            allowed = None
    elif isinstance(override_keys, str):
        allowed = {override_keys}

    entries: List[str] = []
    for key in FORCE_STYLE_KEY_ORDER:
        if key not in style:
            continue
        if allowed is not None and key not in allowed:
            continue
        value = style[key]
        if key in _FLOAT_STYLE_KEYS:
            text = _format_number(value)
        elif key in _INT_STYLE_KEYS:
            number = _optional_float(value)
            text = str(int(round(number))) if number is not None else str(value)
        else:
            text = str(value)
        entries.append(f'{key}={text}')

    # 无任何可输出键时不产生空的 force_style 参数
    if not entries:
        return ''

    payload = ','.join(entries).replace("'", r"\'")
    return f"force_style='{payload}'"


def _diagnose_range(
    notes: List[str],
    overrides: Dict[str, Any],
    raw_key: str,
    label: str,
    value_range: tuple,
    effective: float,
) -> None:
    """记录数值倍率的夹紧/边界提示。"""
    minimum, maximum = value_range
    raw_value = overrides.get(raw_key)
    raw_number = _optional_float(raw_value)
    if raw_number is not None and (raw_number < minimum or raw_number > maximum):
        notes.append(
            f'{label}{_format_number(raw_number)} 超出允许范围 '
            f'{_format_number(minimum)}~{_format_number(maximum)}，已夹紧为 {_format_number(effective)}。'
        )
        return
    if effective >= maximum:
        notes.append(f'{label}已达到上限 {_format_number(maximum)}，字号/边距可能超出安全区。')
    elif effective <= minimum:
        notes.append(f'{label}已达到下限 {_format_number(minimum)}，文字可能过小或贴边。')


def diagnose_style(overrides: Any) -> List[str]:
    """返回人类可读的问题列表，用于任务日志/设置页提示。

    入参可以是 parse_style_overrides 的返回值，也可以是原始 SUBTITLE_* 配置
    dict（后者能做更准确的夹紧与非法颜色回退判定）。无问题时返回 []。
    """
    if not isinstance(overrides, dict):
        return []

    parsed = _collect_overrides(overrides)
    notes: List[str] = []

    _diagnose_range(
        notes, overrides, 'SUBTITLE_FONT_SIZE_SCALE', '字号倍率',
        FONT_SIZE_SCALE_RANGE, parsed['font_size_scale'],
    )
    _diagnose_range(
        notes, overrides, 'SUBTITLE_MARGIN_V_SCALE', '底部边距倍率',
        MARGIN_V_SCALE_RANGE, parsed['margin_v_scale'],
    )

    # 颜色非法回退提示（仅当传入原始配置键时才可能判定）
    for raw_key, label, _default in _COLOR_CONFIG_KEYS:
        raw_value = overrides.get(raw_key)
        if not isinstance(raw_value, str) or not raw_value.strip():
            continue
        if _normalize_color_candidate(raw_value) is not None:
            continue
        digits = raw_value.strip().lstrip('#')
        if len(digits) == 8 and all(char in _HEX_DIGITS for char in digits.upper()):
            notes.append(f'{label}“{raw_value}”是 8 位 AARRGGBB 格式，不被支持，已回退为 6 位默认色。')
        else:
            notes.append(f'{label}“{raw_value}”不是合法的 #RRGGBB 颜色，已回退默认色。')

    if parsed['outline_enabled'] and parsed['outline_scale'] <= 0.0:
        notes.append('描边已开启但描边倍率为 0，画面上看不到描边。')
    if parsed['shadow_enabled'] and parsed['shadow_scale'] <= 0.0:
        notes.append('阴影已开启但阴影倍率为 0，画面上看不到阴影。')

    if parsed['background_enabled']:
        if parsed['shadow_enabled'] and parsed['shadow_scale'] > 0.0:
            notes.append(
                '底框（BorderStyle=4）开启时 libass 会把阴影当作底框外扩，'
                '建议关闭阴影或把阴影倍率设为 0。'
            )
        if parsed['background_opacity'] >= 0.85:
            notes.append('底框不透明度较高，可能遮挡画面内容，建议降到 0.7 以下。')

    return notes
