# -*- coding: utf-8 -*-
"""A 组：ASR/VAD 质量结局 → 烧录门控闭环回归测试。

覆盖 ``_subtitle_embed_allowed`` 全部分支、``_translate_subtitle`` 在
``failed`` / ``degraded`` / ``ok`` 三种质量结局下的烧录决策、
``_run_subtitle_qc`` 三态返回、``_infer_completed_stages_from_task`` 的
checkpoint 推断、``_get_embedded_video_candidate`` 的产物新鲜度判定，
以及断点续跑/超时重置对 ``subtitle_quality_state`` 的清理。

不发起任何真实网络/FFmpeg 调用：字幕识别器、嵌入、DB、任务读写全部 mock。
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from modules import task_manager as tm


class SubtitleEmbedAllowedGateTests(unittest.TestCase):
    """模块级门控函数的六条分支（含逃生口）。"""

    def test_escape_hatch_only_relaxes_asr_quality_outcome(self):
        """``ASR_FAILURE_BLOCKS_EMBED=False`` 只放宽 ASR 来源结局，不放过质检结论。

        此前该开关在 ``_subtitle_embed_allowed`` 开头直接 ``return True``，
        把「质检明确否决」也一并放行了 —— 与本文件
        ``test_qc_failed_always_blocks_preexisting_subtitle`` 声明的
        「针对具体字幕文件的质检结论必须始终生效」互相矛盾。
        """
        config = {'ASR_FAILURE_BLOCKS_EMBED': False}
        self.assertTrue(tm._subtitle_embed_allowed(config, 'failed', False))
        self.assertTrue(tm._subtitle_embed_allowed(config, 'degraded', False))
        self.assertTrue(tm._subtitle_embed_allowed(config, 'failed', True, qc_cleared=True))
        # 质检结论不受逃生口影响
        self.assertFalse(tm._subtitle_embed_allowed(config, 'failed', True))
        self.assertFalse(tm._subtitle_embed_allowed(config, 'ok', True))

    def test_qc_disabled_relaxes_qc_based_blocking_only(self):
        """主动关闭质检后，历史 qc_failed 与 degraded 不再拦烧录。

        否则「关掉质检」会比开着更严格：degraded 素材永远等不到那次严格质检，
        历史 qc_failed 标记也永远清不掉。
        """
        config = {'SUBTITLE_QC_ENABLED': False}
        self.assertTrue(tm._subtitle_embed_allowed(config, 'ok', True))
        self.assertTrue(tm._subtitle_embed_allowed(config, 'degraded', True))
        # failed 是纯粹的「来源不可信」，与质检开关无关，仍然拦截
        self.assertFalse(tm._subtitle_embed_allowed(config, 'failed', False))
        # degraded 的语义是「必须通过严格质检才放行」；质检被关闭时该条件无法
        # 满足，此时必须放行，否则「关掉质检」会比开着更严格（反向回归）。
        self.assertTrue(tm._subtitle_embed_allowed(config, 'degraded', False))

    def test_block_reason_to_warning_covers_all_reasons(self):
        """每个门控原因都必须有落库文案，否则任务列表会显示原始英文键名。"""
        for reason in (
            'asr_quality_failed',
            'asr_quality_degraded_without_strict_qc',
            'subtitle_qc_rejected',
        ):
            warning = tm._block_reason_to_warning(reason)
            self.assertTrue(warning)
            self.assertEqual(warning, tm.SUBTITLE_BLOCK_WARNING_MESSAGES[reason])
        # 两类 ASR 结局必须落到不同文案，不能都退化成同一个兜底值
        self.assertNotEqual(
            tm._block_reason_to_warning('asr_quality_failed'),
            tm._block_reason_to_warning('asr_quality_degraded_without_strict_qc'),
        )
        self.assertEqual(
            tm._block_reason_to_warning('unknown_reason'), 'asr_failed_block_embed'
        )

    def test_block_reasons_lists_every_active_switch(self):
        """原因列表要能同时表达「来源结局」与「质检结论」两类拦截。"""
        reasons = tm._subtitle_block_reasons(
            {}, 'failed', True, qc_cleared=False
        )
        self.assertIn('asr_quality_failed', reasons)
        self.assertIn('subtitle_qc_rejected', reasons)
        self.assertEqual(tm._subtitle_block_reasons({}, 'ok', False), [])

    def test_failed_state_blocks_embed(self):
        self.assertFalse(tm._subtitle_embed_allowed({}, 'failed', False))
        self.assertFalse(tm._subtitle_embed_allowed({}, 'failed', False, qc_cleared=True))

    def test_qc_failed_blocks_embed(self):
        self.assertFalse(tm._subtitle_embed_allowed({}, 'ok', True))

    def test_qc_failed_but_cleared_allows_embed(self):
        self.assertTrue(tm._subtitle_embed_allowed({}, 'ok', True, qc_cleared=True))

    def test_degraded_without_cleared_qc_blocks_embed(self):
        self.assertFalse(tm._subtitle_embed_allowed({}, 'degraded', False))

    def test_degraded_with_cleared_qc_allows_embed(self):
        self.assertTrue(tm._subtitle_embed_allowed({}, 'degraded', False, qc_cleared=True))

    def test_ok_state_allows_embed(self):
        self.assertTrue(tm._subtitle_embed_allowed({}, 'ok', False))
        self.assertTrue(tm._subtitle_embed_allowed({}, None, 0))

    def test_deny_writes_warning_to_task_logger(self):
        logger = MagicMock()
        self.assertFalse(tm._subtitle_embed_allowed({}, 'failed', False, task_logger=logger))
        logger.warning.assert_called()
        logger2 = MagicMock()
        self.assertTrue(tm._subtitle_embed_allowed({}, 'ok', False, task_logger=logger2))
        logger2.warning.assert_not_called()

    def test_missing_escape_hatch_key_defaults_to_blocking(self):
        # 配置里没有 ASR_FAILURE_BLOCKS_EMBED（旧 config.json）时必须是「拦截」，
        # 否则升级后的老配置会静默关掉门控。
        self.assertFalse(tm._subtitle_embed_allowed({}, 'failed', False))


class _TranslateSubtitleHarness(unittest.TestCase):
    """构造只含 ASR 生成字幕的任务目录，并记录所有 update_task 写入。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='y2a-embed-gate-')
        self.downloads_dir = os.path.join(self.tmpdir, 'downloads')
        self.task_id = 'task-quality-gate'
        self.task_dir = os.path.join(self.downloads_dir, self.task_id)
        os.makedirs(self.task_dir, exist_ok=True)
        self.video_path = os.path.join(self.task_dir, 'video.mp4')
        with open(self.video_path, 'wb') as handle:
            handle.write(b'fake video')
        self.writes = []

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_recognizer(self, quality_state, reasons=None):
        recognizer = MagicMock()
        recognizer.last_quality_state = quality_state
        recognizer.last_degraded_reasons = list(reasons or [])
        recognizer.last_warning_message = ''
        recognizer.last_error_message = ''
        asr_path = os.path.join(self.task_dir, f'asr_{self.task_id}.srt')

        def fake_transcribe(video_path, output_path):
            with open(output_path, 'w', encoding='utf-8') as handle:
                handle.write('1\n00:00:00,000 --> 00:00:02,000\nhello world\n')
            return output_path

        recognizer.transcribe_video_to_subtitles.side_effect = fake_transcribe
        recognizer.expected_path = asr_path
        return recognizer

    def _make_translator(self):
        """构造只写盘、不联网的翻译器桩件。"""
        translator = MagicMock()

        def fake_translate_file(source, target, progress_callback=None, cancel_event=None):
            with open(target, 'w', encoding='utf-8') as handle:
                handle.write('1\n00:00:00,000 --> 00:00:02,000\n你好世界\n')
            return True

        translator.translate_file.side_effect = fake_translate_file
        return translator

    def run_translate(self, task, config, recognizer, qc_side_effect=None,
                      translation_enabled=False, embed_enabled=True, translator=None,
                      patch_qc=True, qc_returns=True):
        config = dict(config)
        config.setdefault('SPEECH_RECOGNITION_ENABLED', True)
        config['SUBTITLE_TRANSLATION_ENABLED'] = translation_enabled
        config['SUBTITLE_EMBED_IN_VIDEO'] = embed_enabled

        processor = tm.TaskProcessor(config)
        processor._embed_subtitle_in_video = MagicMock(
            return_value=os.path.join(self.task_dir, 'video_with_subtitle.mp4')
        )
        if patch_qc:
            if qc_side_effect is not None:
                processor._run_subtitle_qc = MagicMock(side_effect=qc_side_effect)
            else:
                processor._run_subtitle_qc = MagicMock(return_value=qc_returns)

        def fake_get_task(task_id):
            self.assertEqual(task_id, self.task_id)
            return dict(task)

        def fake_update_task(task_id, **kwargs):
            self.assertEqual(task_id, self.task_id)
            self.writes.append(dict(kwargs))
            for key, value in kwargs.items():
                if key != 'silent':
                    task[key] = value
            return True

        with patch.object(tm, 'DOWNLOADS_DIR', self.downloads_dir), \
                patch.object(tm, 'get_task', side_effect=fake_get_task), \
                patch.object(tm, 'update_task', side_effect=fake_update_task), \
                patch('modules.speech_recognition.create_speech_recognizer_from_config',
                      return_value=recognizer), \
                patch('modules.subtitle_translator.create_translator_from_config',
                      return_value=translator or self._make_translator()):
            result = processor._translate_subtitle(self.task_id, MagicMock())

        return processor, result

    def written_keys(self):
        keys = set()
        for write in self.writes:
            keys |= set(write.keys())
        return keys

    def last_value(self, key, default=None):
        for write in reversed(self.writes):
            if key in write:
                return write[key]
        return default

    @staticmethod
    def base_task(task_id):
        return {
            'id': task_id,
            'status': tm.TASK_STATES['TRANSLATING_SUBTITLE'],
            'video_path_local': None,
        }


