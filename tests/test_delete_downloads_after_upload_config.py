import pathlib
import unittest

from modules.config_manager import DEFAULT_CONFIG


ROOT = pathlib.Path(__file__).resolve().parents[1]


class DeleteDownloadsAfterUploadConfigTests(unittest.TestCase):
    def test_option_is_registered_and_disabled_by_default(self):
        self.assertIn('DELETE_DOWNLOAD_FILES_AFTER_UPLOAD', DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG['DELETE_DOWNLOAD_FILES_AFTER_UPLOAD'])

    def test_settings_page_exposes_option_and_save_handler_tracks_checkbox(self):
        template = (ROOT / 'templates' / 'settings.html').read_text(encoding='utf-8')
        app_source = (ROOT / 'app.py').read_text(encoding='utf-8')

        self.assertIn('name="DELETE_DOWNLOAD_FILES_AFTER_UPLOAD"', template)
        self.assertIn("'DELETE_DOWNLOAD_FILES_AFTER_UPLOAD'", app_source)

    def test_upload_cleanup_is_guarded_by_option_for_both_platform_handlers(self):
        source = (ROOT / 'modules' / 'task_manager.py').read_text(encoding='utf-8')
        option_guard = "self.config.get('DELETE_DOWNLOAD_FILES_AFTER_UPLOAD', False)"

        self.assertEqual(source.count(option_guard), 2)
        self.assertIn('and _get_task_upload_target(task) == UPLOAD_TARGET_ACFUN', source)
        self.assertIn('and _get_task_upload_target(task) != UPLOAD_TARGET_ACFUN', source)


if __name__ == '__main__':
    unittest.main()
