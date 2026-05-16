import ast
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock


def _safe_join(base, *paths):
    candidate = os.path.join(base, *paths)
    candidate_real = os.path.realpath(candidate)
    base_real = os.path.realpath(base)
    try:
        if os.path.commonpath([base_real, candidate_real]) != base_real:
            return None
    except ValueError:
        return None
    return candidate_real


def _load_delete_helpers(downloads_dir):
    task_manager_path = pathlib.Path(__file__).resolve().parents[1] / "modules" / "task_manager.py"
    source = task_manager_path.read_text(encoding="utf-8")
    module_ast = ast.parse(source, filename=str(task_manager_path))
    selected = [
        node for node in module_ast.body
        if isinstance(node, ast.FunctionDef) and node.name in {
            '_get_task_download_dir_real',
            'delete_task_files',
        }
    ]
    isolated_module = ast.Module(body=selected, type_ignores=[])
    logger = MagicMock()
    namespace = {
        'os': os,
        'uuid': __import__('uuid'),
        'shutil': shutil,
        'safe_join': _safe_join,
        'DOWNLOADS_DIR': downloads_dir,
        'logger': logger,
    }
    exec(compile(isolated_module, str(task_manager_path), 'exec'), namespace)
    return namespace['_get_task_download_dir_real'], namespace['delete_task_files'], logger


class DeleteTaskFilesTests(unittest.TestCase):
    TASK_ID = '12345678-1234-5678-1234-567812345678'

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.downloads_dir = os.path.join(self.tmpdir, 'downloads')
        os.makedirs(self.downloads_dir, exist_ok=True)
        (
            self._get_task_download_dir_real,
            self.delete_task_files,
            self.mock_logger,
        ) = _load_delete_helpers(self.downloads_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_get_task_download_dir_real_returns_canonical_downloads_path(self):
        task_dir = self._get_task_download_dir_real(self.TASK_ID)
        expected = os.path.realpath(os.path.join(self.downloads_dir, self.TASK_ID))
        self.assertEqual(task_dir, expected)

    def test_delete_task_files_rejects_invalid_uuid(self):
        self.assertFalse(self.delete_task_files('../../etc/passwd'))
        self.mock_logger.error.assert_called()

    def test_delete_task_files_removes_existing_task_directory(self):
        task_dir = os.path.join(self.downloads_dir, self.TASK_ID)
        os.makedirs(task_dir, exist_ok=True)
        with open(os.path.join(task_dir, 'metadata.json'), 'w', encoding='utf-8') as fh:
            fh.write('{}')

        result = self.delete_task_files(self.TASK_ID)

        self.assertTrue(result)
        self.assertFalse(os.path.exists(task_dir))


if __name__ == '__main__':
    unittest.main()