class TranslateSubtitleQualityGateTests(_TranslateSubtitleHarness):
    def test_failed_quality_state_blocks_embed_and_keeps_checkpoint_clean(self):
        task = self.base_task(self.task_id)
        task['video_path_local'] = self.video_path
        recognizer = self._make_recognizer('failed', ['vad_all_windows_failed'])

        processor, result = self.run_translate(task, {}, recognizer)

        self.assertTrue(result)
        processor._embed_subtitle_in_video.assert_not_called()
        processor._run_subtitle_qc.assert_not_called()
        self.assertEqual(self.last_value('subtitle_warning_message'), 'asr_failed_block_embed')
        self.assertNotIn('subtitle_path_original', self.written_keys())
        self.assertNotIn('subtitle_path_translated', self.written_keys())
        self.assertNotIn('video_path_local', self.written_keys())
        self.assertEqual(self.last_value('subtitle_quality_state'), 'failed')

    def test_degraded_quality_state_with_qc_failure_blocks_embed(self):
        task = self.base_task(self.task_id)
        task['video_path_local'] = self.video_path
        recognizer = self._make_recognizer('degraded', ['asr_high_failure_ratio: 0.60'])

        processor, result = self.run_translate(task, {}, recognizer, qc_side_effect=[False])

        self.assertTrue(result)
        processor._embed_subtitle_in_video.assert_not_called()
        processor._run_subtitle_qc.assert_called_once()
        self.assertEqual(processor._run_subtitle_qc.call_args.kwargs.get('strict'), True)
        self.assertEqual(self.last_value('subtitle_warning_message'), 'subtitle_qc_rejected')
        self.assertNotIn('subtitle_path_original', self.written_keys())

    def test_degraded_quality_state_with_qc_pass_embeds_once(self):
        task = self.base_task(self.task_id)
        task['video_path_local'] = self.video_path
        recognizer = self._make_recognizer('degraded', ['asr_high_failure_ratio: 0.60'])

        processor, result = self.run_translate(task, {}, recognizer, qc_side_effect=[True])

        self.assertTrue(result)
        processor._embed_subtitle_in_video.assert_called_once()
        self.assertEqual(processor._run_subtitle_qc.call_args.kwargs.get('strict'), True)
        self.assertEqual(self.last_value('subtitle_warning_message'), None)

    def test_degraded_quality_state_with_qc_unavailable_blocks_embed(self):
        # 质检「没跑成」（None）必须按拒绝处理，不能当成通过。
        task = self.base_task(self.task_id)
        task['video_path_local'] = self.video_path
        recognizer = self._make_recognizer('degraded', ['asr_high_failure_ratio: 0.60'])

        processor, result = self.run_translate(task, {}, recognizer, qc_side_effect=[None])

        self.assertTrue(result)
        processor._embed_subtitle_in_video.assert_not_called()
        self.assertEqual(self.last_value('subtitle_warning_message'), 'qc_unavailable')

    def test_ok_quality_state_with_qc_pass_embeds_once(self):
        task = self.base_task(self.task_id)
        task['video_path_local'] = self.video_path
        recognizer = self._make_recognizer('ok', [])

        processor, result = self.run_translate(task, {}, recognizer, qc_side_effect=[True])

        self.assertTrue(result)
        processor._embed_subtitle_in_video.assert_called_once()
        self.assertEqual(processor._run_subtitle_qc.call_args.kwargs.get('strict'), False)
        self.assertEqual(self.last_value('subtitle_path_original'),
                         recognizer.expected_path)

    def test_gate_blocks_embed_for_preexisting_subtitle(self):
        # 非 ASR 来源（task_dir 里已有字幕）：门控被真实接线，
        # failed 质量结局 + qc_failed 会拦下烧录。
        subtitle_path = os.path.join(self.task_dir, 'video.en.srt')
        with open(subtitle_path, 'w', encoding='utf-8') as handle:
            handle.write('1\n00:00:00,000 --> 00:00:02,000\nhello world\n')
        task = self.base_task(self.task_id)
        task['video_path_local'] = self.video_path
        task['subtitle_quality_state'] = 'failed'
        task['subtitle_qc_failed'] = 1

        processor, result = self.run_translate(
            task, {}, MagicMock(), translation_enabled=False, embed_enabled=True
        )

        self.assertTrue(result)
        processor._embed_subtitle_in_video.assert_not_called()
        self.assertNotIn('video_path_local', self.written_keys())

    def test_stale_quality_state_alone_does_not_block_preexisting_subtitle(self):
        # subtitle_quality_state 描述的是「本次 ASR 产物」的来源可靠度，
        # 已有字幕不该被历史 failed 标记永久阻断（否则一次 ASR 失败即锁死烧录）。
        asr_path = os.path.join(self.task_dir, f'asr_{self.task_id}.srt')
        with open(asr_path, 'w', encoding='utf-8') as handle:
            handle.write('1\n00:00:00,000 --> 00:00:02,000\nhello world\n')
        task = self.base_task(self.task_id)
        task['video_path_local'] = self.video_path
        task['subtitle_quality_state'] = 'failed'

        processor, result = self.run_translate(
            task, {}, MagicMock(), translation_enabled=False, embed_enabled=True
        )

        self.assertTrue(result)
        processor._embed_subtitle_in_video.assert_called_once()

    def test_escape_hatch_restores_legacy_embed_for_fresh_asr_artifact(self):
        """逃生口必须对「本次 ASR 现场生成的产物」也生效。

        此前 ``_translate_subtitle`` 的 failed 早退分支不看该开关，
        导致 ASR_FAILURE_BLOCKS_EMBED=False 只对 task_dir 里已存在的字幕有效，
        用户想恢复旧行为时仍被无条件拦截。
        """
        task = self.base_task(self.task_id)
        task['video_path_local'] = self.video_path
        recognizer = self._make_recognizer('failed', ['vad_no_speech'])

        processor, result = self.run_translate(
            task, {'ASR_FAILURE_BLOCKS_EMBED': False}, recognizer,
            translation_enabled=False, embed_enabled=True,
            qc_side_effect=[True],
        )

        self.assertTrue(result)
        processor._embed_subtitle_in_video.assert_called_once()

    def test_escape_hatch_tolerates_qc_unavailable_for_fresh_asr_artifact(self):
        """逃生口生效时，「质检没跑成」也不应阻断烧录（否则开关无法恢复旧行为）。"""
        task = self.base_task(self.task_id)
        task['video_path_local'] = self.video_path
        recognizer = self._make_recognizer('failed', ['vad_no_speech'])

        processor, result = self.run_translate(
            task, {'ASR_FAILURE_BLOCKS_EMBED': False}, recognizer,
            translation_enabled=False, embed_enabled=True,
            qc_side_effect=[None],
        )

        self.assertTrue(result)
        processor._embed_subtitle_in_video.assert_called_once()

    def test_escape_hatch_still_blocks_on_explicit_qc_rejection(self):
        """逃生口只放宽「来源退化」与「质检不可用」，不放过明确的质检失败结论。"""
        task = self.base_task(self.task_id)
        task['video_path_local'] = self.video_path
        recognizer = self._make_recognizer('failed', ['vad_no_speech'])

        processor, result = self.run_translate(
            task, {'ASR_FAILURE_BLOCKS_EMBED': False}, recognizer,
            translation_enabled=False, embed_enabled=True,
            qc_side_effect=[False],
        )

        self.assertTrue(result)
        processor._embed_subtitle_in_video.assert_not_called()
        self.assertEqual(self.last_value('subtitle_warning_message'), 'subtitle_qc_rejected')

    def test_qc_failed_always_blocks_preexisting_subtitle(self):
        # 与上一条对照：subtitle_qc_failed 针对具体字幕文件的质检结论，必须始终生效。
        asr_path = os.path.join(self.task_dir, f'asr_{self.task_id}.srt')
        with open(asr_path, 'w', encoding='utf-8') as handle:
            handle.write('1\n00:00:00,000 --> 00:00:02,000\nhello world\n')
        task = self.base_task(self.task_id)
        task['video_path_local'] = self.video_path
        task['subtitle_quality_state'] = 'failed'
        task['subtitle_qc_failed'] = 1

        processor, result = self.run_translate(
            task, {}, MagicMock(), translation_enabled=False, embed_enabled=True
        )

        self.assertTrue(result)
        processor._embed_subtitle_in_video.assert_not_called()
        # 被拦截的 ASR 产物必须移出扫描范围（改名为 .rejected.txt），否则下一轮
        # os.listdir 会命中它并复用这条不合格字幕，用户重跑永远拿不到新字幕。
        self.assertFalse(os.path.exists(asr_path))
        self.assertTrue(os.path.exists(f"{os.path.splitext(asr_path)[0]}.rejected.txt"))
        self.assertNotIn('video_path_local', self.written_keys())
        completed = tm._infer_completed_stages_from_task(task)
        self.assertNotIn(tm.PIPELINE_STAGE_TRANSLATE_SUBTITLE, completed)

    def test_escape_hatch_does_not_bypass_qc_rejection_for_preexisting_subtitle(self):
        """逃生口只放宽 ASR 来源结局；明确的质检结论必须仍然生效。

        此前 ``_subtitle_embed_allowed`` 在开关为 False 时直接放行，
        与 ``test_qc_failed_always_blocks_preexisting_subtitle`` 的契约冲突。
        """
        subtitle_path = os.path.join(self.task_dir, 'video.en.srt')
        with open(subtitle_path, 'w', encoding='utf-8') as handle:
            handle.write('1\n00:00:00,000 --> 00:00:02,000\nhello world\n')
        task = self.base_task(self.task_id)
        task['video_path_local'] = self.video_path
        task['subtitle_quality_state'] = 'failed'
        task['subtitle_qc_failed'] = 1

        processor, result = self.run_translate(
            task, {'ASR_FAILURE_BLOCKS_EMBED': False}, MagicMock(),
            translation_enabled=False, embed_enabled=True,
        )

        self.assertTrue(result)
        processor._embed_subtitle_in_video.assert_not_called()
        self.assertEqual(self.last_value('subtitle_warning_message'), 'subtitle_qc_rejected')
        # 外部字幕不是 ASR 产物：不该被改名
        self.assertTrue(os.path.exists(subtitle_path))

    def test_escape_hatch_restores_legacy_embed_when_qc_never_rejected(self):
        """逃生口在「没有质检失败结论」时确实恢复旧行为。"""
        subtitle_path = os.path.join(self.task_dir, 'video.en.srt')
        with open(subtitle_path, 'w', encoding='utf-8') as handle:
            handle.write('1\n00:00:00,000 --> 00:00:02,000\nhello world\n')
        task = self.base_task(self.task_id)
        task['video_path_local'] = self.video_path
        task['subtitle_quality_state'] = 'failed'
        task['subtitle_qc_failed'] = 0

        processor, result = self.run_translate(
            task, {'ASR_FAILURE_BLOCKS_EMBED': False}, MagicMock(),
            translation_enabled=False, embed_enabled=True,
        )

        self.assertTrue(result)
        processor._embed_subtitle_in_video.assert_called_once()


