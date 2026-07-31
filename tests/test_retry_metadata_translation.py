import json
import pathlib
import unittest
from unittest.mock import patch

import app as web_app
from modules import task_manager as tm


class RetryMetadataTranslationTests(unittest.TestCase):
    def test_retryable_only_accepts_translation_failures_in_manual_review(self):
        self.assertTrue(tm.is_metadata_translation_retryable({
            'status': tm.TASK_STATES['AWAITING_REVIEW'],
            'error_category': tm.METADATA_TRANSLATION_ERROR_CATEGORY,
            'error_message': '任意展示文案均不影响重试资格。',
        }))

        self.assertFalse(tm.is_metadata_translation_retryable({
            'status': tm.TASK_STATES['FAILED'],
            'error_category': tm.METADATA_TRANSLATION_ERROR_CATEGORY,
        }))
        self.assertFalse(tm.is_metadata_translation_retryable({
            'status': tm.TASK_STATES['AWAITING_REVIEW'],
            'error_category': 'content_moderation_failed',
        }))

    def test_retry_requires_enabled_translation_and_api_key(self):
        task = {
            'video_title_original': 'Original title',
            'description_original': 'Original description',
        }
        self.assertEqual(
            tm.get_metadata_translation_retry_block_reason(task, {
                'TRANSLATE_TITLE': False,
                'TRANSLATE_DESCRIPTION': False,
                'OPENAI_API_KEY': 'key',
            }),
            '当前未启用可用的标题或简介自动翻译，无法重新翻译。',
        )
        self.assertEqual(
            tm.get_metadata_translation_retry_block_reason(task, {'TRANSLATE_TITLE': True}),
            '未配置 OpenAI API Key，无法重新翻译。',
        )
        self.assertIsNone(tm.get_metadata_translation_retry_block_reason(task, {
            'TRANSLATE_TITLE': True,
            'OPENAI_API_KEY': 'key',
        }))

    def test_retry_resets_translation_and_downstream_stages_before_scheduling(self):
        task_id = 'retry-translation-success'
        task = {
            'id': task_id,
            'status': tm.TASK_STATES['AWAITING_REVIEW'],
            'video_title_original': 'Original title',
            'description_original': 'Original description',
            'video_title_translated': '旧标题',
            'description_translated': '旧简介',
            'tags_generated': '["旧标签"]',
            'recommended_partition_id': '1',
            'recommended_partition_id_acfun': '2',
            'recommended_partition_id_bilibili': '3',
            'selected_partition_id': '4',
            'selected_partition_id_acfun': '5',
            'selected_partition_id_bilibili': '6',
            'moderation_result': '{"overall_pass": false}',
            'error_message': '自动翻译失败，简介：简介格式不自然。任务已转入人工审核。',
            'error_category': tm.METADATA_TRANSLATION_ERROR_CATEGORY,
            'upload_progress': '旧进度',
            'pipeline_checkpoint': json.dumps({
                'version': 1,
                'completed': [
                    tm.PIPELINE_STAGE_FETCH_INFO,
                    tm.PIPELINE_STAGE_TRANSLATE_CONTENT,
                    tm.PIPELINE_STAGE_GENERATE_TAGS,
                    tm.PIPELINE_STAGE_RECOMMEND_PARTITION,
                    tm.PIPELINE_STAGE_MODERATE_CONTENT,
                ],
            }),
        }

        def fake_update_task(_task_id, **kwargs):
            self.assertEqual(_task_id, task_id)
            task.update({key: value for key, value in kwargs.items() if key != 'silent'})
            return True

        with patch.object(tm, 'get_task', return_value=task), \
             patch.object(tm, 'update_task', side_effect=fake_update_task), \
             patch.object(tm, 'start_task', return_value=True) as start_task_mock:
            result = tm.retry_metadata_translation_task(task_id, {
                'TRANSLATE_DESCRIPTION': True,
                'OPENAI_API_KEY': 'key',
            })

        self.assertTrue(result)
        self.assertEqual(task['status'], tm.TASK_STATES['PENDING'])
        self.assertEqual(task['video_title_translated'], '')
        self.assertEqual(task['description_translated'], '')
        self.assertIsNone(task['tags_generated'])
        self.assertIsNone(task['moderation_result'])
        self.assertIsNone(task['error_message'])
        self.assertIsNone(task['error_category'])
        self.assertIsNone(task['upload_progress'])
        self.assertIsNone(task['recommended_partition_id_acfun'])
        self.assertIsNone(task['recommended_partition_id_bilibili'])
        self.assertIsNone(task['selected_partition_id'])
        self.assertIsNone(task['selected_partition_id_acfun'])
        self.assertIsNone(task['selected_partition_id_bilibili'])
        checkpoint = json.loads(task['pipeline_checkpoint'])
        self.assertEqual(checkpoint['completed'], [tm.PIPELINE_STAGE_FETCH_INFO])
        self.assertNotIn(tm.PIPELINE_STAGE_TRANSLATE_CONTENT, tm._get_completed_stages(task))
        start_task_mock.assert_called_once_with(task_id, {
            'TRANSLATE_DESCRIPTION': True,
            'OPENAI_API_KEY': 'key',
        })

    def test_retry_rejects_non_translation_manual_review_task(self):
        task = {
            'id': 'moderation-review',
            'status': tm.TASK_STATES['AWAITING_REVIEW'],
            'error_category': 'content_moderation_failed',
        }

        with patch.object(tm, 'get_task', return_value=task), \
             patch.object(tm, 'update_task') as update_task_mock, \
             patch.object(tm, 'start_task') as start_task_mock:
            result = tm.retry_metadata_translation_task(task['id'], {})

        self.assertFalse(result)
        update_task_mock.assert_not_called()
        start_task_mock.assert_not_called()

    def test_retry_rolls_back_when_scheduling_fails(self):
        task_id = 'retry-translation-rollback'
        original = {
            'id': task_id,
            'status': tm.TASK_STATES['AWAITING_REVIEW'],
            'video_title_translated': '旧标题',
            'description_translated': '旧简介',
            'tags_generated': '["旧标签"]',
            'recommended_partition_id': '1',
            'recommended_partition_id_acfun': '2',
            'recommended_partition_id_bilibili': '3',
            'selected_partition_id': '4',
            'selected_partition_id_acfun': '5',
            'selected_partition_id_bilibili': '6',
            'moderation_result': '{"overall_pass": false}',
            'error_message': '自动翻译失败，简介：简介格式不自然。任务已转入人工审核。',
            'error_category': tm.METADATA_TRANSLATION_ERROR_CATEGORY,
            'pipeline_checkpoint': '{"version": 1, "completed": ["fetch_info"]}',
            'upload_progress': '旧进度',
        }
        task = dict(original)

        def fake_update_task(_task_id, **kwargs):
            self.assertEqual(_task_id, task_id)
            task.update({key: value for key, value in kwargs.items() if key != 'silent'})
            return True

        with patch.object(tm, 'get_task', return_value=task), \
             patch.object(tm, 'update_task', side_effect=fake_update_task), \
             patch.object(tm, 'start_task', return_value=False):
            result = tm.retry_metadata_translation_task(task_id, {
                'TRANSLATE_TITLE': True,
                'OPENAI_API_KEY': 'key',
            })

        self.assertFalse(result)
        for key, value in original.items():
            self.assertEqual(task.get(key), value, key)

    def test_retry_translation_route_schedules_eligible_task(self):
        task = {
            'id': 'route-success',
            'status': tm.TASK_STATES['AWAITING_REVIEW'],
            'error_category': tm.METADATA_TRANSLATION_ERROR_CATEGORY,
            'video_title_original': 'Original title',
        }
        config = {
            'password_protection_enabled': False,
            'TRANSLATE_TITLE': True,
            'OPENAI_API_KEY': 'key',
        }
        with patch.object(web_app, 'get_task', return_value=task), \
             patch.object(web_app, 'load_config', return_value=config), \
             patch.object(web_app, 'retry_metadata_translation_task', return_value=True) as retry_mock:
            response = web_app.app.test_client().post('/tasks/route-success/retry_translation')

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/manual_review'))
        retry_mock.assert_called_once_with('route-success', config)

    def test_retry_translation_route_rejects_invalid_current_config(self):
        task = {
            'id': 'route-disabled',
            'status': tm.TASK_STATES['AWAITING_REVIEW'],
            'error_category': tm.METADATA_TRANSLATION_ERROR_CATEGORY,
            'video_title_original': 'Original title',
        }
        with patch.object(web_app, 'get_task', return_value=task), \
             patch.object(web_app, 'load_config', return_value={'password_protection_enabled': False}), \
             patch.object(web_app, 'retry_metadata_translation_task') as retry_mock:
            response = web_app.app.test_client().post('/tasks/route-disabled/retry_translation')

        self.assertEqual(response.status_code, 302)
        retry_mock.assert_not_called()

    def test_manual_review_template_includes_server_side_eligibility_and_data_loss_warning(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        template = (root / 'templates' / 'manual_review.html').read_text(encoding='utf-8')
        app_source = (root / 'app.py').read_text(encoding='utf-8')

        self.assertIn('{% if task.can_retry_translation %}', template)
        self.assertIn('id="retryTranslationForm" method="post"', template)
        self.assertIn('已手动修改的标题、简介、标签和分区选择将被清空并重新生成。', template)
        self.assertIn("@app.route('/tasks/<task_id>/retry_translation', methods=['POST'])", app_source)
        self.assertIn('if not is_metadata_translation_retryable(task):', app_source)


if __name__ == '__main__':
    unittest.main()
