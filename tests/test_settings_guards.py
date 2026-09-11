"""设置页数值 guard、交叉约束与旧钉死配置迁移的行为锁定测试。

覆盖六类行为：
1. guard:越界 / 非法提交 → 回退到 DEFAULT_CONFIG，且产生用户可见的 warning 消息
   （修复前只有 logger.debug，用户只看到「配置已成功保存」）
2. 交叉约束:AUDIO_CHUNK_OVERLAP_S 必须严格小于 AUDIO_CHUNK_WINDOW_S
   （否则 _create_audio_chunks 的步进 <= 0，分片循环永不终止）
3. 无控件键的防御性 guard:SUBTITLE_QC_* 三键没有 UI 入口，但 /settings 的 POST
   用 request.form.to_dict() 接收任意键名，手工提交仍能写坏配置
4. 旧钉死换行组合（999/1/on/on）在「设置页提交」与「配置迁移」两条路径落盘一致
5. 迁移三态:命中 / 不命中 / 二次执行幂等
6. 反向守卫:每个数值白名单键必须有数值控件，或登记于显式豁免表

这里断言的是「提交值 → 落盘值」与「用户可见消息」这类外部可观测行为，
不复用 app.py 内部的格式化函数，避免断言与实现同构。
"""
import os
import pathlib
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from lxml import html as lxml_html

import app as web_app
from app import (
    SETTINGS_FLOAT_FIELDS,
    SETTINGS_INT_FIELDS,
    SETTINGS_RANGE_GUARDS,
)
from modules import config_manager as cm
from modules.config_manager import DEFAULT_CONFIG
from modules.speech_pipeline_settings import (
    coerce_bool,
    migrate_pinned_subtitle_wrap_config,
)

# 数值白名单里刻意不暴露控件的键（无 UI 入口，只能被手工提交写入）。
# 反向守卫要求「有控件 或 在此登记」，所以新增无控件数值键时必须显式登记，
# 否则 test_每个数值白名单键必须有控件或登记豁免 会失败。
NO_CONTROL_WHITELIST_KEYS = {
    'YOUTUBE_DOWNLOAD_MAX_HEIGHT',
    'VAD_SILERO_MIN_SILENCE_MS',
    'VAD_SILERO_MAX_SPEECH_S',
    'VAD_SILERO_SPEECH_PAD_MS',
    'WHISPER_MAX_WORKERS',
    'SUBTITLE_TRANSLATION_MAX_CHARS_PER_BATCH',
    'AI_SEGMENTATION_CONTEXT_WINDOW',
    'AI_SEGMENTATION_BOUNDARY_WINDOW',
    'WHISPER_RETRY_DELAY_S',
    'VAD_MERGE_GAP_S',
    'VAD_MIN_SEGMENT_S',
    'VAD_MAX_SEGMENT_S_FOR_SPLIT',
    'WHISPER_TEMPERATURE',
    'WHISPER_NO_SPEECH_THRESHOLD',
    'SUBTITLE_MAX_CUE_DURATION_S',
    'SUBTITLE_MAX_CPS',
    'SUBTITLE_QC_MIN_COVERAGE_RATIO',
    'SUBTITLE_QC_MAX_GAP_S',
    'SUBTITLE_QC_MAX_CPS',
}

# 旧模板钉死的字幕换行组合（migrate_pinned_subtitle_wrap_config 的判据）
LEGACY_PINNED_WRAP = {
    'SUBTITLE_MAX_LINE_LENGTH': 999,
    'SUBTITLE_MAX_LINES': 1,
    'SUBTITLE_MAX_LINE_LENGTH_ENABLED': True,
    'SUBTITLE_MAX_LINES_ENABLED': True,
}