class TranslateBranchGateTests(_TranslateSubtitleHarness):
    """翻译分支的双闸门：复用已有 asr_*.srt 也必须先过质检。

    此前翻译分支只有 ``_embed_guard`` 单闸门，而 ``asr_artifact=False`` 会把
    质量结局短路成 ``ok``，于是「已有 asr_*.srt + 翻译开启」这条路径既不质检
    也不受门控，直接把未经质检的字幕烧进成片。
    """

    def _asr_srt(self):
        path = os.path.join(self.task_dir, f'asr_{self.task_id}.srt')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('1\n00:00:00,000 --> 00:00:02,000\nhello world\n')
        return path

    def test_reused_asr_subtitle_must_pass_qc_before_embed(self):
        asr_path = self._asr_srt()
        task = self.base_task(self.task_id)
        task['video_path_local'] = self.video_path

        processor, result = self.run_translate(
            task, {}, MagicMock(), translation_enabled=True, embed_enabled=True,
            qc_side_effect=[False],
        )

        self.assertTrue(result)
        processor._run_subtitle_qc.assert_called_once()
        self.assertEqual(processor._run_subtitle_qc.call_args.args[1], asr_path)
        processor._embed_subtitle_in_video.assert_not_called()
        self.assertEqual(self.last_value('subtitle_warning_message'), 'subtitle_qc_rejected')

    def test_reused_asr_subtitle_with_passing_qc_embeds_translated(self):
        self._asr_srt()
        task = self.base_task(self.task_id)
        task['video_path_local'] = self.video_path

        processor, result = self.run_translate(
            task, {}, MagicMock(), translation_enabled=True, embed_enabled=True,
            qc_side_effect=[True],
        )

        self.assertTrue(result)
        processor._run_subtitle_qc.assert_called_once()
        processor._embed_subtitle_in_video.assert_called_once()
        embedded_source = processor._embed_subtitle_in_video.call_args.args[2]
        self.assertEqual(
            os.path.basename(embedded_source), f'translated_{self.task_id}.srt'
        )

    def test_reused_asr_subtitle_blocked_by_gate_is_quarantined(self):
        """被门控拦截的 ASR 产物必须改名，否则下轮重跑永远复用旧字幕。"""
        asr_path = self._asr_srt()
        task = self.base_task(self.task_id)
        task['video_path_local'] = self.video_path
        task['subtitle_qc_failed'] = 1

        processor, result = self.run_translate(
            task, {}, MagicMock(), translation_enabled=True, embed_enabled=True,
        )

        self.assertTrue(result)
        processor._embed_subtitle_in_video.assert_not_called()
        self.assertFalse(os.path.exists(asr_path))
        self.assertTrue(os.path.exists(f"{os.path.splitext(asr_path)[0]}.rejected.txt"))
        self.assertEqual(self.last_value('subtitle_warning_message'), 'subtitle_qc_rejected')

    def test_qc_disabled_embeds_reused_asr_subtitle_without_quarantine(self):
        """关闭质检后复用已有 ASR 字幕必须放行 —— 不能比开着质检更严格。

        这里刻意不替换 ``_run_subtitle_qc``，走真实方法验证
        ``SUBTITLE_QC_ENABLED=False`` 返回 ``SUBTITLE_QC_DISABLED`` 而不是 ``None``。
        """
        asr_path = self._asr_srt()
        task = self.base_task(self.task_id)
        task['video_path_local'] = self.video_path
        task['subtitle_qc_failed'] = 1

        processor, result = self.run_translate(
            task, {'SUBTITLE_QC_ENABLED': False}, MagicMock(),
            translation_enabled=False, embed_enabled=True, patch_qc=False,
        )

        self.assertTrue(result)
        processor._embed_subtitle_in_video.assert_called_once()
        self.assertTrue(os.path.exists(asr_path))
        self.assertIsNone(self.last_value('subtitle_warning_message'))

    def test_qc_unsupported_reason_is_not_overwritten(self):
        """质检「没跑成」的归因必须保留 qc_unavailable，不能被兜底文案覆盖。"""
        self._asr_srt()
        task = self.base_task(self.task_id)
        task['video_path_local'] = self.video_path

        processor, result = self.run_translate(
            task, {}, MagicMock(), translation_enabled=False, embed_enabled=True,
            qc_side_effect=[None],
        )

        self.assertTrue(result)
        processor._embed_subtitle_in_video.assert_not_called()
        self.assertEqual(self.last_value('subtitle_warning_message'), 'qc_unavailable')


