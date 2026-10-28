"""Issue #158：监控配置的「标题包含关键词」过滤（本地标题匹配，不消耗 API 配额）。"""

import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from modules.youtube_monitor import API_INIT_STATUS_MISSING_API_KEY, YouTubeMonitor


BASE_CONFIG = {
    'start_date': '',
    'video_types': 'video',
    'min_view_count': 0,
    'min_like_count': 0,
    'min_comment_count': 0,
    'exclude_keywords': '',
    'exclude_channel_ids': '',
    'channel_ids': '',
    'min_duration': 0,
    'max_duration': 0,
    'title_keywords': '',
    'title_match_mode': 'any',
}


def _video_info(title):
    return {
        'id': 'vid-1',
        'title': title,
        'channel_title': 'channel',
        'channel_id': 'UC123',
        'published_at': '2026-01-01T00:00:00Z',
        'duration': 'PT30M',
        'view_count': 100,
        'like_count': 10,
        'comment_count': 1,
        'video_type': 'video',
    }


class TitleKeywordMatchingTests(unittest.TestCase):
    def setUp(self):
        # 只测纯函数逻辑，不初始化数据库/调度器
        self.monitor = YouTubeMonitor.__new__(YouTubeMonitor)

    def test_split_supports_mixed_separators_and_lowercases(self):
        self.assertEqual(
            self.monitor._split_title_keywords('ASMR, 助眠；Sleep\u3000relax\n耳语'),
            ['asmr', '助眠', 'sleep', 'relax', '耳语'],
        )

    def test_split_empty_returns_empty_list(self):
        self.assertEqual(self.monitor._split_title_keywords(''), [])
        self.assertEqual(self.monitor._split_title_keywords(None), [])
        self.assertEqual(self.monitor._split_title_keywords('   '), [])

    def test_empty_title_keywords_keeps_everything(self):
        self.assertTrue(self.monitor._title_matches('普通标题', {}))
        self.assertTrue(self.monitor._title_matches('普通标题', {
            'title_keywords': '   ',
            'title_match_mode': 'all',
        }))

    def test_any_mode_needs_only_one_keyword(self):
        config = {'title_keywords': 'asmr 助眠', 'title_match_mode': 'any'}
        self.assertTrue(self.monitor._title_matches('【ASMR】深夜耳语', config))
        self.assertTrue(self.monitor._title_matches('助眠直播回放', config))
        self.assertFalse(self.monitor._title_matches('游戏实况合集', config))

    def test_all_mode_needs_every_keyword(self):
        config = {'title_keywords': 'asmr 助眠', 'title_match_mode': 'all'}
        self.assertTrue(self.monitor._title_matches('ASMR 助眠 3小时', config))
        self.assertFalse(self.monitor._title_matches('ASMR 剪辑', config))

    def test_unknown_match_mode_falls_back_to_any(self):
        config = {'title_keywords': 'asmr 助眠', 'title_match_mode': 'unexpected'}
        self.assertTrue(self.monitor._title_matches('助眠回放', config))

    def test_quoted_phrase_stays_one_keyword(self):
        self.assertEqual(
            self.monitor._split_title_keywords('"deep sleep" asmr'),
            ['deep sleep', 'asmr'],
        )
        self.assertEqual(self.monitor._split_title_keywords('“助眠 轻音乐”'), ['助眠 轻音乐'])

        config = {'title_keywords': '"deep sleep"', 'title_match_mode': 'any'}
        self.assertTrue(self.monitor._title_matches('Deep Sleep Music', config))
        self.assertFalse(self.monitor._title_matches('Sleep Walking', config))

    def test_meets_criteria_applies_title_filter(self):
        config = dict(BASE_CONFIG, title_keywords='asmr, 助眠')

        self.assertTrue(self.monitor._meets_criteria(_video_info('【ASMR】耳语'), config))
        self.assertFalse(self.monitor._meets_criteria(_video_info('游戏实况'), config))

    def test_title_filter_combines_with_exclude_keywords(self):
        config = dict(
            BASE_CONFIG,
            title_keywords='asmr',
            exclude_keywords='广告',
        )

        self.assertTrue(self.monitor._meets_criteria(_video_info('ASMR 助眠'), config))
        self.assertFalse(self.monitor._meets_criteria(_video_info('ASMR 广告合作'), config))


class TitleKeywordsPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.get_app_subdir_patcher = patch(
            "modules.youtube_monitor.get_app_subdir",
            side_effect=self._get_app_subdir,
        )
        self.init_api_patcher = patch.object(
            YouTubeMonitor,
            "_init_youtube_api",
            return_value=(False, API_INIT_STATUS_MISSING_API_KEY),
        )
        self.get_app_subdir_patcher.start()
        self.init_api_patcher.start()
        self.monitor = YouTubeMonitor()

    def tearDown(self):
        try:
            scheduler = getattr(self.monitor, "scheduler", None)
            if scheduler and getattr(scheduler, "running", False):
                scheduler.shutdown(wait=False)
        finally:
            self.get_app_subdir_patcher.stop()
            self.init_api_patcher.stop()
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _get_app_subdir(self, subdir_name):
        path = os.path.join(self.tmpdir, subdir_name)
        os.makedirs(path, exist_ok=True)
        return path

    def test_title_filter_defaults_to_empty_and_any(self):
        config_id = self.monitor.create_monitor_config({"name": "title-defaults"})

        config = self.monitor.get_monitor_config(config_id)

        self.assertEqual(config["title_keywords"], "")
        self.assertEqual(config["title_match_mode"], "any")

    def test_title_filter_is_persisted_and_updated(self):
        config_id = self.monitor.create_monitor_config({
            "name": "asmr-monitor",
            "title_keywords": "asmr, 助眠",
            "title_match_mode": "all",
        })

        created = self.monitor.get_monitor_config(config_id)
        self.assertEqual(created["title_keywords"], "asmr, 助眠")
        self.assertEqual(created["title_match_mode"], "all")

        self.monitor.update_monitor_config(config_id, {
            "name": "asmr-monitor",
            "title_keywords": "asmr",
            "title_match_mode": "any",
        })

        updated = self.monitor.get_monitor_config(config_id)
        self.assertEqual(updated["title_keywords"], "asmr")
        self.assertEqual(updated["title_match_mode"], "any")

    def test_existing_database_gets_title_filter_columns(self):
        with sqlite3.connect(self.monitor.db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(monitor_configs)")}

        self.assertIn("title_keywords", columns)
        self.assertIn("title_match_mode", columns)

    def test_legacy_database_is_migrated_with_alter_table(self):
        # 模拟升级前的老库：删掉两列后再实例化，验证 ALTER TABLE 迁移分支
        with sqlite3.connect(self.monitor.db_path) as conn:
            conn.execute("ALTER TABLE monitor_configs DROP COLUMN title_keywords")
            conn.execute("ALTER TABLE monitor_configs DROP COLUMN title_match_mode")

        migrated = YouTubeMonitor()
        try:
            with sqlite3.connect(migrated.db_path) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(monitor_configs)")}
        finally:
            scheduler = getattr(migrated, "scheduler", None)
            if scheduler and getattr(scheduler, "running", False):
                scheduler.shutdown(wait=False)

        self.assertIn("title_keywords", columns)
        self.assertIn("title_match_mode", columns)

    def test_update_without_title_fields_keeps_existing_filter(self):
        config_id = self.monitor.create_monitor_config({
            "name": "keep-filter",
            "title_keywords": "asmr",
            "title_match_mode": "all",
        })

        # 更新调用方漏传标题字段时不应静默清空过滤条件
        self.monitor.update_monitor_config(config_id, {"name": "keep-filter"})

        config = self.monitor.get_monitor_config(config_id)
        self.assertEqual(config["title_keywords"], "asmr")
        self.assertEqual(config["title_match_mode"], "all")

    def test_changing_title_filter_resets_historical_offset(self):
        config_id = self.monitor.create_monitor_config({
            "name": "historical-filter",
            "channel_mode": "historical",
            "title_keywords": "",
            "historical_offset": 42,
        })
        self.assertEqual(self.monitor.get_monitor_config(config_id)["historical_offset"], 42)

        self.monitor.update_monitor_config(config_id, {
            "name": "historical-filter",
            "channel_mode": "historical",
            "title_keywords": "asmr",
            "historical_offset": 42,
        })

        self.assertEqual(self.monitor.get_monitor_config(config_id)["historical_offset"], 0)

    def test_unchanged_title_filter_keeps_historical_offset(self):
        config_id = self.monitor.create_monitor_config({
            "name": "historical-keep",
            "channel_mode": "historical",
            "title_keywords": "asmr",
            "historical_offset": 42,
        })

        self.monitor.update_monitor_config(config_id, {
            "name": "historical-keep",
            "channel_mode": "historical",
            "title_keywords": "asmr",
            "historical_offset": 42,
        })

        self.assertEqual(self.monitor.get_monitor_config(config_id)["historical_offset"], 42)


if __name__ == '__main__':
    unittest.main()
