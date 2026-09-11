"""字幕外观纯函数模块（modules/subtitle_style.py）回归测试。

覆盖颜色归一化、hex->ASS 的 BGR 反相换算、alpha 处理、覆盖项宽容解析、
apply_style_overrides 的默认等价性（防回归核心）、底框换算、
force_style 序列化与 diagnose_style 诊断。
"""

import unittest

from modules.subtitle_style import (
    ASS_HEX_ALPHA_OPAQUE,
    apply_style_overrides,
    build_force_style,
    diagnose_style,
    hex_to_ass_color,
    normalize_hex_color,
    parse_style_overrides,
)

# 与 task_manager._build_streaming_ass_style 相同形状的基础样式（1080p 横屏实际取值）
BASE_STYLE = {
    'FontSize': 56.0,
    'Outline': 2.0,
    'Shadow': 1.0,
    'MarginV': 40.0,
    'MarginL': 96.0,
    'MarginR': 96.0,
    'Alignment': 2,
    'BorderStyle': 1,
    'Bold': 1,
    'PrimaryColour': '&H00FFFFFF',
    'SecondaryColour': '&H00FFFFFF',
    'OutlineColour': '&HB2000000',
    'BackColour': '&H00000000',
    'PlayResX': 1920,
    'PlayResY': 1080,
}


class NormalizeHexColorTests(unittest.TestCase):
    def test_six_digit_with_hash_is_uppercased(self):
        self.assertEqual(normalize_hex_color('#abcdef'), '#ABCDEF')

    def test_six_digit_without_hash_is_accepted(self):
        self.assertEqual(normalize_hex_color('123456'), '#123456')

    def test_short_form_is_expanded(self):
        self.assertEqual(normalize_hex_color('#abc'), '#AABBCC')
        self.assertEqual(normalize_hex_color('123'), '#112233')
        self.assertEqual(normalize_hex_color('F0a'), '#FF00AA')

    def test_surrounding_whitespace_is_trimmed(self):
        self.assertEqual(normalize_hex_color('  #AbCdEf  '), '#ABCDEF')

    def test_eight_digit_input_is_rejected(self):
        # 8 位 AARRGGBB 一律视为非法，回退默认值
        self.assertEqual(normalize_hex_color('#80123456'), '#FFFFFF')
        self.assertEqual(normalize_hex_color('FF123456', '#000000'), '#000000')

    def test_invalid_inputs_fall_back_to_default(self):
        for value in ('#GGGGGG', 'xyz', '#12345', '#1234567', '', '   ', '#', None, 123, 1.5, [], {}, object()):
            with self.subTest(value=value):
                self.assertEqual(normalize_hex_color(value), '#FFFFFF')

    def test_invalid_default_falls_back_to_white(self):
        self.assertEqual(normalize_hex_color('nope', 'also-nope'), '#FFFFFF')
        self.assertEqual(normalize_hex_color(None, None), '#FFFFFF')

    def test_default_is_normalized_too(self):
        self.assertEqual(normalize_hex_color('nope', 'abc'), '#AABBCC')


class HexToAssColorTests(unittest.TestCase):
    def test_bgr_order_with_opaque_alpha(self):
        # ASS 是 BGR 排列：&HAABBGGRR，alpha=00 表示完全不透明
        self.assertEqual(hex_to_ass_color('#123456'), '&H00563412')
        self.assertEqual(hex_to_ass_color('#FF0000'), '&H000000FF')
        self.assertEqual(hex_to_ass_color('#0000FF'), '&H00FF0000')
        self.assertEqual(hex_to_ass_color('#00FF00'), '&H0000FF00')
        self.assertEqual(hex_to_ass_color('#FFFFFF'), '&H00FFFFFF')
        self.assertEqual(hex_to_ass_color('#000000'), '&H00000000')

    def test_alpha_is_uppercased_and_prefixed(self):
        self.assertEqual(hex_to_ass_color('#000000', alpha='b2'), '&HB2000000')
        self.assertEqual(hex_to_ass_color('#FFFFFF', alpha='ff'), '&HFFFFFFFF')
        self.assertEqual(hex_to_ass_color('#000000', alpha='00'), '&H00000000')

    def test_invalid_alpha_falls_back_to_opaque(self):
        for alpha in ('zz', '0', '000', '', None, 12, '#00', 'g0'):
            with self.subTest(alpha=alpha):
                self.assertEqual(hex_to_ass_color('#FFFFFF', alpha=alpha), '&H00FFFFFF')

    def test_invalid_color_uses_default(self):
        self.assertEqual(hex_to_ass_color('#GGGGGG', '#123456'), '&H00563412')
        self.assertEqual(hex_to_ass_color(None, 'nope'), '&H00FFFFFF')

    def test_opaque_alpha_constant(self):
        self.assertEqual(ASS_HEX_ALPHA_OPAQUE, '00')