class EnsureAsrSubtitleQcGateTests(unittest.TestCase):
    """方法级闸门：ASR 产物必须过质检，外部字幕放行。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='y2a-asr-qc-gate-')
        self.task_id = 'task-asr-qc'
        self.writes = []
        self.processor = tm.TaskProcessor({})

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _srt(self, name):
        path = os.path.join(self.tmpdir, name)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('1\n00:00:00,000 --> 00:00:02,000\nhello world\n')
        return path

    def _run(self, subtitle_path, quality_state, qc_result):
        self.processor._run_subtitle_qc = MagicMock(return_value=qc_result)
        self.processor._quarantine_rejected_subtitle = MagicMock(return_value='x.rejected.txt')

        def fake_update_task(task_id, **kwargs):
            self.writes.append(dict(kwargs))
            return True

        with patch.object(tm, 'update_task', side_effect=fake_update_task):
            return self.processor._ensure_asr_subtitle_qc(
                self.task_id, subtitle_path, MagicMock(), quality_state
            )

    def test_asr_artifact_must_pass_qc(self):
        allowed, cleared, _reason = self._run(self._srt(f'asr_{self.task_id}.srt'), 'ok', True)
        self.assertTrue(allowed)
        self.assertTrue(cleared)
        self.processor._run_subtitle_qc.assert_called_once()

    def test_asr_artifact_rejected_quarantines_file(self):
        allowed, cleared, reason = self._run(
            self._srt(f'asr_{self.task_id}.srt'), 'ok', False
        )
        self.assertFalse(allowed)
        self.assertFalse(cleared)
        self.assertEqual(reason, 'subtitle_qc_rejected')
        self.processor._quarantine_rejected_subtitle.assert_called_once()

    def test_asr_artifact_with_unavailable_qc_is_rejected(self):
        allowed, cleared, reason = self._run(
            self._srt(f'asr_{self.task_id}.srt'), 'ok', None
        )
        self.assertFalse(allowed)
        self.assertFalse(cleared)
        self.assertEqual(reason, 'qc_unavailable')

    def test_asr_artifact_with_disabled_qc_is_allowed_without_clearing_qc(self):
        """质检被主动关闭时放行，且不谎称「本次已通过质检」。

        必须与 ``None``（质检启用但没跑成）区分：两者混用会让关闭质检
        反而拒绝全部 ASR 字幕并改名。
        """
        allowed, cleared, reason = self._run(
            self._srt(f'asr_{self.task_id}.srt'), 'ok', tm.SUBTITLE_QC_DISABLED
        )
        self.assertTrue(allowed)
        self.assertFalse(cleared)
        self.assertEqual(reason, '')
        self.processor._quarantine_rejected_subtitle.assert_not_called()

    def test_external_subtitle_skips_qc_entirely(self):
        # 外部字幕（平台自带/人工）不是幻觉高风险来源，不应额外增加 AI 调用。
        allowed, cleared, _reason = self._run(self._srt('video.en.srt'), 'ok', False)
        self.assertTrue(allowed)
        self.assertFalse(cleared)
        self.processor._run_subtitle_qc.assert_not_called()

    def test_degraded_state_forces_strict_qc(self):
        self._run(self._srt(f'asr_{self.task_id}.srt'), 'degraded', True)
        self.assertEqual(self.processor._run_subtitle_qc.call_args.kwargs.get('strict'), True)

    def test_non_asr_artifact_with_degraded_state_is_still_gated(self):
        # 任务级 degraded 结局说明该任务经历过 ASR，其字幕按 ASR 产物对待。
        allowed, _cleared, _reason = self._run(self._srt('video.en.srt'), 'degraded', False)
        self.assertFalse(allowed)


class RunSubtitleQcTriStateTests(unittest.TestCase):
    """``_run_subtitle_qc`` 的三态返回。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='y2a-run-qc-')
        self.srt_path = os.path.join(self.tmpdir, 'asr.srt')
        with open(self.srt_path, 'w', encoding='utf-8') as handle:
            handle.write('1\n00:00:00,000 --> 00:00:02,000\nhello world\n')
        self.processor = tm.TaskProcessor({})
        self.processor._get_video_duration = MagicMock(return_value=None)
        self.updates = []

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, config, srt_path=None, run_side_effect=None, run_return=None):
        self.processor.config = dict(config)
        self.updates = []

        def fake_update_task(task_id, **kwargs):
            self.updates.append(dict(kwargs))
            return True

        target = run_side_effect if run_side_effect is not None else run_return
        with patch.object(tm, 'update_task', side_effect=fake_update_task), \
                patch.object(tm, 'get_task', return_value={'video_path_local': ''}), \
                patch('modules.subtitle_qc.run_subtitle_qc',
                      side_effect=run_side_effect if run_side_effect is not None else None,
                      return_value=None if run_side_effect is not None else target):
            return self.processor._run_subtitle_qc(
                'task-qc', srt_path or self.srt_path, MagicMock()
            )

    def test_disabled_returns_sentinel_not_none(self):
        """主动关闭质检必须与「质检没跑成」区分开。"""
        result = self._run({'SUBTITLE_QC_ENABLED': False})
        self.assertIs(result, tm.SUBTITLE_QC_DISABLED)
        self.assertIsNotNone(result)
        self.assertEqual(self.updates, [])

    def test_missing_file_returns_none(self):
        missing = os.path.join(self.tmpdir, 'nope.srt')
        self.assertIsNone(self._run({'SUBTITLE_QC_ENABLED': True}, srt_path=missing))
        self.assertEqual(self.updates, [])

    def test_exception_returns_none_not_true(self):
        result = self._run({'SUBTITLE_QC_ENABLED': True},
                           run_side_effect=RuntimeError('boom'))
        self.assertIsNone(result)
        self.assertEqual(self.updates, [])

    def test_passed_returns_true_and_persists_zero(self):
        qc_result = MagicMock(passed=True, reason='rule_pass:healthy_distribution',
                              score=1.0, rule_score=1.0, ai_score=None,
                              decision='rule_pass', sample_items=0, sample_chars=0,
                              raw_ai={'ai_mode': 'strict', 'ai_override': False})
        result = self._run({'SUBTITLE_QC_ENABLED': True}, run_return=qc_result)
        self.assertIs(result, True)
        self.assertEqual(self.updates[-1]['subtitle_qc_failed'], 0)
        self.assertEqual(self.updates[-1]['subtitle_qc_reason'],
                         'rule_pass:healthy_distribution')

    def test_not_passed_returns_false_and_persists_one(self):
        qc_result = MagicMock(passed=False, reason='rule_fail:timeline_coverage_too_low',
                              score=0.2, rule_score=0.2, ai_score=None,
                              decision='rule_fail', sample_items=0, sample_chars=0,
                              raw_ai={'ai_mode': 'strict', 'ai_override': False})
        result = self._run({'SUBTITLE_QC_ENABLED': True}, run_return=qc_result)
        self.assertIs(result, False)
        self.assertEqual(self.updates[-1]['subtitle_qc_failed'], 1)

    def test_strict_flag_is_forwarded(self):
        qc_result = MagicMock(passed=True, reason='ok', score=1.0, rule_score=1.0,
                              ai_score=None, decision='rule_pass', sample_items=0,
                              sample_chars=0, raw_ai={})
        captured = {}

        def fake_run(path, config, total_duration_s=None, strict=False):
            captured['strict'] = strict
            return qc_result

        with patch.object(tm, 'update_task', return_value=True), \
                patch.object(tm, 'get_task', return_value={'video_path_local': ''}), \
                patch('modules.subtitle_qc.run_subtitle_qc', side_effect=fake_run):
            self.processor.config = {'SUBTITLE_QC_ENABLED': True}
            self.processor._run_subtitle_qc('task-qc', self.srt_path, MagicMock(), strict=True)

        self.assertIs(captured['strict'], True)


class InferCompletedStagesTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='y2a-infer-')
        self.original_srt = os.path.join(self.tmpdir, 'orig.srt')
        self.translated_srt = os.path.join(self.tmpdir, 'translated.srt')
        for path in (self.original_srt, self.translated_srt):
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write('1\n00:00:00,000 --> 00:00:02,000\nhello\n')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_qc_failed_with_original_subtitle_not_marked_completed(self):
        task = {
            'id': 't1',
            'subtitle_qc_failed': 1,
            'subtitle_path_original': self.original_srt,
            'subtitle_path_translated': None,
        }
        self.assertNotIn(tm.PIPELINE_STAGE_TRANSLATE_SUBTITLE,
                         tm._infer_completed_stages_from_task(task))

    def test_quality_state_failed_not_marked_completed(self):
        task = {
            'id': 't2',
            'subtitle_quality_state': 'failed',
            'subtitle_path_original': self.original_srt,
            'subtitle_path_translated': None,
        }
        self.assertNotIn(tm.PIPELINE_STAGE_TRANSLATE_SUBTITLE,
                         tm._infer_completed_stages_from_task(task))

    def test_valid_translated_subtitle_still_marked_completed(self):
        task = {
            'id': 't3',
            'subtitle_qc_failed': 1,
            'subtitle_quality_state': 'failed',
            'subtitle_path_original': self.original_srt,
            'subtitle_path_translated': self.translated_srt,
        }
        self.assertIn(tm.PIPELINE_STAGE_TRANSLATE_SUBTITLE,
                      tm._infer_completed_stages_from_task(task))

    def test_healthy_task_marks_subtitle_from_original(self):
        task = {
            'id': 't4',
            'subtitle_qc_failed': 0,
            'subtitle_quality_state': 'ok',
            'subtitle_path_original': self.original_srt,
            'subtitle_path_translated': None,
        }
        self.assertIn(tm.PIPELINE_STAGE_TRANSLATE_SUBTITLE,
                      tm._infer_completed_stages_from_task(task))

    def test_checkpoint_runner_skips_stage_when_required(self):
        # 整链路校验：_get_completed_stages 会把「质检失败」的任务排除在字幕阶段之外
        task = {
            'id': 't5',
            'pipeline_checkpoint': '{"version": 1, "completed": []}',
            'subtitle_qc_failed': 1,
            'subtitle_path_original': self.original_srt,
        }
        self.assertNotIn(tm.PIPELINE_STAGE_TRANSLATE_SUBTITLE,
                         tm._get_completed_stages(task))


class EmbeddedVideoCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='y2a-embed-cand-')
        self.video_path = os.path.join(self.tmpdir, 'video.mp4')
        self.candidate = os.path.join(self.tmpdir, 'video_with_subtitle.mp4')
        self.subtitle = os.path.join(self.tmpdir, 'video.en.srt')
        for path in (self.video_path, self.candidate, self.subtitle):
            with open(path, 'wb') as handle:
                handle.write(b'x')
        self._set_mtimes(video=1000, candidate=1000, subtitle=1000)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _set_mtimes(self, video=None, candidate=None, subtitle=None):
        if video is not None:
            os.utime(self.video_path, (video, video))
        if candidate is not None:
            os.utime(self.candidate, (candidate, candidate))
        if subtitle is not None:
            os.utime(self.subtitle, (subtitle, subtitle))

    @staticmethod
    def _call(video_path, subtitle_paths=None):
        # 该方法不使用 self，用哑对象作 self 以免触发 TaskProcessor 初始化副作用。
        return tm.TaskProcessor._get_embedded_video_candidate(
            object(), video_path, subtitle_paths
        )

    def test_stale_product_is_rejected(self):
        self._set_mtimes(candidate=900, subtitle=1000)
        self.assertEqual(self._call(self.video_path, [self.subtitle]), '')

    def test_fresh_product_is_returned(self):
        self._set_mtimes(candidate=2000, subtitle=1000)
        self.assertEqual(self._call(self.video_path, [self.subtitle]), self.candidate)

    def test_without_subtitle_paths_keeps_legacy_behaviour(self):
        self._set_mtimes(candidate=900, subtitle=1000)
        self.assertEqual(self._call(self.video_path), self.candidate)

    def test_missing_subtitle_does_not_disqualify(self):
        self._set_mtimes(candidate=900, subtitle=1000)
        missing = os.path.join(self.tmpdir, 'gone.srt')
        self.assertEqual(self._call(self.video_path, [missing]), self.candidate)

    def test_embedded_video_itself_is_freshness_checked(self):
        # video_path_local 直接指向 _with_subtitle 产物时也要判新鲜度
        self._set_mtimes(video=900, candidate=900, subtitle=1000)
        self.assertEqual(self._call(self.candidate, [self.subtitle]), '')

    def test_invalid_input_returns_empty(self):
        self.assertEqual(self._call(''), '')
        self.assertEqual(self._call(os.path.join(self.tmpdir, 'nope.mp4')), '')


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []
        self.committed = False
        self.closed = False

    def execute(self, sql, params=None):
        self.statements.append((' '.join(str(sql).split()), params))
        if str(sql).lstrip().upper().startswith('SELECT'):
            return _FakeCursor(self.rows)
        return _FakeCursor([])

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True

    def update_sql(self):
        return [sql for sql, _params in self.statements if sql.upper().startswith('UPDATE')]


