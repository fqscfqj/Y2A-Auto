import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from flask import render_template

import app as web_app
from modules import config_manager as cm
from modules import task_manager as tm


class CoverModerationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.task = {
            'id': 'cover-moderation',
            'video_title_original': 'Original title',
            'video_title_translated': 'Translated title',
            'description_original': 'Description',
            'tags_generated': '["tag"]',
            'cover_path_local': 'downloads/cover-moderation/cover.jpg',
        }
        self.processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
        self.processor.config = {'CONTENT_MODERATION_ENABLED': True}
        self.processor._recover_cover_path = MagicMock(return_value=self.task['cover_path_local'])
        self.logger = MagicMock()
        self.moderator = MagicMock()
        self.moderator.moderate_text.return_value = {'pass': True, 'details': []}
        self.moderator.moderate_image.return_value = {'pass': True, 'details': []}

    def run_moderation(self):
        def update(_task_id, **values):
            self.task.update(values)

        with patch.object(tm, 'get_task', return_value=self.task), \
             patch.object(tm, 'update_task', side_effect=update), \
             patch('modules.content_moderator.AlibabaCloudModerator', return_value=self.moderator):
            self.processor._moderate_content(self.task['id'], self.logger)
        return json.loads(self.task['moderation_result'])

    def test_cover_disabled_by_default_does_not_read_or_upload_cover(self):
        result = self.run_moderation()
        self.assertTrue(result['overall_pass'])
        self.assertEqual(result['cover']['status'], 'disabled')
        self.moderator.moderate_image.assert_not_called()
        self.processor._recover_cover_path.assert_not_called()
        self.assertEqual(self.moderator.moderate_text.call_count, 3)
        self.moderator.moderate_text.assert_any_call('Translated title', service_type='comment_detection_pro')

    def test_explicit_cover_disable_keeps_text_moderation(self):
        self.processor.config['CONTENT_MODERATION_COVER_ENABLED'] = False
        self.moderator.moderate_text.return_value = {'pass': False, 'details': []}
        result = self.run_moderation()
        self.assertFalse(result['overall_pass'])
        self.moderator.moderate_image.assert_not_called()
        self.assertEqual(self.task['status'], tm.TASK_STATES['AWAITING_REVIEW'])

    def test_master_disable_prevents_all_moderation(self):
        self.processor.config.update(CONTENT_MODERATION_ENABLED=False, CONTENT_MODERATION_COVER_ENABLED=True)
        with patch.object(tm, 'get_task') as get_task, \
             patch('modules.content_moderator.AlibabaCloudModerator') as moderator:
            self.processor._moderate_content(self.task['id'], self.logger)
        get_task.assert_not_called()
        moderator.assert_not_called()
        self.processor._recover_cover_path.assert_not_called()

    def test_enabled_cover_uses_recovered_local_file_and_default_service(self):
        self.processor.config['CONTENT_MODERATION_COVER_ENABLED'] = True
        self.processor._recover_cover_path.return_value = 'downloads/cover-moderation/recovered.png'
        result = self.run_moderation()
        self.moderator.moderate_image.assert_called_once_with(
            'downloads/cover-moderation/recovered.png', service_type='baselineCheck')
        self.assertTrue(result['overall_pass'])
        self.assertEqual(result['cover'], {'pass': True, 'details': []})

    def test_cover_failure_requires_review_even_if_text_passes(self):
        self.processor.config.update(CONTENT_MODERATION_COVER_ENABLED=True,
                                     ALIYUN_IMAGE_MODERATION_SERVICE='customImageService')
        self.moderator.moderate_image.return_value = {
            'pass': False, 'details': [{'label': 'risk', 'suggestion': 'review', 'reason': 'cover risk'}]}
        result = self.run_moderation()
        self.assertFalse(result['overall_pass'])
        self.assertEqual(self.task['status'], tm.TASK_STATES['AWAITING_REVIEW'])
        self.assertEqual(self.task['error_category'], tm.CONTENT_MODERATION_ERROR_CATEGORY)
        self.assertEqual(result['cover']['details'][0]['reason'], 'cover risk')
        self.moderator.moderate_image.assert_called_once_with(
            self.task['cover_path_local'], service_type='customImageService')

    def test_missing_cover_is_submitted_to_fail_closed_validation(self):
        self.processor.config['CONTENT_MODERATION_COVER_ENABLED'] = True
        self.processor._recover_cover_path.return_value = ''
        self.moderator.moderate_image.return_value = {'pass': False, 'status': 'error', 'details': []}
        result = self.run_moderation()
        self.assertFalse(result['overall_pass'])
        self.moderator.moderate_image.assert_called_once_with('', service_type='baselineCheck')
        self.assertEqual(self.task['status'], tm.TASK_STATES['AWAITING_REVIEW'])

    def test_recovery_and_sdk_exceptions_require_review(self):
        self.processor.config['CONTENT_MODERATION_COVER_ENABLED'] = True
        for failing_step in (self.processor._recover_cover_path, self.moderator.moderate_image):
            with self.subTest(step=failing_step):
                failing_step.side_effect = OSError('unavailable')
                result = self.run_moderation()
                self.assertFalse(result['overall_pass'])
                self.assertEqual(result['cover']['status'], 'error')
                self.assertEqual(self.task['status'], tm.TASK_STATES['AWAITING_REVIEW'])
                failing_step.side_effect = None

    def test_missing_pass_in_cover_response_does_not_pass(self):
        self.processor.config['CONTENT_MODERATION_COVER_ENABLED'] = True
        self.moderator.moderate_image.return_value = {'details': []}
        self.assertFalse(self.run_moderation()['overall_pass'])


