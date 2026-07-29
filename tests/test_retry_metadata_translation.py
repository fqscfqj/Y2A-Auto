import json
import pathlib
import unittest
from unittest.mock import patch

from modules import task_manager as tm


class RetryMetadataTranslationTests(unittest.TestCase):
    def test_retryable_only_accepts_translation_failures_in_manual_review(self):
        retryable_messages = (
            '自动翻译失败，简介：简介格式不自然。任务已转入人工审核。',
            '自动翻译未完成：简介仍缺少有效译文，任务已转入人工审核。',
        )
        for message in retryable_messages:
            with self.subTest(message=message):
                self.assertTrue(tm.is_metadata_translation_retryable({
                    'status': tm.TASK_STATES['AWAITING_REVIEW'],
                    'error_message': message,
                }))

        self.assertFalse(tm.is_metadata_translation_retryable({
            'status': tm.TASK_STATES['FAILED'],
            'error_message': retryable_messages[0],
        }))
        self.assertFalse(tm.is_metadata_translation_retryable({
            'status': tm.TASK_STATES['AWAITING_REVIEW'],
            'error_message': '内容审核不通过，请人工复审。',
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
            result = tm.retry_metadata_translation_task(task_id, {'TRANSLATE_DESCRIPTION': True})

        self.assertTrue(result)
        self.assertEqual(task['status'], tm.TASK_STATES['PENDING'])
        self.assertEqual(task['video_title_translated'], '')
        self.assertEqual(task['description_translated'], '')
        self.assertIsNone(task['tags_generated'])
        self.assertIsNone(task['moderation_result'])
        self.assertIsNone(task['error_message'])
        self.assertIsNone(task['upload_progress'])
        self.assertIsNone(task['recommended_partition_id_acfun'])
        self.assertIsNone(task['recommended_partition_id_bilibili'])
        self.assertIsNone(task['selected_partition_id'])
        self.assertIsNone(task['selected_partition_id_acfun'])
        self.assertIsNone(task['selected_partition_id_bilibili'])
        checkpoint = json.loads(task['pipeline_checkpoint'])
        self.assertEqual(checkpoint['completed'], [tm.PIPELINE_STAGE_FETCH_INFO])
        self.assertNotIn(tm.PIPELINE_STAGE_TRANSLATE_CONTENT, tm._get_completed_stages(task))
        start_task_mock.assert_called_once_with(task_id, {'TRANSLATE_DESCRIPTION': True})

    def test_retry_rejects_non_translation_manual_review_task(self):
        task = {
            'id': 'moderation-review',
            'status': tm.TASK_STATES['AWAITING_REVIEW'],
            'error_message': '内容审核不通过，请人工复审。',
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
            result = tm.retry_metadata_translation_task(task_id, {})

        self.assertFalse(result)
        for key, value in original.items():
            self.assertEqual(task.get(key), value, key)

    def test_manual_review_uses_post_form_and_server_side_eligibility_check(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        template = (root / 'templates' / 'manual_review.html').read_text(encoding='utf-8')
        app_source = (root / 'app.py').read_text(encoding='utf-8')

        self.assertIn('{% if task.can_retry_translation %}', template)
        self.assertIn('id="retryTranslationForm" method="post"', template)
        self.assertIn("@app.route('/tasks/<task_id>/retry_translation', methods=['POST'])", app_source)
        self.assertIn('if not is_metadata_translation_retryable(task):', app_source)


if __name__ == '__main__':
    unittest.main()
