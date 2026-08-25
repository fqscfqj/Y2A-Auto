import json
import unittest

from lxml import html

import app as web_app


class YouTubeMonitorConfigTemplateTests(unittest.TestCase):
    @staticmethod
    def _embedded_config(rendered):
        document = html.fromstring(rendered)
        raw_config = document.get_element_by_id('config-data').text
        return raw_config, json.loads(raw_config)

    def test_embedded_config_json_round_trips_special_characters(self):
        keywords = 'rock & roll "live" </script><script>alert(1)</script>'
        config = {
            'channel_ids': 'UC123',
            'channel_keywords': keywords,
            'channel_mode': 'search',
            'schedule_interval': 120,
            'auto_add_to_tasks': False,
            'video_types': 'video,short,live',
        }

        with web_app.app.test_request_context('/youtube_monitor/config/1/edit'):
            rendered = web_app.render_template(
                'youtube_monitor_config.html',
                config=config,
                is_edit=True,
            )

        raw_config, parsed_config = self._embedded_config(rendered)

        self.assertEqual(parsed_config['channel_keywords'], keywords)
        self.assertNotIn('</script><script>', raw_config)

    def test_new_config_page_embeds_null_instead_of_flask_config(self):
        web_app.app.config.update(TESTING=True)
        client = web_app.app.test_client()
        with client.session_transaction() as session:
            session['logged_in'] = True

        response = client.get('/youtube_monitor/config')

        self.assertEqual(response.status_code, 200)
        _raw_config, parsed_config = self._embedded_config(response.get_data(as_text=True))
        self.assertIsNone(parsed_config)


if __name__ == '__main__':
    unittest.main()