class ParseStyleOverridesTests(unittest.TestCase):
    def test_none_and_non_dict_config_use_defaults(self):
        for config in (None, [], 'x', 3, object()):
            with self.subTest(config=config):
                self.assertEqual(parse_style_overrides(config), parse_style_overrides({}))

    def test_default_values(self):
        overrides = parse_style_overrides({})
        self.assertEqual(overrides, {
            'font_size_scale': 1.0,
            'margin_v_scale': 1.0,
            'font_color': '#FFFFFF',
            'outline_color': '#000000',
            'outline_enabled': True,
            'outline_scale': 1.0,
            'shadow_enabled': True,
            'shadow_scale': 1.0,
            'text_bold': True,
            'background_enabled': False,
            'background_color': '#000000',
            'background_opacity': 0.5,
        })
        self.assertIsInstance(overrides['font_size_scale'], float)
        self.assertIsInstance(overrides['outline_enabled'], bool)

    def test_explicit_values_are_parsed(self):
        overrides = parse_style_overrides({
            'SUBTITLE_FONT_SIZE_SCALE': 1.25,
            'SUBTITLE_MARGIN_V_SCALE': 0.8,
            'SUBTITLE_FONT_COLOR': '#abc',
            'SUBTITLE_OUTLINE_COLOR': 'FF0000',
            'SUBTITLE_OUTLINE_ENABLED': 'off',
            'SUBTITLE_OUTLINE_SCALE': 2.5,
            'SUBTITLE_SHADOW_ENABLED': 'no',
            'SUBTITLE_SHADOW_SCALE': 0.0,
            'SUBTITLE_TEXT_BOLD': False,
            'SUBTITLE_BACKGROUND_ENABLED': True,
            'SUBTITLE_BACKGROUND_COLOR': '#123456',
            'SUBTITLE_BACKGROUND_OPACITY': 0.25,
        })
        self.assertEqual(overrides['font_size_scale'], 1.25)
        self.assertEqual(overrides['margin_v_scale'], 0.8)
        self.assertEqual(overrides['font_color'], '#AABBCC')
        self.assertEqual(overrides['outline_color'], '#FF0000')
        self.assertIs(overrides['outline_enabled'], False)
        self.assertEqual(overrides['outline_scale'], 2.5)
        self.assertIs(overrides['shadow_enabled'], False)
        self.assertEqual(overrides['shadow_scale'], 0.0)
        self.assertIs(overrides['text_bold'], False)
        self.assertIs(overrides['background_enabled'], True)
        self.assertEqual(overrides['background_color'], '#123456')
        self.assertEqual(overrides['background_opacity'], 0.25)

    def test_boolean_tolerance(self):
        false_tokens = ('false', 'FALSE', 'False', '0', 'no', 'NO', 'off', 'OFF', 0, 0.0)
        true_tokens = ('true', 'TRUE', '1', 'yes', 'YES', 'on', 'ON', 1, 1.0)
        for token in false_tokens:
            with self.subTest(token=token):
                self.assertIs(parse_style_overrides({'SUBTITLE_OUTLINE_ENABLED': token})['outline_enabled'], False)
        for token in true_tokens:
            with self.subTest(token=token):
                self.assertIs(parse_style_overrides({'SUBTITLE_SHADOW_ENABLED': token})['shadow_enabled'], True)

    def test_unrecognized_boolean_falls_back_to_default(self):
        # 字符串 'false' 绝不能因 bool() 强转变成 True，无法识别时保持默认 True
        self.assertIs(parse_style_overrides({'SUBTITLE_TEXT_BOLD': 'maybe'})['text_bold'], True)
        self.assertIs(parse_style_overrides({'SUBTITLE_TEXT_BOLD': None})['text_bold'], True)
        self.assertIs(parse_style_overrides({'SUBTITLE_TEXT_BOLD': 7})['text_bold'], True)
        self.assertIs(parse_style_overrides({'SUBTITLE_BACKGROUND_ENABLED': 'maybe'})['background_enabled'], False)

    def test_numeric_clamping(self):
        self.assertEqual(parse_style_overrides({'SUBTITLE_FONT_SIZE_SCALE': 9.0})['font_size_scale'], 2.0)
        self.assertEqual(parse_style_overrides({'SUBTITLE_FONT_SIZE_SCALE': 0.01})['font_size_scale'], 0.5)
        self.assertEqual(parse_style_overrides({'SUBTITLE_MARGIN_V_SCALE': 99})['margin_v_scale'], 2.0)
        self.assertEqual(parse_style_overrides({'SUBTITLE_MARGIN_V_SCALE': 0.0})['margin_v_scale'], 0.5)
        self.assertEqual(parse_style_overrides({'SUBTITLE_OUTLINE_SCALE': 10.0})['outline_scale'], 3.0)
        self.assertEqual(parse_style_overrides({'SUBTITLE_OUTLINE_SCALE': -2.0})['outline_scale'], 0.0)
        self.assertEqual(parse_style_overrides({'SUBTITLE_SHADOW_SCALE': -1})['shadow_scale'], 0.0)
        self.assertEqual(parse_style_overrides({'SUBTITLE_BACKGROUND_OPACITY': 2.0})['background_opacity'], 1.0)
        self.assertEqual(parse_style_overrides({'SUBTITLE_BACKGROUND_OPACITY': -0.3})['background_opacity'], 0.0)

    def test_non_numeric_values_fall_back_to_default(self):
        for value in ('abc', '', None, [], {}, True):
            with self.subTest(value=value):
                self.assertEqual(parse_style_overrides({'SUBTITLE_FONT_SIZE_SCALE': value})['font_size_scale'], 1.0)
        self.assertEqual(parse_style_overrides({'SUBTITLE_BACKGROUND_OPACITY': 'nan'})['background_opacity'], 0.5)

    def test_numeric_strings_are_accepted(self):
        self.assertEqual(parse_style_overrides({'SUBTITLE_FONT_SIZE_SCALE': '1.5'})['font_size_scale'], 1.5)

    def test_invalid_colors_fall_back_to_defaults(self):
        overrides = parse_style_overrides({
            'SUBTITLE_FONT_COLOR': '#ZZZZZZ',
            'SUBTITLE_OUTLINE_COLOR': '#80FFFFFF',
            'SUBTITLE_BACKGROUND_COLOR': None,
        })
        self.assertEqual(overrides['font_color'], '#FFFFFF')
        self.assertEqual(overrides['outline_color'], '#000000')
        self.assertEqual(overrides['background_color'], '#000000')