class HistoricalCoverModerationTests(unittest.TestCase):
    def setUp(self):
        self.task = {
            'id': 'historical-cover', 'status': tm.TASK_STATES['PENDING'],
            'youtube_url': 'https://www.youtube.com/watch?v=abcdefghijk',
            'upload_target': 'acfun', 'video_title_original': 'Title',
            'description_original': 'Description', 'tags_generated': '["tag"]',
            'cover_path_local': 'downloads/historical-cover/cover.jpg',
            tm.PIPELINE_CHECKPOINT_FIELD: json.dumps({'completed': [
                tm.PIPELINE_STAGE_FETCH_INFO, tm.PIPELINE_STAGE_MODERATE_CONTENT,
                tm.PIPELINE_STAGE_DOWNLOAD_VIDEO]}),
        }
        self.config = {
            'CONTENT_MODERATION_ENABLED': True, 'CONTENT_MODERATION_COVER_ENABLED': True,
            'AUTO_MODE_ENABLED': True, 'TRANSLATE_TITLE': False, 'TRANSLATE_DESCRIPTION': False,
            'GENERATE_TAGS': False, 'RECOMMEND_PARTITION': False,
            'SUBTITLE_TRANSLATION_ENABLED': False, 'SUBTITLE_EMBED_IN_VIDEO': False,
        }
        self.processor = tm.TaskProcessor.__new__(tm.TaskProcessor)
        self.processor.config = self.config
        self.processor._recover_cover_path = MagicMock(return_value=self.task['cover_path_local'])
        self.processor._ensure_required_translations_ready = MagicMock(return_value=True)
        self.processor._upload_to_acfun = MagicMock()
        self.processor._upload_to_bilibili = MagicMock()
        self.logger = MagicMock()
        self.moderator = MagicMock()
        self.moderator.moderate_text.return_value = {'pass': True, 'details': []}
        self.moderator.moderate_image.return_value = {'pass': True, 'status': 'passed', 'details': []}
        self.set_cover_result(None)
        for p in (
            patch.object(tm, 'get_task', side_effect=lambda _task_id: dict(self.task)),
            patch.object(tm, 'update_task', side_effect=self.update_task),
            patch('modules.content_moderator.AlibabaCloudModerator', return_value=self.moderator),
        ):
            p.start()
            self.addCleanup(p.stop)

    def update_task(self, _task_id, **values):
        self.task.update({key: value for key, value in values.items() if key != 'silent'})
        return True

    def set_cover_result(self, cover):
        result = {'overall_pass': True, 'title': {'pass': True, 'details': []}}
        if cover is not None:
            result['cover'] = cover
        self.task['moderation_result'] = json.dumps(result)

    def run_process(self):
        with patch.object(tm, 'setup_task_logger', return_value=self.logger), \
             patch.object(tm, '_raise_if_cancelled'), patch.object(tm, 'clear_task_cancel'), \
             patch('threading.Thread'):
            self.processor.process_task(self.task['id'], slot_already_acquired=True,
                                        acquired_task_semaphore=MagicMock())
        self.logger.error.assert_not_called()

    def test_legacy_and_malformed_results_invalidate_old_checkpoint(self):
        for cover in (None, {}, [], 'invalid', {'pass': True, 'details': []},
                      {'pass': True, 'status': 'disabled'}, {'status': 'passed'},
                      {'pass': 'true', 'status': 'passed'},
                      {'pass': False, 'status': 'passed'}, {'pass': True, 'status': 'rejected'},
                      {'pass': False, 'status': 'error'}, {'pass': False, 'status': 'unavailable'},
                      {'pass': False, 'status': 'invalid'}):
            with self.subTest(cover=cover):
                self.set_cover_result(cover)
                self.assertNotIn(tm.PIPELINE_STAGE_MODERATE_CONTENT,
                                 tm._get_completed_stages(self.task, self.config))
        for raw in ('not-json', '[]', 'null'):
            with self.subTest(raw=raw):
                self.task['moderation_result'] = raw
                self.assertNotIn(tm.PIPELINE_STAGE_MODERATE_CONTENT,
                                 tm._get_completed_stages(self.task, self.config))

    def test_completed_audits_and_disabled_switches_preserve_checkpoint(self):
        for status in ('passed', 'rejected'):
            with self.subTest(status=status):
                self.set_cover_result({'pass': status == 'passed', 'status': status})
                self.assertIn(tm.PIPELINE_STAGE_MODERATE_CONTENT,
                              tm._get_completed_stages(self.task, self.config))
        self.set_cover_result(None)
        for key in ('CONTENT_MODERATION_ENABLED', 'CONTENT_MODERATION_COVER_ENABLED'):
            with self.subTest(disabled=key):
                config = dict(self.config, **{key: False})
                self.assertIn(tm.PIPELINE_STAGE_MODERATE_CONTENT,
                              tm._get_completed_stages(self.task, config))

    def test_normal_resume_checks_cover_before_automatic_upload(self):
        self.set_cover_result({'pass': True, 'details': [
            {'label': 'skipped', 'suggestion': 'pass', 'reason': 'disabled'}]})
        order = MagicMock()
        order.attach_mock(self.moderator.moderate_image, 'audit')
        order.attach_mock(self.processor._upload_to_acfun, 'upload')
        self.run_process()
        self.assertEqual([call[0] for call in order.mock_calls], ['audit', 'upload'])
        self.assertEqual(json.loads(self.task['moderation_result'])['cover']['status'], 'passed')

    def test_normal_resume_stops_for_cover_review(self):
        self.moderator.moderate_image.return_value = {'pass': False, 'status': 'rejected', 'details': []}
        self.run_process()
        self.assertEqual(self.task['status'], tm.TASK_STATES['AWAITING_REVIEW'])
        self.processor._upload_to_acfun.assert_not_called()

    def test_upload_dispatch_checks_historical_cover_for_normal_and_force_paths(self):
        for forced in (False, True):
            for cover in (None, {'pass': True, 'status': 'disabled'}, {'pass': True, 'details': []}):
                with self.subTest(forced=forced, cover=cover):
                    self.set_cover_result(cover)
                    self.moderator.moderate_image.reset_mock()
                    self.processor._upload_to_acfun.reset_mock()
                    self.processor._upload_to_target(self.task['id'], self.logger,
                                                     allow_missing_translations=forced)
                    self.moderator.moderate_image.assert_called_once()
                    self.processor._upload_to_acfun.assert_called_once()

    def test_new_cover_failure_stops_even_explicit_force_upload(self):
        for status in ('rejected', 'error', 'unavailable', 'invalid'):
            with self.subTest(status=status):
                self.set_cover_result(None)
                self.moderator.moderate_image.return_value = {'pass': False, 'status': status, 'details': []}
                self.processor._upload_to_target(self.task['id'], self.logger,
                                                 allow_missing_translations=True)
                self.assertEqual(self.task['status'], tm.TASK_STATES['AWAITING_REVIEW'])
                self.processor._upload_to_acfun.assert_not_called()

    def test_incomplete_new_cover_response_cannot_reuse_old_overall_pass(self):
        self.moderator.moderate_image.return_value = {'pass': True, 'status': 'disabled', 'details': []}
        self.processor._upload_to_target(self.task['id'], self.logger, allow_missing_translations=True)
        self.assertEqual(self.task['status'], tm.TASK_STATES['AWAITING_REVIEW'])
        self.processor._upload_to_acfun.assert_not_called()

    def test_partial_platform_retry_checks_cover_and_skips_successful_platform(self):
        self.task.update(status=tm.TASK_STATES['FAILED'], upload_target='both',
                         acfun_upload_response='{"videoId": "already-uploaded"}')
        self.run_process()
        self.moderator.moderate_image.assert_called_once()
        self.processor._upload_to_acfun.assert_not_called()
        self.processor._upload_to_bilibili.assert_called_once()

    def test_failed_upload_batch_retry_checks_historical_cover(self):
        with tempfile.NamedTemporaryFile() as video:
            self.task.update(status=tm.TASK_STATES['FAILED'], video_path_local=video.name,
                             error_message='upload failed')
            with patch.object(tm, 'get_tasks_by_status', return_value=[dict(self.task)]), \
                 patch.object(tm, '_start_background_upload_retry', side_effect=lambda task_id, _config:
                              self.processor._upload_to_target(task_id, self.logger,
                                                               allow_missing_translations=True)):
                result = tm.retry_failed_tasks(self.config)
        self.assertEqual(result['scheduled'], 1)
        self.moderator.moderate_image.assert_called_once()
        self.processor._upload_to_acfun.assert_called_once()

    def test_current_cover_result_preserves_explicit_manual_override(self):
        for status in ('passed', 'rejected'):
            with self.subTest(status=status):
                self.set_cover_result({'pass': status == 'passed', 'status': status, 'details': []})
                self.processor._upload_to_acfun.reset_mock()
                self.processor._upload_to_target(self.task['id'], self.logger,
                                                 allow_missing_translations=True)
                self.moderator.moderate_image.assert_not_called()
                self.processor._upload_to_acfun.assert_called_once()

    def test_passing_backfill_preserves_text_rejection_override_only_for_force_upload(self):
        self.moderator.moderate_text.return_value = {'pass': False, 'details': []}
        for forced in (False, True):
            with self.subTest(forced=forced):
                self.task['moderation_result'] = json.dumps({
                    'overall_pass': False, 'title': {'pass': False, 'details': []},
                    'cover': {'pass': True, 'status': 'disabled', 'details': []}})
                self.moderator.moderate_image.reset_mock()
                self.processor._upload_to_acfun.reset_mock()
                self.processor._upload_to_target(self.task['id'], self.logger,
                                                 allow_missing_translations=forced)
                self.moderator.moderate_image.assert_called_once()
                result = json.loads(self.task['moderation_result'])
                self.assertFalse(result['overall_pass'])
                self.assertEqual(result['cover']['status'], 'passed')
                self.assertEqual(self.processor._upload_to_acfun.called, forced)

    def test_failed_attempt_is_retried_before_explicit_force_upload(self):
        for status in ('error', 'unavailable', 'invalid'):
            with self.subTest(status=status):
                self.set_cover_result({'pass': False, 'status': status, 'details': []})
                self.moderator.moderate_image.reset_mock()
                self.processor._upload_to_acfun.reset_mock()
                self.processor._upload_to_target(self.task['id'], self.logger,
                                                 allow_missing_translations=True)
                self.moderator.moderate_image.assert_called_once()
                self.processor._upload_to_acfun.assert_called_once()

    def test_current_rejected_result_blocks_automatic_upload_without_reauditing(self):
        for overall_pass in (False, True):
            with self.subTest(overall_pass=overall_pass):
                self.task['moderation_result'] = json.dumps({
                    'overall_pass': overall_pass,
                    'cover': {'pass': False, 'status': 'rejected', 'details': []}})
                self.processor._upload_to_target(self.task['id'], self.logger)
                self.assertEqual(self.task['status'], tm.TASK_STATES['AWAITING_REVIEW'])
                self.moderator.moderate_image.assert_not_called()
                self.processor._upload_to_acfun.assert_not_called()

    def test_disabled_switch_does_not_upload_cover_for_historical_tasks(self):
        self.config['CONTENT_MODERATION_COVER_ENABLED'] = False
        self.processor._upload_to_target(self.task['id'], self.logger)
        self.moderator.moderate_image.assert_not_called()
        self.processor._upload_to_acfun.assert_called_once()

    def test_fully_uploaded_task_is_not_audited_or_reuploaded(self):
        self.task.update(status=tm.TASK_STATES['COMPLETED'], acfun_upload_response='{"videoId": "done"}')
        self.processor._upload_to_target(self.task['id'], self.logger)
        self.moderator.moderate_image.assert_not_called()
        self.processor._upload_to_acfun.assert_not_called()