# (键, 提交值, 期望落盘值)。期望值一律等于 DEFAULT_CONFIG，区间见 SETTINGS_RANGE_GUARDS。
GUARD_CASES = (
    ('SUBTITLE_MAX_LINE_LENGTH', '999', 42.0),
    ('SUBTITLE_MAX_LINES', '9', 2.0),
    ('VAD_MAX_SEGMENT_S', '5000', 15.0),
    ('VAD_MAX_SEGMENT_S', '3', 15.0),
    ('AUDIO_CHUNK_WINDOW_S', '2', 15.0),
    ('AUDIO_CHUNK_WINDOW_S', '999', 15.0),
    ('AUDIO_CHUNK_OVERLAP_S', '9', 0.4),
    ('VAD_MIN_SEGMENT_S', '99', 0.8),
    ('VAD_MERGE_GAP_S', '-1', 0.35),
    ('VAD_MAX_SEGMENT_S_FOR_SPLIT', '1', 15.0),
    ('VAD_MIN_SPEECH_COVERAGE_RATIO', '2', 0.015),
    ('SUBTITLE_MAX_CUE_DURATION_S', '0', 8.0),
    ('SUBTITLE_MAX_CPS', 'inf', 20.0),
    ('SUBTITLE_MAX_CPS', 'nan', 20.0),
    ('SUBTITLE_TRANSLATION_MAX_CHARS_PER_BATCH', '999999', 2000.0),
    ('WHISPER_TEMPERATURE', '2.5', 0.0),
    ('WHISPER_NO_SPEECH_THRESHOLD', '-1', 0.6),
    ('SUBTITLE_QC_MIN_COVERAGE_RATIO', '3', 0.15),
    ('SUBTITLE_QC_MAX_GAP_S', '-10', 90.0),
    ('SUBTITLE_QC_MAX_CPS', '99999', 25.0),
)

# (键, 提交值, 期望落盘值):非数字提交在归一化阶段被回退，范围校验看不到它，
# 但仍必须产生可见消息，否则就是「值被改掉却提示成功」。
NON_NUMERIC_CASES = (
    ('VAD_MAX_SEGMENT_S', 'abc', 15.0),
    ('SUBTITLE_MAX_LINE_LENGTH', '', 42.0),
    ('WHISPER_TEMPERATURE', '   ', 0.0),
)

# (键, 合法提交值, 期望落盘值):合法值必须原样保留且不产生任何 warning。
VALID_CASES = (
    ('VAD_MAX_SEGMENT_S', '10', 10.0),
    ('VAD_MAX_SEGMENT_S', '120', 120.0),
    ('AUDIO_CHUNK_WINDOW_S', '5', 5.0),
    ('AUDIO_CHUNK_OVERLAP_S', '0', 0.0),
    ('SUBTITLE_MAX_LINE_LENGTH', '42', 42.0),
    ('SUBTITLE_MAX_LINES', '2', 2.0),
    ('VAD_MIN_SPEECH_COVERAGE_RATIO', '0.015', 0.015),
    ('SUBTITLE_QC_MIN_COVERAGE_RATIO', '0.2', 0.2),
    ('SUBTITLE_QC_MAX_GAP_S', '120', 120.0),
    ('SUBTITLE_QC_MAX_CPS', '30', 30.0),
)

# (窗口, 重叠, 期望落盘重叠)。交叉约束:overlap >= window → clamp(window - 1.0, 下限 0)。
CROSS_CASES = (
    ('5', '5', 4.0),       # 相等 → 钳到窗口 − 1 秒
    ('5', '8', 0.4),       # overlap 越界先被范围 guard 回退成默认 0.4
    ('20', '5', 5.0),      # 5 < 20，合法，不改
    ('5', '4.5', 4.5),     # 4.5 < 5，合法，不改
    ('10', '10', 0.4),     # 相等但 overlap=10 越界，先回退成默认 0.4
    ('15', '0.4', 0.4),    # 默认组合，不改
)