class ApplyStyleOverridesDefaultEquivalenceTests(unittest.TestCase):
    """默认配置下必须与现状完全等价（防回归核心）。"""

    def test_default_overrides_keep_base_style_semantics(self):
        applied = apply_style_overrides(BASE_STYLE, parse_style_overrides({}))

        self.assertEqual(applied['FontSize'], 56.0)
        self.assertEqual(applied['Outline'], 2.0)
        self.assertEqual(applied['Shadow'], 1.0)
        self.assertEqual(applied['MarginV'], 40)
        self.assertEqual(applied['MarginL'], 96.0)
        self.assertEqual(applied['MarginR'], 96.0)
        self.assertEqual(applied['PrimaryColour'], '&H00FFFFFF')
        self.assertEqual(applied['OutlineColour'], '&HB2000000')
        self.assertEqual(applied['SecondaryColour'], '&H00FFFFFF')
        self.assertEqual(applied['BackColour'], '&H00000000')
        self.assertEqual(applied['Bold'], 1)
        self.assertEqual(applied['BorderStyle'], 1)
        self.assertEqual(applied['Alignment'], 2)
        self.assertEqual(applied['PlayResX'], 1920)
        self.assertEqual(applied['PlayResY'], 1080)
        self.assertEqual(applied, dict(BASE_STYLE))