class CoverModerationSettingsTests(unittest.TestCase):
    def test_defaults_keep_cover_disabled(self):
        self.assertIs(cm.DEFAULT_CONFIG['CONTENT_MODERATION_COVER_ENABLED'], False)
        self.assertEqual(cm.DEFAULT_CONFIG['ALIYUN_IMAGE_MODERATION_SERVICE'], 'baselineCheck')

    def test_settings_save_can_enable_then_uncheck_cover_option(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(cm, 'get_app_subdir', side_effect=lambda name: os.path.join(tmp, name)), \
             patch.object(web_app, 'configure_app'), \
             patch.object(web_app, '_sync_notification_service'), \
             patch('modules.task_manager.get_global_task_processor', return_value=None), \
             patch.object(web_app.youtube_monitor, 'reload_api_client', return_value=(False, 'missing_api_key')), \
             patch.object(web_app.youtube_monitor, 'start_all_schedules'), \
             patch.object(web_app.youtube_monitor, 'stop_all_schedules'):
            enabled = web_app._perform_settings_save({
                'CONTENT_MODERATION_ENABLED': 'on',
                'CONTENT_MODERATION_COVER_ENABLED': 'on',
                'ALIYUN_IMAGE_MODERATION_SERVICE': 'customImageService',
            }, {})
            self.assertTrue(enabled['success'])
            self.assertIs(cm.load_config()['CONTENT_MODERATION_COVER_ENABLED'], True)
            self.assertEqual(cm.load_config()['ALIYUN_IMAGE_MODERATION_SERVICE'], 'customImageService')
            disabled = web_app._perform_settings_save({'CONTENT_MODERATION_ENABLED': 'on'}, {})
            self.assertTrue(disabled['success'])
            saved = cm.load_config()
            self.assertIs(saved['CONTENT_MODERATION_COVER_ENABLED'], False)
            self.assertIs(saved['CONTENT_MODERATION_ENABLED'], True)

    def test_review_templates_show_cover_failure_and_allow_legacy_results(self):
        result = {'overall_pass': False, 'title': {'pass': True, 'details': []},
                  'description': {'pass': True, 'details': []},
                  'cover': {'pass': False, 'details': [
                      {'label': 'error', 'suggestion': 'review', 'reason': 'cover-service-unavailable'}]}}
        task = {'id': 'cover-review', 'status': tm.TASK_STATES['AWAITING_REVIEW'],
                'video_title_original': 'Title'}
        for template in ('manual_review.html', 'edit_task.html'):
            with self.subTest(template=template), web_app.app.test_request_context('/'):
                task['moderation_result'] = json.dumps(result)
                page = render_template(template, task=task, tasks=[task], config=cm.DEFAULT_CONFIG)
                self.assertIn('cover-service-unavailable', page)
                self.assertIn('封面问题:', page)
                legacy = {key: value for key, value in result.items() if key != 'cover'}
                task['moderation_result'] = json.dumps(legacy)
                legacy_page = render_template(template, task=task, tasks=[task], config=cm.DEFAULT_CONFIG)
                self.assertNotIn('封面问题:', legacy_page)
                result['cover'] = {'pass': True, 'status': 'disabled', 'details': [
                    {'label': 'skipped', 'suggestion': 'pass', 'reason': 'disabled'}]}
                task['moderation_result'] = json.dumps(result)
                disabled_page = render_template(template, task=task, tasks=[task], config=cm.DEFAULT_CONFIG)
                self.assertNotIn('封面问题:', disabled_page)
                result['cover'] = {'pass': False, 'details': [
                    {'label': 'error', 'suggestion': 'review', 'reason': 'cover-service-unavailable'}]}


if __name__ == '__main__':
    unittest.main()
