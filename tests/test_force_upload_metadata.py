import ast
import pathlib
import unittest
from unittest.mock import MagicMock, patch

from modules import task_manager as tm


def _load_app_partition_helper():
    app_path = pathlib.Path(__file__).resolve().parents[1] / 'app.py'
    source = app_path.read_text(encoding='utf-8')
    tree = ast.parse(source, filename=str(app_path))
    selected = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == '_missing_upload_partition_labels'
    ]
    isolated = ast.Module(body=selected, type_ignores=[])
    namespace = {}
    exec(compile(isolated, str(app_path), 'exec'), namespace)
    return namespace['_missing_upload_partition_labels']


class ForceUploadMetadataTests(unittest.TestCase):
    def test_normalize_tags_list_handles_none_and_invalid_json(self):
        self.assertEqual(tm._normalize_tags_list(None), [])
        self.assertEqual(tm._normalize_tags_list(''), [])
        self.assertEqual(tm._normalize_tags_list('not-json'), [])
        self.assertEqual(tm._normalize_tags_list('["asmr", "", null, "game"]'), ['asmr', 'game'])

    def test_force_upload_generates_missing_tags_and_partitions(self):
        task_id = 'task-force-upload'
        task = {
            'id': task_id,
            'upload_target': 'both',
            'video_title_original': 'Matching game in ASMR',
            'description_original': 'Ys: 45\nTo: 90',
            'video_title_translated': 'ASMR中的配对游戏',
            'description_translated': '',
            'tags_generated': None,
            'selected_partition_id_acfun': '',
            'recommended_partition_id_acfun': '',
            'selected_partition_id_bilibili': '',
            'recommended_partition_id_bilibili': '',
        }

        def fake_get_task(_task_id):
            self.assertEqual(_task_id, task_id)
            return dict(task)

        def fake_update_task(_task_id, **kwargs):
            self.assertEqual(_task_id, task_id)
            task.update({k: v for k, v in kwargs.items() if k != 'silent'})
            return True

        processor = tm.TaskProcessor({
            'GENERATE_TAGS': True,
            'RECOMMEND_PARTITION': True,
        })
        processor._generate_tags = MagicMock(side_effect=lambda *_args: task.update({
            'tags_generated': '["ASMR", "配对游戏"]'
        }) or True)
        processor._recommend_partition = MagicMock(side_effect=lambda *_args: task.update({
            'recommended_partition_id_acfun': '1001',
            'selected_partition_id_acfun': '1001',
            'recommended_partition_id_bilibili': '2001',
            'selected_partition_id_bilibili': '2001',
        }) or True)

        with patch.object(tm, 'get_task', side_effect=fake_get_task), \
             patch.object(tm, 'update_task', side_effect=fake_update_task):
            result = processor._ensure_force_upload_metadata_ready(task_id, MagicMock())

        self.assertEqual(result['tags_generated'], '["ASMR", "配对游戏"]')
        self.assertEqual(result['selected_partition_id_acfun'], '1001')
        self.assertEqual(result['selected_partition_id_bilibili'], '2001')
        processor._generate_tags.assert_called_once()
        processor._recommend_partition.assert_called_once()

    def test_force_upload_respects_existing_tags_and_partitions(self):
        task_id = 'task-existing-metadata'
        task = {
            'id': task_id,
            'upload_target': 'both',
            'video_title_original': 'Original title',
            'description_original': 'Original description',
            'video_title_translated': '',
            'description_translated': '',
            'tags_generated': '["手动标签"]',
            'selected_partition_id_acfun': '1001',
            'recommended_partition_id_acfun': '',
            'selected_partition_id_bilibili': '2001',
            'recommended_partition_id_bilibili': '',
        }

        processor = tm.TaskProcessor({
            'GENERATE_TAGS': True,
            'RECOMMEND_PARTITION': True,
        })
        processor._generate_tags = MagicMock()
        processor._recommend_partition = MagicMock()

        with patch.object(tm, 'get_task', return_value=dict(task)):
            result = processor._ensure_force_upload_metadata_ready(task_id, MagicMock())

        self.assertEqual(result['tags_generated'], '["手动标签"]')
        self.assertEqual(result['selected_partition_id_acfun'], '1001')
        self.assertEqual(result['selected_partition_id_bilibili'], '2001')
        processor._generate_tags.assert_not_called()
        processor._recommend_partition.assert_not_called()

    def test_partition_precheck_blocks_only_when_no_recommendation_or_fixed_partition(self):
        missing_upload_partition_labels = _load_app_partition_helper()
        task = {
            'upload_target': 'both',
            'selected_partition_id_acfun': '',
            'recommended_partition_id_acfun': '',
            'selected_partition_id_bilibili': '',
            'recommended_partition_id_bilibili': '',
        }

        self.assertEqual(
            missing_upload_partition_labels(task, {'RECOMMEND_PARTITION': 'false'}),
            ['AcFun 分区', 'bilibili 分区'],
        )
        self.assertEqual(
            missing_upload_partition_labels(task, {'RECOMMEND_PARTITION': True}),
            [],
        )
        self.assertEqual(
            missing_upload_partition_labels(task, {
                'RECOMMEND_PARTITION': False,
                'FIXED_PARTITION_ID': '1001',
                'FIXED_PARTITION_ID_BILIBILI': '2001',
            }),
            [],
        )


if __name__ == '__main__':
    unittest.main()