class StuckTaskResetClearsQualityStateTests(unittest.TestCase):
    """断点续跑 / 超时重置必须清掉 subtitle_quality_state，避免永久锁死。"""

    def test_recover_interrupted_tasks_clears_quality_state(self):
        rows = [{
            'id': 'task-recover',
            'status': tm.TASK_STATES['ASR_TRANSCRIBING'],
            'upload_target': None,
            'acfun_upload_response': None,
            'bilibili_upload_response': None,
        }]
        conn = _FakeConnection(rows)
        with patch.object(tm, 'get_db_connection', return_value=conn):
            recovered = tm.recover_interrupted_tasks_to_pending()

        self.assertEqual(recovered, 1)
        updates = conn.update_sql()
        self.assertEqual(len(updates), 1)
        self.assertIn('subtitle_quality_state = NULL', updates[0])
        self.assertIn('subtitle_qc_failed = 0', updates[0])
        self.assertTrue(conn.committed)
        self.assertTrue(conn.closed)

    def test_reset_stuck_tasks_clears_quality_state(self):
        rows = [('task-stuck', tm.TASK_STATES['TRANSLATING_SUBTITLE'],
                 '2000-01-01 00:00:00')]
        conn = _FakeConnection(rows)
        with patch.object(tm, 'get_db_connection', return_value=conn):
            reset_count = tm.reset_stuck_tasks()

        self.assertEqual(reset_count, 1)
        updates = conn.update_sql()
        self.assertEqual(len(updates), 1)
        self.assertIn('subtitle_quality_state = NULL', updates[0])
        self.assertIn('subtitle_qc_failed = 0', updates[0])
        self.assertTrue(conn.closed)


if __name__ == '__main__':
    unittest.main()