class _SettingsSaveTestCase(unittest.TestCase):
    """把配置目录指向临时目录，并屏蔽 _perform_settings_save 的外部副作用。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='y2a-settings-guards-')
        self._patchers = []
        for patcher in (
            patch.object(cm, 'get_app_subdir',
                         lambda name: os.path.join(self._tmp, name)),
            patch.object(web_app, 'configure_app', return_value=None),
            patch.object(web_app, '_sync_notification_service', return_value=None),
            patch('modules.task_manager.get_global_task_processor', return_value=None),
            patch.object(web_app.youtube_monitor, 'reload_api_client',
                         return_value=(False, 'missing_api_key')),
            patch.object(web_app.youtube_monitor, 'start_all_schedules', return_value=None),
            patch.object(web_app.youtube_monitor, 'stop_all_schedules', return_value=None),
        ):
            patcher.start()
            self._patchers.append(patcher)

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _save(self, form):
        result = web_app._perform_settings_save(dict(form), {})
        self.assertTrue(result['success'], msg=result.get('messages'))
        return result

    @staticmethod
    def _warnings(result):
        return [msg['text'] for msg in result['messages'] if msg['category'] == 'warning']

    @staticmethod
    def _numeric(config, key):
        return float(config.get(key))


class SettingsRangeGuardTests(_SettingsSaveTestCase):
    """越界回退必须落盘到期望值，且用户能看到回退说明。"""

    def test_越界提交回退且产生可见消息(self):
        for key, submitted, expected in GUARD_CASES:
            with self.subTest(key=key, submitted=submitted):
                result = self._save({key: submitted})
                self.assertEqual(
                    self._numeric(result['updated_config'], key), expected,
                    msg=f'{key} 未回退到 DEFAULT_CONFIG 值')

                warnings = self._warnings(result)
                self.assertEqual(len(warnings), 1, msg=f'回退未产生唯一一条可见 warning: {warnings}')
                text = warnings[0]
                self.assertIn(key, text, msg=f'提示未点名被回退的键: {text}')
                self.assertIn(str(submitted), text, msg=f'提示未给出提交值: {text}')
                self.assertIn('生效', text, msg=f'提示未给出最终生效值: {text}')
                self.assertIn(
                    '配置已成功保存', [msg['text'] for msg in result['messages']],
                    msg='保存本身的成功提示不应被回退提示替换')
                self.assertEqual(result['final_level'], 'warning')

    def test_非数字提交回退且产生可见消息(self):
        for key, submitted, expected in NON_NUMERIC_CASES:
            with self.subTest(key=key, submitted=submitted):
                result = self._save({key: submitted})
                self.assertEqual(self._numeric(result['updated_config'], key), expected)
                warnings = self._warnings(result)
                self.assertEqual(len(warnings), 1, msg=f'非数字回退未提示: {warnings}')
                self.assertIn(key, warnings[0])

    def test_合法值原样保留且不产生提示(self):
        for key, submitted, expected in VALID_CASES:
            with self.subTest(key=key, submitted=submitted):
                result = self._save({key: submitted})
                self.assertEqual(self._numeric(result['updated_config'], key), expected)
                self.assertEqual(self._warnings(result), [], msg=f'合法值被误报: {key}')
                self.assertEqual(result['final_level'], 'success')

    def test_未提交的守卫键保持原配置值(self):
        # guard 只处理本次提交里出现的键，不能顺手改动其它配置项
        web_app.update_config({'WHISPER_TEMPERATURE': '0.4'})
        result = self._save({'SUBTITLE_MAX_LINE_LENGTH': '999'})
        self.assertEqual(self._numeric(result['updated_config'], 'WHISPER_TEMPERATURE'), 0.4)
        self.assertNotIn('WHISPER_TEMPERATURE', self._warnings(result)[0])

    def test_guard表覆盖的键都在DEFAULT_CONFIG里(self):
        # 回退值取自 DEFAULT_CONFIG，缺键会静默退化成 fallback（1 / 0.0）
        missing = sorted(key for key, _, _ in SETTINGS_RANGE_GUARDS if key not in DEFAULT_CONFIG)
        self.assertEqual(missing, [], f'guard 覆盖的键不在 DEFAULT_CONFIG 中: {missing}')


class AudioChunkCrossGuardTests(_SettingsSaveTestCase):
    """分片重叠必须严格小于分片窗口。"""

    def test_重叠不小于窗口时被钳制(self):
        for window, overlap, expected in CROSS_CASES:
            with self.subTest(window=window, overlap=overlap):
                result = self._save({
                    'AUDIO_CHUNK_WINDOW_S': window,
                    'AUDIO_CHUNK_OVERLAP_S': overlap,
                })
                saved_window = self._numeric(result['updated_config'], 'AUDIO_CHUNK_WINDOW_S')
                saved_overlap = self._numeric(result['updated_config'], 'AUDIO_CHUNK_OVERLAP_S')
                self.assertEqual(saved_overlap, expected)
                self.assertLess(saved_overlap, saved_window,
                                msg='落盘后仍不满足 overlap < window')

    def test_钳制到窗口减一秒并给出原因(self):
        result = self._save({'AUDIO_CHUNK_WINDOW_S': '5', 'AUDIO_CHUNK_OVERLAP_S': '5'})
        warnings = self._warnings(result)
        self.assertEqual(len(warnings), 1, msg=warnings)
        self.assertIn('AUDIO_CHUNK_OVERLAP_S', warnings[0])
        self.assertIn('分片窗口', warnings[0])

    def test_只提交重叠时按已存窗口钳制(self):
        # 脚本可只提交一个键，此时窗口取已存配置值，避免写入违反不变量的组合
        web_app.update_config({'AUDIO_CHUNK_WINDOW_S': '5'})
        result = self._save({'AUDIO_CHUNK_OVERLAP_S': '5'})
        self.assertEqual(self._numeric(result['updated_config'], 'AUDIO_CHUNK_OVERLAP_S'), 4.0)

    def test_分片循环在落盘配置下必然前进(self):
        # 交叉约束的目的：_create_audio_chunks 的步进 (window - overlap) 必须为正
        for window, overlap, _ in CROSS_CASES:
            with self.subTest(window=window, overlap=overlap):
                result = self._save({
                    'AUDIO_CHUNK_WINDOW_S': window,
                    'AUDIO_CHUNK_OVERLAP_S': overlap,
                })
                step = (self._numeric(result['updated_config'], 'AUDIO_CHUNK_WINDOW_S')
                        - self._numeric(result['updated_config'], 'AUDIO_CHUNK_OVERLAP_S'))
                self.assertGreater(step, 0.0, msg=f'步进不为正: window={window} overlap={overlap}')

    def test_只提交窗口时按已存重叠钳制(self):
        # 反向提交：先落盘一个各处都在自己区间内的合法组合（15 / 5），再只把窗口压到
        # 重叠之下。旧逻辑只在「本次提交含 overlap」时才比较，这条路径不触发任何校验，
        # 会落盘 5/5 —— 步进 0，模块兜底把 overlap 钳到 window-0.01 后步进只剩 0.01 秒，
        # 60 秒素材被切成 5502 片、2 小时素材 719501 片。
        web_app.update_config({'AUDIO_CHUNK_WINDOW_S': '15', 'AUDIO_CHUNK_OVERLAP_S': '5'})

        result = self._save({'AUDIO_CHUNK_WINDOW_S': '5'})

        saved_window = self._numeric(result['updated_config'], 'AUDIO_CHUNK_WINDOW_S')
        saved_overlap = self._numeric(result['updated_config'], 'AUDIO_CHUNK_OVERLAP_S')
        self.assertEqual(saved_window, 5.0)
        self.assertLess(saved_overlap, saved_window, msg='落盘后仍不满足 overlap < window')
        self.assertEqual(saved_overlap, 4.0, msg='应向用户可见地钳到 window-1')

        warnings = self._warnings(result)
        self.assertEqual(len(warnings), 1, msg=warnings)
        self.assertIn('AUDIO_CHUNK_OVERLAP_S', warnings[0])
        self.assertIn('分片窗口', warnings[0])
        self.assertEqual(result['final_level'], 'warning')

    def test_只提交窗口且不越界时不误报(self):
        # 反向守卫：合法的窗口提交不能被交叉校验误伤
        web_app.update_config({'AUDIO_CHUNK_WINDOW_S': '15', 'AUDIO_CHUNK_OVERLAP_S': '5'})

        result = self._save({'AUDIO_CHUNK_WINDOW_S': '20'})

        self.assertEqual(self._numeric(result['updated_config'], 'AUDIO_CHUNK_WINDOW_S'), 20.0)
        self.assertEqual(self._numeric(result['updated_config'], 'AUDIO_CHUNK_OVERLAP_S'), 5.0)
        self.assertEqual(self._warnings(result), [])
        self.assertEqual(result['final_level'], 'success')

    def test_已存配置被写坏时再保存会收回重叠(self):
        # update_config 本身不做交叉校验（手工改配置、旧版本落盘都可能留下 5/5）。
        # 终态校验必须覆盖这种「本次两个键都没提交」的情形，否则坏配置会一直留着，
        # 直到模块兜底把步进压到 0.01 秒。
        web_app.update_config({'AUDIO_CHUNK_WINDOW_S': '5', 'AUDIO_CHUNK_OVERLAP_S': '5'})

        result = self._save({'SUBTITLE_MAX_LINES': '2'})

        self.assertLess(
            self._numeric(result['updated_config'], 'AUDIO_CHUNK_OVERLAP_S'),
            self._numeric(result['updated_config'], 'AUDIO_CHUNK_WINDOW_S'),
        )
        self.assertEqual(len(self._warnings(result)), 1, msg=self._warnings(result))


class NoControlKeyGuardTests(_SettingsSaveTestCase):
    """没有 UI 控件的键仍可被 /settings 的 POST 写入，必须由服务端守住。"""

    def test_质检阈值越界被回退(self):
        result = self._save({
            'SUBTITLE_QC_MIN_COVERAGE_RATIO': '3',
            'SUBTITLE_QC_MAX_GAP_S': '-10',
            'SUBTITLE_QC_MAX_CPS': '9999',
        })
        saved = result['updated_config']
        self.assertEqual(self._numeric(saved, 'SUBTITLE_QC_MIN_COVERAGE_RATIO'), 0.15)
        self.assertEqual(self._numeric(saved, 'SUBTITLE_QC_MAX_GAP_S'), 90.0)
        self.assertEqual(self._numeric(saved, 'SUBTITLE_QC_MAX_CPS'), 25.0)
        text = ' '.join(self._warnings(result))
        for key in ('SUBTITLE_QC_MIN_COVERAGE_RATIO', 'SUBTITLE_QC_MAX_GAP_S', 'SUBTITLE_QC_MAX_CPS'):
            self.assertIn(key, text)

    def test_质检覆盖率比值不得超过1(self):
        # subtitle_qc 直接采信该键:比值 > 1 会让覆盖率维度恒失败、字幕永不烧录
        result = self._save({'SUBTITLE_QC_MIN_COVERAGE_RATIO': '1.5'})
        self.assertLessEqual(
            self._numeric(result['updated_config'], 'SUBTITLE_QC_MIN_COVERAGE_RATIO'), 1.0)

    def test_质检阈值合法值原样保留(self):
        result = self._save({
            'SUBTITLE_QC_MIN_COVERAGE_RATIO': '0.2',
            'SUBTITLE_QC_MAX_GAP_S': '120',
            'SUBTITLE_QC_MAX_CPS': '30',
        })
        saved = result['updated_config']
        self.assertEqual(self._numeric(saved, 'SUBTITLE_QC_MIN_COVERAGE_RATIO'), 0.2)
        self.assertEqual(self._numeric(saved, 'SUBTITLE_QC_MAX_GAP_S'), 120.0)
        self.assertEqual(self._numeric(saved, 'SUBTITLE_QC_MAX_CPS'), 30.0)
        self.assertEqual(self._warnings(result), [])


class PinnedWrapPathConsistencyTests(_SettingsSaveTestCase):
    """旧钉死换行组合经设置页提交与经配置迁移，落盘结果必须一致。"""

    def _submit_pinned_wrap(self):
        form = {}
        for key, value in LEGACY_PINNED_WRAP.items():
            form[key] = 'on' if isinstance(value, bool) else str(value)
        return self._save(form)['updated_config']

    def test_两条路径落盘一致(self):
        migrated, changed = migrate_pinned_subtitle_wrap_config(dict(LEGACY_PINNED_WRAP))
        self.assertTrue(changed, '迁移未识别旧钉死组合')
        saved = self._submit_pinned_wrap()

        for key, expected in migrated.items():
            with self.subTest(key=key):
                if isinstance(expected, bool):
                    self.assertEqual(coerce_bool(saved.get(key)), expected,
                                     msg=f'{key} 两条路径结果不一致')
                else:
                    self.assertEqual(float(saved.get(key)), float(expected),
                                     msg=f'{key} 两条路径结果不一致')

    def test_整组回退到默认值(self):
        saved = self._submit_pinned_wrap()
        self.assertEqual(float(saved['SUBTITLE_MAX_LINE_LENGTH']), float(DEFAULT_CONFIG['SUBTITLE_MAX_LINE_LENGTH']))
        self.assertEqual(float(saved['SUBTITLE_MAX_LINES']), float(DEFAULT_CONFIG['SUBTITLE_MAX_LINES']))
        self.assertFalse(coerce_bool(saved['SUBTITLE_MAX_LINE_LENGTH_ENABLED']))
        self.assertFalse(coerce_bool(saved['SUBTITLE_MAX_LINES_ENABLED']))

    def test_提示覆盖整组被改动的键(self):
        result = self._save({
            key: ('on' if isinstance(value, bool) else str(value))
            for key, value in LEGACY_PINNED_WRAP.items()
        })
        text = ' '.join(self._warnings(result))
        for key in LEGACY_PINNED_WRAP:
            self.assertIn(key, text, msg=f'提示未点名 {key}: {text}')

    def test_只提交单行且未越界时不被整组回退(self):
        # 用户主动选择「最多 1 行 + 启用上限」是合法配置，不能因为形状相似就归一
        result = self._save({
            'SUBTITLE_MAX_LINES': '1',
            'SUBTITLE_MAX_LINES_ENABLED': 'on',
        })
        saved = result['updated_config']
        self.assertEqual(float(saved['SUBTITLE_MAX_LINES']), 1.0)
        self.assertTrue(coerce_bool(saved['SUBTITLE_MAX_LINES_ENABLED']))
        self.assertEqual(self._warnings(result), [])


class PinnedSubtitleWrapMigrationTests(unittest.TestCase):
    """迁移三态:命中 / 不命中 / 幂等。"""

    MIGRATION_CASES = (
        ('命中', dict(LEGACY_PINNED_WRAP), True),
        ('行数为 2 不命中', {
            'SUBTITLE_MAX_LINE_LENGTH': 999, 'SUBTITLE_MAX_LINES': 2,
            'SUBTITLE_MAX_LINE_LENGTH_ENABLED': True, 'SUBTITLE_MAX_LINES_ENABLED': True,
        }, False),
        ('开关未全开不命中', {
            'SUBTITLE_MAX_LINE_LENGTH': 999, 'SUBTITLE_MAX_LINES': 1,
            'SUBTITLE_MAX_LINE_LENGTH_ENABLED': True, 'SUBTITLE_MAX_LINES_ENABLED': False,
        }, False),
        ('键缺失不命中', {
            'SUBTITLE_MAX_LINE_LENGTH': 60, 'SUBTITLE_MAX_LINES': 1,
            'SUBTITLE_MAX_LINE_LENGTH_ENABLED': True, 'SUBTITLE_MAX_LINES_ENABLED': True,
        }, False),
        ('空配置不命中', {}, False),
    )

    def test_迁移命中与不命中(self):
        for label, config, expected_changed in self.MIGRATION_CASES:
            with self.subTest(label=label):
                original = dict(config)
                migrated, changed = migrate_pinned_subtitle_wrap_config(dict(config))
                self.assertEqual(changed, expected_changed, msg=label)
                if not expected_changed:
                    self.assertEqual(migrated, original, msg=f'{label} 不应改动配置')

    def test_迁移结果等于DEFAULT_CONFIG(self):
        migrated, changed = migrate_pinned_subtitle_wrap_config(dict(LEGACY_PINNED_WRAP))
        self.assertTrue(changed)
        for key, value in migrated.items():
            with self.subTest(key=key):
                if isinstance(value, bool):
                    self.assertEqual(value, bool(DEFAULT_CONFIG[key]))
                else:
                    self.assertEqual(float(value), float(DEFAULT_CONFIG[key]))

    def test_二次执行幂等(self):
        migrated, first = migrate_pinned_subtitle_wrap_config(dict(LEGACY_PINNED_WRAP))
        again, second = migrate_pinned_subtitle_wrap_config(dict(migrated))
        self.assertTrue(first)
        self.assertFalse(second, '迁移结果再次迁移仍被判定为命中')
        self.assertEqual(again, migrated, '二次迁移改动了配置')

    def test_不修改传入字典(self):
        original = dict(LEGACY_PINNED_WRAP)
        migrate_pinned_subtitle_wrap_config(original)
        self.assertEqual(original, LEGACY_PINNED_WRAP, '迁移不应就地修改调用方传入的配置')


class EmbedGateCopyTests(unittest.TestCase):
    """设置页文案必须与 modules/task_manager 的烧录门控语义一致。

    两个开关的作用域互不重叠，文案写错会让用户按错误预期取消勾选：
    - ASR_FAILURE_BLOCKS_EMBED 只放宽「ASR 来源结局」与「质检没跑成」，
      质检给出的明确失败结论始终生效；
    - SUBTITLE_QC_ENABLED 关闭时不再看历史质检结论、也不再要求降级素材
      通过严格质检（否则「关掉质检反而更严格」），但 ASR 来源失败仍拦截。
    """

    @classmethod
    def setUpClass(cls):
        template_path = (
            pathlib.Path(__file__).resolve().parents[1] / 'templates' / 'settings.html'
        )
        cls.doc = lxml_html.fromstring(template_path.read_text(encoding='utf-8'))

    def _copy_for(self, name):
        nodes = self.doc.xpath(
            f"//input[@name='{name}']"
            "/ancestor::div[contains(@class,'settings-toggle-field')"
            " or contains(@class,'settings-field')][1]")
        self.assertTrue(nodes, f'{name} 附近找不到说明文案容器')
        return ' '.join(nodes[0].text_content().split())

    def test_ASR异常禁止烧录文案点明质检结论不受影响(self):
        copy = self._copy_for('ASR_FAILURE_BLOCKS_EMBED')
        self.assertIn('ASR 来源结局', copy, msg=f'未说明放宽范围: {copy}')
        self.assertIn('质检没跑成', copy, msg=f'未说明「质检没跑成」也被放宽: {copy}')
        self.assertIn('始终生效', copy, msg=f'未声明质检结论不受本开关影响: {copy}')

    def test_质检开关文案点明关闭语义与跑不成的区别(self):
        copy = self._copy_for('SUBTITLE_QC_ENABLED')
        self.assertIn('更严格', copy, msg=f'未说明关闭质检不应更严格: {copy}')
        self.assertIn('没跑成', copy, msg=f'未区分「主动关闭」与「质检没跑成」: {copy}')
        self.assertIn('拦截', copy, msg=f'未说明 ASR 来源失败仍会拦截: {copy}')


class NumericWhitelistControlCoverageTests(unittest.TestCase):
    """反向守卫:数值白名单键必须有数值控件，或登记于豁免表。"""

    @classmethod
    def setUpClass(cls):
        template_path = (
            pathlib.Path(__file__).resolve().parents[1] / 'templates' / 'settings.html'
        )
        cls.source = template_path.read_text(encoding='utf-8')
        cls.doc = lxml_html.fromstring(cls.source)
        cls.control_names = {
            el.get('name')
            for el in cls.doc.xpath("//form[@id='settings-form']//input[@type='number'][@name]")
        }

    def test_每个数值白名单键必须有控件或登记豁免(self):
        numeric_whitelist = set(SETTINGS_INT_FIELDS) | set(SETTINGS_FLOAT_FIELDS)
        missing = sorted(numeric_whitelist - self.control_names - NO_CONTROL_WHITELIST_KEYS)
        self.assertEqual(
            missing, [],
            '这些数值键既没有控件也没有登记豁免:保存时用户看不到、改不动,'
            f'只能靠手工提交配置: {missing}')

    def test_豁免表里的键确实没有控件(self):
        # 反向约束:某天补上了控件却忘记从豁免表里删除，这条会报出来
        stale = sorted(key for key in NO_CONTROL_WHITELIST_KEYS if key in self.control_names)
        self.assertEqual(stale, [], f'这些键已经有控件了,请从豁免表移除: {stale}')

    def test_豁免表没有多余键(self):
        unknown = sorted(key for key in NO_CONTROL_WHITELIST_KEYS
                         if key not in set(SETTINGS_INT_FIELDS) | set(SETTINGS_FLOAT_FIELDS))
        self.assertEqual(unknown, [], f'豁免表登记了不在数值白名单里的键: {unknown}')


class GuardTableShapeTests(unittest.TestCase):
    """guard 表自身的形状约束。"""

    def test_区间合法且键不重复(self):
        seen = set()
        for key, guard_min, guard_max in SETTINGS_RANGE_GUARDS:
            with self.subTest(key=key):
                self.assertNotIn(key, seen, msg=f'{key} 在 guard 表里重复')
                seen.add(key)
                self.assertLess(guard_min, guard_max, msg=f'{key} 区间非法')

    def test_默认值落在guard区间内(self):
        # 页面兜底字面量与 DEFAULT_CONFIG 的关系由 test_settings_template_layout 覆盖,
        # 这里只确认 guard 区间包含 DEFAULT_CONFIG,否则默认值自己会被 guard 回退
        for key, guard_min, guard_max in SETTINGS_RANGE_GUARDS:
            with self.subTest(key=key):
                default = float(DEFAULT_CONFIG[key])
                self.assertTrue(guard_min <= default <= guard_max,
                                msg=f'{key} 的默认值 {default} 不在 guard 区间内')

    def test_中文标签覆盖guard表(self):
        for key, _, _ in SETTINGS_RANGE_GUARDS:
            with self.subTest(key=key):
                self.assertIn(key, web_app.SETTINGS_GUARD_LABELS,
                              msg=f'{key} 缺少回退提示用的中文标签')


if __name__ == '__main__':
    unittest.main()
