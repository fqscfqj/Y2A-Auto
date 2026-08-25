"""Regression tests for the edit-task cover mode JSON endpoint."""

import os
import tempfile
import unittest
from unittest.mock import patch

import app as web_app
from modules import config_manager


class EditTaskCoverModeRouteTests(unittest.TestCase):
    def setUp(self):
        web_app.app.config['TESTING'] = True
        self.client = web_app.app.test_client()

    def _post(self, payload):
        return self.client.post('/settings/update_cover_mode', json=payload)

    def test_valid_modes_are_persisted_and_returned(self):
        for mode in ('crop', 'pad'):
            with self.subTest(mode=mode), \
                    patch.object(web_app, 'load_config', return_value={'password_protection_enabled': False}), \
                    patch.object(web_app, 'update_config', return_value={'COVER_PROCESSING_MODE': mode}) as update_mock, \
                    patch.object(web_app, 'configure_app') as configure_mock, \
                    patch('modules.task_manager.get_global_task_processor') as processor_mock:
                response = self._post({'mode': mode})

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {
                'success': True,
                'mode': mode,
                'cover_processing_mode': mode,
                'message': '封面处理模式已更新。',
            })
            update_mock.assert_called_once_with({'COVER_PROCESSING_MODE': mode})
            configure_mock.assert_called_once_with(web_app.app, {'COVER_PROCESSING_MODE': mode})
            processor_mock.assert_called_once_with({'COVER_PROCESSING_MODE': mode})

    def test_mode_is_written_to_config_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            def temp_app_subdir(name):
                path = os.path.join(temp_dir, name)
                os.makedirs(path, exist_ok=True)
                return path

            with patch.object(web_app, 'load_config', return_value={'password_protection_enabled': False}), \
                    patch.object(config_manager, 'get_app_subdir', side_effect=temp_app_subdir), \
                    patch.object(web_app, 'configure_app'), \
                    patch('modules.task_manager.get_global_task_processor'):
                response = self._post({'mode': 'pad'})
                persisted = config_manager.load_config()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(persisted['COVER_PROCESSING_MODE'], 'pad')

    def test_invalid_modes_are_rejected_without_persisting(self):
        invalid_payloads = ({}, {'mode': ''}, {'mode': 'crop '}, {'mode': 'CROP'}, {'mode': 'fit'}, {'mode': None})
        for payload in invalid_payloads:
            with self.subTest(payload=payload), \
                    patch.object(web_app, 'load_config', return_value={'password_protection_enabled': False}), \
                    patch.object(web_app, 'update_config') as update_mock:
                response = self._post(payload)

            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()['success'], False)
            update_mock.assert_not_called()

    def test_non_json_body_is_rejected(self):
        with patch.object(web_app, 'load_config', return_value={'password_protection_enabled': False}), \
                patch.object(web_app, 'update_config') as update_mock:
            response = self.client.post('/settings/update_cover_mode', data={'mode': 'crop'})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['success'], False)
        update_mock.assert_not_called()

    def test_route_requires_login_when_password_protection_is_enabled(self):
        with patch.object(web_app, 'load_config', return_value={'password_protection_enabled': True}), \
                patch.object(web_app, 'update_config') as update_mock:
            response = self._post({'mode': 'crop'})

        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers.get('Location', ''))
        update_mock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