class ApplyStyleOverridesTests(unittest.TestCase):
    def test_non_dict_style_returns_empty_dict(self):
        for style in (None, [], 'x', 5):
            with self.subTest(style=style):
                self.assertEqual(apply_style_overrides(style, parse_style_overrides({})), {})

    def test_none_overrides_returns_shallow_copy(self):
        copied = apply_style_overrides(BASE_STYLE, None)
        self.assertEqual(copied, BASE_STYLE)
        self.assertIsNot(copied, BASE_STYLE)
        copied['FontSize'] = 1.0
        self.assertEqual(BASE_STYLE['FontSize'], 56.0)

    def test_non_dict_overrides_returns_shallow_copy(self):
        self.assertEqual(apply_style_overrides(BASE_STYLE, 'nope'), BASE_STYLE)

    def test_input_style_is_not_mutated(self):
        snapshot = dict(BASE_STYLE)
        apply_style_overrides(BASE_STYLE, parse_style_overrides({
            'SUBTITLE_FONT_SIZE_SCALE': 0.5,
            'SUBTITLE_BACKGROUND_ENABLED': True,
            'SUBTITLE_FONT_COLOR': '#FF0000',
        }))
        self.assertEqual(BASE_STYLE, snapshot)

    def test_font_size_scale_is_rounded_to_two_decimals(self):
        applied = apply_style_overrides(BASE_STYLE, parse_style_overrides({'SUBTITLE_FONT_SIZE_SCALE': 0.5}))
        self.assertEqual(applied['FontSize'], 28.0)
        applied = apply_style_overrides(BASE_STYLE, parse_style_overrides({'SUBTITLE_FONT_SIZE_SCALE': 0.97}))
        self.assertEqual(applied['FontSize'], 54.32)

    def test_margin_v_scale_is_int_and_side_margins_untouched(self):
        applied = apply_style_overrides(BASE_STYLE, parse_style_overrides({'SUBTITLE_MARGIN_V_SCALE': 1.55}))
        self.assertEqual(applied['MarginV'], 62)
        self.assertIsInstance(applied['MarginV'], int)
        self.assertEqual(applied['MarginL'], 96.0)
        self.assertEqual(applied['MarginR'], 96.0)

    def test_outline_scale_and_disable(self):
        scaled = apply_style_overrides(BASE_STYLE, parse_style_overrides({'SUBTITLE_OUTLINE_SCALE': 2.5}))
        self.assertEqual(scaled['Outline'], 5.0)
        disabled = apply_style_overrides(BASE_STYLE, parse_style_overrides({'SUBTITLE_OUTLINE_ENABLED': 'false'}))
        self.assertEqual(disabled['Outline'], 0.0)
        # 关闭描边时 OutlineColour 保持原值不动
        self.assertEqual(disabled['OutlineColour'], '&HB2000000')

    def test_shadow_scale_and_disable(self):
        scaled = apply_style_overrides(BASE_STYLE, parse_style_overrides({'SUBTITLE_SHADOW_SCALE': 2.0}))
        self.assertEqual(scaled['Shadow'], 2.0)
        disabled = apply_style_overrides(BASE_STYLE, parse_style_overrides({'SUBTITLE_SHADOW_ENABLED': False}))
        self.assertEqual(disabled['Shadow'], 0.0)

    def test_font_color_is_applied(self):
        applied = apply_style_overrides(BASE_STYLE, parse_style_overrides({'SUBTITLE_FONT_COLOR': '#FF0000'}))
        self.assertEqual(applied['PrimaryColour'], '&H000000FF')

    def test_outline_color_keeps_fixed_half_transparent_alpha(self):
        applied = apply_style_overrides(BASE_STYLE, parse_style_overrides({'SUBTITLE_OUTLINE_COLOR': '#FFFFFF'}))
        self.assertEqual(applied['OutlineColour'], '&HB2FFFFFF')

    def test_bold_toggle(self):
        self.assertEqual(
            apply_style_overrides(BASE_STYLE, parse_style_overrides({'SUBTITLE_TEXT_BOLD': 'false'}))['Bold'],
            0,
        )
        self.assertEqual(
            apply_style_overrides(BASE_STYLE, parse_style_overrides({'SUBTITLE_TEXT_BOLD': True}))['Bold'],
            1,
        )

    def test_background_disabled_keeps_original_border_style_and_backcolour(self):
        applied = apply_style_overrides(BASE_STYLE, parse_style_overrides({'SUBTITLE_BACKGROUND_ENABLED': False}))
        self.assertEqual(applied['BorderStyle'], 1)
        self.assertEqual(applied['BackColour'], '&H00000000')

    def test_background_enabled_switches_border_style_and_backcolour(self):
        applied = apply_style_overrides(BASE_STYLE, parse_style_overrides({
            'SUBTITLE_BACKGROUND_ENABLED': True,
            'SUBTITLE_BACKGROUND_COLOR': '#123456',
            'SUBTITLE_BACKGROUND_OPACITY': 0.5,
        }))
        self.assertEqual(applied['BorderStyle'], 4)
        # #123456 -> BGR 563412，opacity 0.5 -> alpha 0x80
        self.assertEqual(applied['BackColour'], '&H80563412')
        # BorderStyle=4 时不额外放大描边
        self.assertEqual(applied['Outline'], 2.0)

    def test_background_opacity_alpha_range(self):
        full = apply_style_overrides(BASE_STYLE, parse_style_overrides({
            'SUBTITLE_BACKGROUND_ENABLED': True,
            'SUBTITLE_BACKGROUND_OPACITY': 1.0,
        }))
        invisible = apply_style_overrides(BASE_STYLE, parse_style_overrides({
            'SUBTITLE_BACKGROUND_ENABLED': True,
            'SUBTITLE_BACKGROUND_OPACITY': 0.0,
        }))
        self.assertEqual(full['BackColour'], '&H00000000')
        self.assertEqual(invisible['BackColour'], '&HFF000000')

    def test_style_without_optional_keys_does_not_crash(self):
        applied = apply_style_overrides({'FontSize': 54.0}, parse_style_overrides({}))
        self.assertEqual(applied['FontSize'], 54.0)
        self.assertEqual(applied['PrimaryColour'], '&H00FFFFFF')
        self.assertEqual(applied['Bold'], 1)
        self.assertNotIn('MarginV', applied)
        self.assertNotIn('BackColour', applied)

    def test_apply_also_accepts_raw_config_keys(self):
        applied = apply_style_overrides(BASE_STYLE, {
            'SUBTITLE_FONT_SIZE_SCALE': 0.5,
            'SUBTITLE_FONT_COLOR': '#FF0000',
            'SUBTITLE_SHADOW_ENABLED': 'false',
        })
        self.assertEqual(applied['FontSize'], 28.0)
        self.assertEqual(applied['PrimaryColour'], '&H000000FF')
        self.assertEqual(applied['Shadow'], 0.0)

    def test_canonical_keys_match_raw_config_semantics(self):
        # apply 同时接受原始 SUBTITLE_* 配置与 parse_style_overrides 的结果
        raw = {'SUBTITLE_FONT_SIZE_SCALE': 1.25, 'SUBTITLE_BACKGROUND_ENABLED': True}
        canonical = parse_style_overrides(raw)
        self.assertEqual(
            apply_style_overrides(BASE_STYLE, raw),
            apply_style_overrides(BASE_STYLE, canonical),
        )
        self.assertEqual(
            diagnose_style(raw),
            diagnose_style(canonical),
        )


