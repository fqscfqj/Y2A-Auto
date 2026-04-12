"""
Unit tests for app._replace_task_cover().

Covers two behaviour branches introduced by the PR:
  1. No pre-existing cover → must not raise; writes custom_cover.*; does NOT
     create an original_cover.* backup.
  2. Pre-existing cover → backs it up as original_cover.*; writes new
     custom_cover.*; calls update_task with the new path.
"""

import ast
import io
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_cover_helpers(downloads_dir, mock_update_task):
    """Extract _replace_task_cover and its helper functions from app.py into
    an isolated execution namespace so that importing the full Flask app is
    not required."""
    import uuid
    from PIL import Image, UnidentifiedImageError
    from werkzeug.security import safe_join

    app_path = pathlib.Path(__file__).resolve().parents[1] / "app.py"
    source = app_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(app_path))

    function_names = {
        '_get_task_dir_real',
        '_safe_join_task_dir',
        '_get_cover_file_info',
        '_validate_cover_upload',
        '_find_original_cover_backup',
        '_get_current_cover_path',
        '_replace_task_cover',
    }
    variable_names = {'ALLOWED_COVER_EXTENSIONS'}

    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in function_names:
            selected.append(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in variable_names:
                    selected.append(node)

    isolated = ast.Module(body=selected, type_ignores=[])
    namespace = {
        'os': os,
        'shutil': shutil,
        'uuid': uuid,
        'Image': Image,
        'UnidentifiedImageError': UnidentifiedImageError,
        'safe_join': safe_join,
        # Redirect get_app_subdir('downloads') to the temp directory
        'get_app_subdir': lambda _: downloads_dir,
        'update_task': mock_update_task,
    }
    exec(compile(isolated, str(app_path), "exec"), namespace)
    return namespace


def _make_fake_file_storage(filename='upload.png'):
    """Return a mock FileStorage-like object backed by a tiny valid PNG."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (1, 1), color=(200, 100, 50)).save(buf, format='PNG')
    buf.seek(0)

    mock_fs = MagicMock()
    mock_fs.filename = filename
    mock_fs.stream = buf

    def _save(dest_path):
        with open(dest_path, 'wb') as fh:
            fh.write(buf.getvalue())

    mock_fs.save.side_effect = _save
    return mock_fs


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class ReplaceTaskCoverTests(unittest.TestCase):
    """Tests for app._replace_task_cover()."""

    TASK_ID = '12345678-1234-5678-1234-567812345678'

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.downloads_dir = os.path.join(self.tmpdir, 'downloads')
        os.makedirs(self.downloads_dir)
        self.mock_update_task = MagicMock()
        ns = _load_cover_helpers(self.downloads_dir, self.mock_update_task)
        self._replace_task_cover = ns['_replace_task_cover']

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _task_dir(self):
        return os.path.realpath(os.path.join(self.downloads_dir, self.TASK_ID))

    # ------------------------------------------------------------------
    # Branch 1: no pre-existing cover
    # ------------------------------------------------------------------

    def test_no_existing_cover_does_not_raise(self):
        """_replace_task_cover() must not raise when the task has no cover."""
        task = {'id': self.TASK_ID}
        uploaded = _make_fake_file_storage('photo.png')
        # Should complete without exception
        self._replace_task_cover(task, uploaded)

    def test_no_existing_cover_writes_custom_cover(self):
        """_replace_task_cover() must save the upload as custom_cover.* when
        no original cover is present."""
        task = {'id': self.TASK_ID}
        uploaded = _make_fake_file_storage('photo.png')

        result = self._replace_task_cover(task, uploaded)

        expected = os.path.realpath(os.path.join(self._task_dir(), 'custom_cover.png'))
        self.assertEqual(result, expected)
        self.assertTrue(os.path.isfile(expected))

    def test_no_existing_cover_does_not_create_backup(self):
        """No original_cover.* file should appear when there was no pre-existing cover."""
        task = {'id': self.TASK_ID}
        uploaded = _make_fake_file_storage('photo.png')

        self._replace_task_cover(task, uploaded)

        task_dir = self._task_dir()
        for ext in ('.jpg', '.jpeg', '.png', '.webp'):
            self.assertFalse(
                os.path.isfile(os.path.join(task_dir, f'original_cover{ext}')),
                f'Unexpected backup file original_cover{ext} was created',
            )

    def test_no_existing_cover_calls_update_task(self):
        """update_task() must be called once with the new cover path and silent=True."""
        task = {'id': self.TASK_ID}
        uploaded = _make_fake_file_storage('photo.png')

        result = self._replace_task_cover(task, uploaded)

        self.mock_update_task.assert_called_once_with(
            self.TASK_ID,
            cover_path_local=result,
            silent=True,
        )

    # ------------------------------------------------------------------
    # Branch 2: pre-existing cover is present
    # ------------------------------------------------------------------

    def _create_existing_cover(self, filename='cover.png', content=b'original-cover'):
        """Write a dummy cover file into the task directory and return its path."""
        task_dir = self._task_dir()
        os.makedirs(task_dir, exist_ok=True)
        cover_path = os.path.join(task_dir, filename)
        with open(cover_path, 'wb') as fh:
            fh.write(content)
        return cover_path

    def test_existing_cover_creates_original_backup(self):
        """When a cover already exists it must be backed up as original_cover.*"""
        original_content = b'original-cover-bytes'
        existing_cover = self._create_existing_cover('cover.png', original_content)

        task = {'id': self.TASK_ID, 'cover_path_local': existing_cover}
        uploaded = _make_fake_file_storage('new.png')

        self._replace_task_cover(task, uploaded)

        backup = os.path.realpath(os.path.join(self._task_dir(), 'original_cover.png'))
        self.assertTrue(os.path.isfile(backup), 'original_cover.png backup was not created')
        with open(backup, 'rb') as fh:
            self.assertEqual(fh.read(), original_content,
                             'Backup content does not match the original cover')

    def test_existing_cover_writes_new_custom_cover(self):
        """custom_cover.* should be created (or replaced) after uploading."""
        existing_cover = self._create_existing_cover('cover.png')

        task = {'id': self.TASK_ID, 'cover_path_local': existing_cover}
        uploaded = _make_fake_file_storage('new.png')

        result = self._replace_task_cover(task, uploaded)

        expected = os.path.realpath(os.path.join(self._task_dir(), 'custom_cover.png'))
        self.assertEqual(result, expected)
        self.assertTrue(os.path.isfile(expected))

    def test_existing_cover_calls_update_task(self):
        """update_task() must be called once with the new cover path and silent=True."""
        existing_cover = self._create_existing_cover('cover.png')

        task = {'id': self.TASK_ID, 'cover_path_local': existing_cover}
        uploaded = _make_fake_file_storage('new.png')

        result = self._replace_task_cover(task, uploaded)

        self.mock_update_task.assert_called_once_with(
            self.TASK_ID,
            cover_path_local=result,
            silent=True,
        )

    def test_existing_custom_cover_is_replaced_not_duplicated(self):
        """A stale custom_cover.* from a previous upload must be removed before
        the new one is written — only one custom_cover.* file should exist."""
        task_dir = self._task_dir()
        os.makedirs(task_dir, exist_ok=True)

        # Simulate a leftover custom cover from a previous run
        stale = os.path.join(task_dir, 'custom_cover.png')
        with open(stale, 'wb') as fh:
            fh.write(b'stale')

        task = {'id': self.TASK_ID}
        uploaded = _make_fake_file_storage('fresh.png')

        self._replace_task_cover(task, uploaded)

        custom_covers = [
            name for name in os.listdir(task_dir)
            if name.startswith('custom_cover.')
        ]
        self.assertEqual(len(custom_covers), 1,
                         f'Expected exactly one custom_cover.* file, found: {custom_covers}')


if __name__ == '__main__':
    unittest.main()
