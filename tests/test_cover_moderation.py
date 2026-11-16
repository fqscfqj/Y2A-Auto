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