class BuildForceStyleTests(unittest.TestCase):
    def setUp(self):
        self.style = {
            'FontName': 'Noto Sans CJK SC',
            'FontSize': 54.0,
            'Outline': 4.05,
            'Shadow': 1.35,
            'MarginL': 48.0,
            'MarginR': 48.0,
            'MarginV': 62.0,
            'Alignment': 2,
            'BorderStyle': 1,
            'PrimaryColour': '&H00FFFFFF',
            'OutlineColour': '&HB2000000',
            'BackColour': '&H00000000',
        }

    def test_key_order_is_fixed(self):
        payload = build_force_style(self.style)
        expected = (
            "force_style='FontName=Noto Sans CJK SC,FontSize=54,Outline=4.05,Shadow=1.35,"
            "MarginL=48,MarginR=48,MarginV=62,Alignment=2,BorderStyle=1,"
            "PrimaryColour=&H00FFFFFF,OutlineColour=&HB2000000,BackColour=&H00000000'"
        )
        self.assertEqual(payload, expected)

    def test_shuffled_input_dict_still_uses_fixed_order(self):
        shuffled = {key: self.style[key] for key in reversed(list(self.style))}
        self.assertEqual(build_force_style(shuffled), build_force_style(self.style))

    def test_missing_keys_are_skipped(self):
        payload = build_force_style({'FontSize': 54.0, 'Alignment': 2})
        self.assertEqual(payload, "force_style='FontSize=54,Alignment=2'")

    def test_override_keys_filter_keeps_fixed_order(self):
        payload = build_force_style(self.style, ['Alignment', 'FontName', 'NotPresent'])
        self.assertEqual(payload, "force_style='FontName=Noto Sans CJK SC,Alignment=2'")
        self.assertEqual(build_force_style(self.style, ['Unknown']), '')

    def test_number_formatting(self):
        payload = build_force_style({'FontSize': 54.0, 'Outline': 2.5, 'Shadow': 1.0})
        self.assertEqual(payload, "force_style='FontSize=54,Outline=2.5,Shadow=1'")
        payload = build_force_style({'FontSize': '62.00', 'Outline': 4.10})
        self.assertEqual(payload, "force_style='FontSize=62,Outline=4.1'")

    def test_margins_are_rounded_to_int(self):
        payload = build_force_style({'MarginL': 96.4, 'MarginR': 96.6, 'MarginV': 62.5})
        self.assertEqual(payload, "force_style='MarginL=96,MarginR=97,MarginV=62'")

    def test_single_quote_is_escaped(self):
        payload = build_force_style({'FontName': "Bob's Font", 'FontSize': 56.0})
        self.assertEqual(payload, "force_style='FontName=Bob\\'s Font,FontSize=56'")

    def test_non_dict_returns_empty_string(self):
        for style in (None, [], 'x', 3):
            with self.subTest(style=style):
                self.assertEqual(build_force_style(style), '')

    def test_empty_style_returns_empty_string(self):
        self.assertEqual(build_force_style({}), '')
        self.assertEqual(build_force_style({'Unknown': 1}), '')

    def test_applied_style_round_trip(self):
        applied = apply_style_overrides(BASE_STYLE, parse_style_overrides({'SUBTITLE_FONT_SIZE_SCALE': 0.5}))
        applied['FontName'] = 'Noto Sans CJK SC'
        payload = build_force_style(applied, ['FontName', 'FontSize', 'Outline', 'Shadow'])
        self.assertEqual(
            payload,
            "force_style='FontName=Noto Sans CJK SC,FontSize=28,Outline=2,Shadow=1'",
        )


class DiagnoseStyleTests(unittest.TestCase):
    def test_default_has_no_notes(self):
        self.assertEqual(diagnose_style(parse_style_overrides({})), [])
        self.assertEqual(diagnose_style({}), [])

    def test_non_dict_returns_empty_list(self):
        for value in (None, [], 'x', 5):
            with self.subTest(value=value):
                self.assertEqual(diagnose_style(value), [])

    def test_clamped_scale_is_reported(self):
        notes = diagnose_style({'SUBTITLE_FONT_SIZE_SCALE': 5})
        self.assertEqual(len(notes), 1)
        self.assertIn('字号倍率', notes[0])
        self.assertIn('夹紧', notes[0])
        self.assertIn('2', notes[0])

    def test_background_with_shadow_is_reported(self):
        notes = diagnose_style({
            'SUBTITLE_BACKGROUND_ENABLED': True,
            'SUBTITLE_SHADOW_ENABLED': 'true',
            'SUBTITLE_SHADOW_SCALE': 1.0,
        })
        self.assertTrue(any('BorderStyle=4' in note for note in notes))

    def test_invalid_color_fallback_is_reported(self):
        notes = diagnose_style({'SUBTITLE_FONT_COLOR': '#GGGGGG'})
        self.assertEqual(len(notes), 1)
        self.assertIn('#GGGGGG', notes[0])
        self.assertIn('回退', notes[0])

    def test_eight_digit_color_is_reported_separately(self):
        notes = diagnose_style({'SUBTITLE_OUTLINE_COLOR': '#80FFFFFF'})
        self.assertEqual(len(notes), 1)
        self.assertIn('8 位', notes[0])

    def test_zero_scale_with_enabled_flag_is_reported(self):
        notes = diagnose_style({
            'SUBTITLE_OUTLINE_ENABLED': True,
            'SUBTITLE_OUTLINE_SCALE': 0.0,
        })
        self.assertTrue(any('描边' in note and '看不到' in note for note in notes))

    def test_high_background_opacity_is_reported(self):
        notes = diagnose_style({
            'SUBTITLE_BACKGROUND_ENABLED': True,
            'SUBTITLE_BACKGROUND_OPACITY': 0.95,
        })
        self.assertTrue(any('不透明度' in note for note in notes))

    def test_parsed_overrides_can_be_diagnosed(self):
        self.assertEqual(diagnose_style(parse_style_overrides({})), [])
        notes = diagnose_style(parse_style_overrides({'SUBTITLE_BACKGROUND_ENABLED': True}))
        self.assertTrue(any('BorderStyle=4' in note for note in notes))


if __name__ == '__main__':
    unittest.main()
