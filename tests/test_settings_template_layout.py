"""设置页结构与配置一致性守卫测试。

覆盖四类回归:
1. 分组完整性 —— 信息架构重组后的 9 个分组必须齐全
2. 字段零丢失/零重复 —— 搬运设置项时不得漏掉或重复控件
3. 字段归属正确 —— 每个字段必须落在预期分组(防止同类配置再次被拆散)
4. 复选框可关闭性 —— 后端白名单必须覆盖模板里所有开关,
   否则「取消勾选后保存」不会生效(历史上 DOWNLOAD_CLEANUP_ENABLED 即因此失效)
"""
import math
import pathlib
import re
import unittest

from lxml import html as lxml_html

import app as web_app
from app import (
    SETTINGS_CHECKBOX_FIELDS,
    SETTINGS_FLOAT_FIELDS,
    SETTINGS_INT_FIELDS,
    SETTINGS_RANGE_GUARDS,
    _PINNED_SETTINGS_DEFAULTS,
)
from modules.config_manager import DEFAULT_CONFIG


def _bound_text(value):
    """把 guard 边界渲染成 HTML 属性里常见的形式（整数不带小数点）。"""
    numeric = float(value)
    return str(int(numeric)) if numeric.is_integer() else f'{numeric:g}'


def _bound_float(raw):
    """把 HTML 属性里的 min/max 解析成数值；缺失或非法时返回 None。"""
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


EXPECTED_PANES = [
    'vtab-publish',
    'vtab-accounts',
    'vtab-network',
    'vtab-ai',
    'vtab-asr',
    'vtab-subtitle',
    'vtab-media',
    'vtab-notify',
    'vtab-ops',
]

# 字段 -> 所属分组(固化基线,由改造后的设置页导出)。
# 说明:其中绝大多数是「必然如此」的归属(例如 LLM 端点属于 AI 分组);
# 少数属于策略性取舍(例如「下载内容清理」放网络分组、「日志清理」放运维分组、
# 「内容审核」放运行与投稿分组),这些是可以再讨论的。
# 调整策略性归属时请同步更新本表——该断言的作用是防止搬运时误放,
# 而不是禁止有意调整。
FIELD_TAB_MAP = {
    'ACFUN_COOKIES_PATH': 'vtab-accounts',
    'AI_FAILOVER_TIMEOUT_SECONDS': 'vtab-ai',
    'AI_SEGMENTATION_API_KEY': 'vtab-ai',
    'AI_SEGMENTATION_BASE_URL': 'vtab-ai',
    'AI_SEGMENTATION_BATCH_WINDOW_S': 'vtab-asr',
    'AI_SEGMENTATION_BOUNDARY_REFINE_ENABLED': 'vtab-asr',
    'AI_SEGMENTATION_ENABLED': 'vtab-asr',
    'AI_SEGMENTATION_MAX_CHARS_PER_BATCH': 'vtab-asr',
    'AI_SEGMENTATION_MAX_CPS': 'vtab-asr',
    'AI_SEGMENTATION_MAX_CUE_DURATION_S': 'vtab-asr',
    'AI_SEGMENTATION_MAX_RETRIES': 'vtab-asr',
    'AI_SEGMENTATION_MIN_CUE_DURATION_S': 'vtab-asr',
    'AI_SEGMENTATION_MODEL_NAME': 'vtab-ai',
    'AI_SEGMENTATION_RHYTHM_ENABLED': 'vtab-asr',
    'AI_SEGMENTATION_TEMPERATURE': 'vtab-asr',
    'AI_SEGMENTATION_THINKING_ENABLED': 'vtab-ai',
    'ALIYUN_ACCESS_KEY_ID': 'vtab-publish',
    'ALIYUN_ACCESS_KEY_SECRET': 'vtab-publish',
    'ALIYUN_CONTENT_MODERATION_REGION': 'vtab-publish',
    'ALIYUN_TEXT_MODERATION_SERVICE': 'vtab-publish',
    'AUDIO_CHUNK_OVERLAP_S': 'vtab-asr',
    'AUDIO_CHUNK_WINDOW_S': 'vtab-asr',
    'AUTO_MODE_ENABLED': 'vtab-publish',
    'BILIBILI_COOKIES_PATH': 'vtab-accounts',
    'CONTENT_MODERATION_ENABLED': 'vtab-publish',
    'COOKIECLOUD_ALLOW_PLAINTEXT_EXPORT': 'vtab-accounts',
    'COOKIECLOUD_CRYPTO_TYPE': 'vtab-accounts',
    'COOKIECLOUD_ENABLED': 'vtab-accounts',
    'COOKIECLOUD_PASSWORD': 'vtab-accounts',
    'COOKIECLOUD_SERVER_URL': 'vtab-accounts',
    'COOKIECLOUD_UUID': 'vtab-accounts',
    'COVER_PROCESSING_MODE': 'vtab-publish',
    'DELETE_DOWNLOAD_FILES_AFTER_UPLOAD': 'vtab-publish',
    'DOWNLOAD_CLEANUP_ENABLED': 'vtab-network',
    'DOWNLOAD_CLEANUP_HOURS': 'vtab-network',
    'DOWNLOAD_CLEANUP_INTERVAL': 'vtab-network',
    'FALLBACK_OPENAI_API_KEY': 'vtab-ai',
    'FALLBACK_OPENAI_BASE_URL': 'vtab-ai',
    'FALLBACK_OPENAI_MODEL_NAME': 'vtab-ai',
    'FFMPEG_AUTO_DOWNLOAD': 'vtab-media',
    'FFMPEG_LOCATION': 'vtab-media',
    'FIXED_PARTITION_ID': 'vtab-publish',
    'FIXED_PARTITION_ID_BILIBILI': 'vtab-publish',
    'GENERATE_TAGS': 'vtab-publish',
    'LOGIN_LOCKOUT_MINUTES': 'vtab-ops',
    'LOGIN_MAX_FAILED_ATTEMPTS': 'vtab-ops',
    'LOGIN_SESSION_TIMEOUT_MINUTES': 'vtab-ops',
    'LOG_CLEANUP_ENABLED': 'vtab-ops',
    'LOG_CLEANUP_HOURS': 'vtab-ops',
    'LOG_CLEANUP_INTERVAL': 'vtab-ops',
    'MAX_CONCURRENT_TASKS': 'vtab-ops',
    'MAX_CONCURRENT_UPLOADS': 'vtab-ops',
    'METADATA_DESC_RETRY_MODE': 'vtab-subtitle',
    'METADATA_DESC_RETRY_TEXT': 'vtab-subtitle',
    'METADATA_TRANSLATE_MODE': 'vtab-subtitle',
    'METADATA_TRANSLATE_TEXT': 'vtab-subtitle',
    'NOTIFY_ENABLED': 'vtab-notify',
    'NOTIFY_EVENT_LOGIN_LOCKED': 'vtab-notify',
    'NOTIFY_EVENT_LOGIN_SUCCESS': 'vtab-notify',
    'NOTIFY_EVENT_QR_LOGIN_FAILED': 'vtab-notify',
    'NOTIFY_EVENT_QR_LOGIN_SUCCESS': 'vtab-notify',
    'NOTIFY_EVENT_TASK_ADDED': 'vtab-notify',
    'NOTIFY_EVENT_TASK_COMPLETED': 'vtab-notify',
    'NOTIFY_EVENT_TASK_FAILED': 'vtab-notify',
    'NOTIFY_MESSAGE_PUSHER_CHANNEL': 'vtab-notify',
    'NOTIFY_MESSAGE_PUSHER_ENABLED': 'vtab-notify',
    'NOTIFY_MESSAGE_PUSHER_SERVER': 'vtab-notify',
    'NOTIFY_MESSAGE_PUSHER_TOKEN': 'vtab-notify',
    'NOTIFY_MESSAGE_PUSHER_USERNAME': 'vtab-notify',
    'NOTIFY_SERVERCHAN_ENABLED': 'vtab-notify',
    'NOTIFY_SERVERCHAN_SENDKEY': 'vtab-notify',
    'NOTIFY_WECOM_ENABLED': 'vtab-notify',
    'NOTIFY_WECOM_WEBHOOK_URL': 'vtab-notify',
    'OPENAI_API_KEY': 'vtab-ai',
    'OPENAI_BASE_URL': 'vtab-ai',
    'OPENAI_MODEL_NAME': 'vtab-ai',
    'OPENAI_THINKING_ENABLED': 'vtab-ai',
    'RECOMMEND_PARTITION': 'vtab-publish',
    'RECOMMEND_PARTITION_WITH_COVER': 'vtab-publish',
    'SPEECH_RECOGNITION_ENABLED': 'vtab-asr',
    'SPEECH_RECOGNITION_PROVIDER': 'vtab-asr',
    'SUBTITLE_BATCH_SIZE': 'vtab-subtitle',
    'SUBTITLE_EMBED_IN_VIDEO': 'vtab-subtitle',
    'SUBTITLE_FILTER_FILLER_WORDS': 'vtab-subtitle',
    'SUBTITLE_FONT_NAME': 'vtab-subtitle',
    'SUBTITLE_KEEP_ORIGINAL': 'vtab-subtitle',
    'SUBTITLE_MAX_LINES': 'vtab-subtitle',
    'SUBTITLE_MAX_LINES_ENABLED': 'vtab-subtitle',
    'SUBTITLE_MAX_LINE_LENGTH': 'vtab-subtitle',
    'SUBTITLE_MAX_LINE_LENGTH_ENABLED': 'vtab-subtitle',
    'SUBTITLE_MAX_RETRIES': 'vtab-subtitle',
    'SUBTITLE_MAX_WORKERS': 'vtab-subtitle',
    'SUBTITLE_MERGE_GAP_ENABLED': 'vtab-subtitle',
    'SUBTITLE_MERGE_GAP_S': 'vtab-subtitle',
    'SUBTITLE_MIN_CUE_DURATION_ENABLED': 'vtab-subtitle',
    'SUBTITLE_MIN_CUE_DURATION_S': 'vtab-subtitle',
    'SUBTITLE_MIN_TEXT_LENGTH': 'vtab-subtitle',
    'SUBTITLE_MIN_TEXT_LENGTH_ENABLED': 'vtab-subtitle',
    'SUBTITLE_NORMALIZE_PUNCTUATION': 'vtab-subtitle',
    'SUBTITLE_OPENAI_API_KEY': 'vtab-ai',
    'SUBTITLE_OPENAI_BASE_URL': 'vtab-ai',
    'SUBTITLE_OPENAI_MODEL_NAME': 'vtab-ai',
    'SUBTITLE_OPENAI_THINKING_ENABLED': 'vtab-ai',
    'SUBTITLE_PREFER_SINGLE_LINE': 'vtab-subtitle',
    'SUBTITLE_QC_API_KEY': 'vtab-ai',
    'SUBTITLE_QC_BASE_URL': 'vtab-ai',
    'SUBTITLE_QC_ENABLED': 'vtab-subtitle',
    'SUBTITLE_QC_MAX_CHARS': 'vtab-subtitle',
    'SUBTITLE_QC_MODEL_NAME': 'vtab-ai',
    'SUBTITLE_QC_SAMPLE_MAX_ITEMS': 'vtab-subtitle',
    'SUBTITLE_QC_THINKING_ENABLED': 'vtab-ai',
    'SUBTITLE_QC_THRESHOLD': 'vtab-subtitle',
    'SUBTITLE_QC_TIMEOUT_SECONDS': 'vtab-subtitle',
    'SUBTITLE_RETRY_DELAY': 'vtab-subtitle',
    'SUBTITLE_SINGLE_LINE_MIN_FONT_SCALE': 'vtab-subtitle',
    'SUBTITLE_SOURCE_LANGUAGE': 'vtab-subtitle',
    'SUBTITLE_TARGET_LANGUAGE': 'vtab-subtitle',
    'SUBTITLE_TIME_OFFSET_ENABLED': 'vtab-subtitle',
    'SUBTITLE_TIME_OFFSET_S': 'vtab-subtitle',
    'SUBTITLE_TRANSLATE_MODE': 'vtab-subtitle',
    'SUBTITLE_TRANSLATE_STRICT_MODE': 'vtab-subtitle',
    'SUBTITLE_TRANSLATE_STRICT_TEXT': 'vtab-subtitle',
    'SUBTITLE_TRANSLATE_TEXT': 'vtab-subtitle',
    'SUBTITLE_TRANSLATION_ENABLED': 'vtab-subtitle',
    'TRANSLATE_DESCRIPTION': 'vtab-publish',
    'TRANSLATE_TITLE': 'vtab-publish',
    'UPLOAD_APPEND_REPOST_NOTICE': 'vtab-publish',
    'UPLOAD_TARGET_DEFAULT': 'vtab-publish',
    'VAD_ENABLED': 'vtab-asr',
    'VAD_MAX_SEGMENT_S': 'vtab-asr',
    'VAD_MIN_SPEECH_COVERAGE_RATIO': 'vtab-asr',
    'VAD_REFINEMENT_ENABLED': 'vtab-asr',
    'VAD_SILERO_MIN_SPEECH_MS': 'vtab-asr',
    'VAD_SILERO_THRESHOLD': 'vtab-asr',
    'VIDEO_CPU_CODEC': 'vtab-media',
    'VIDEO_CPU_PRESET': 'vtab-media',
    'VIDEO_CPU_PRESET_HD': 'vtab-media',
    'VIDEO_COLOR_METADATA_MODE': 'vtab-media',
    'VIDEO_CUSTOM_PARAMS': 'vtab-media',
    'VIDEO_CUSTOM_PARAMS_ENABLED': 'vtab-media',
    'VIDEO_ENCODER': 'vtab-media',
    'VIDEO_HW_QUALITY_BOOST': 'vtab-media',
    'VIDEO_HW_QUALITY_LEVEL': 'vtab-media',
    'VIDEO_QUALITY_MODE': 'vtab-media',
    'VIDEO_QUALITY_VALUE': 'vtab-media',
    'VIDEO_X264_TUNE': 'vtab-media',
    'VOXTRAL_API_KEY': 'vtab-asr',
    'VOXTRAL_BASE_URL': 'vtab-asr',
    'VOXTRAL_CONTEXT_BIAS': 'vtab-asr',
    'VOXTRAL_DIARIZE': 'vtab-asr',
    'VOXTRAL_ENFORCE_MAX_DURATION': 'vtab-asr',
    'VOXTRAL_LANGUAGE': 'vtab-asr',
    'VOXTRAL_LONG_AUDIO_MARGIN_S': 'vtab-asr',
    'VOXTRAL_MAX_AUDIO_DURATION_S': 'vtab-asr',
    'VOXTRAL_MODEL_NAME': 'vtab-asr',
    'VOXTRAL_TIMESTAMP_GRANULARITIES': 'vtab-asr',
    'WHISPER_API_KEY': 'vtab-asr',
    'WHISPER_BASE_URL': 'vtab-asr',
    'WHISPER_LANGUAGE': 'vtab-asr',
    'WHISPER_MAX_RETRIES': 'vtab-asr',
    'WHISPER_MODEL_NAME': 'vtab-asr',
    'WHISPER_PROMPT': 'vtab-asr',
    'WHISPER_TIMESTAMP_GRANULARITIES': 'vtab-asr',
    'WHISPER_TRANSLATE': 'vtab-asr',
    'YOUTUBE_API_KEY': 'vtab-network',
    'YOUTUBE_API_PROXY_ENABLED': 'vtab-network',
    'YOUTUBE_API_PROXY_PASSWORD': 'vtab-network',
    'YOUTUBE_API_PROXY_URL': 'vtab-network',
    'YOUTUBE_API_PROXY_USERNAME': 'vtab-network',
    'YOUTUBE_AUTO_GENERATED_SUBTITLES_ENABLED': 'vtab-subtitle',
    'YOUTUBE_COOKIES_PATH': 'vtab-accounts',
    'YOUTUBE_DOWNLOAD_MAX_HEIGHT': 'vtab-network',
    'YOUTUBE_DOWNLOAD_QUALITY_MODE': 'vtab-network',
    'YOUTUBE_DOWNLOAD_THREADS': 'vtab-network',
    'YOUTUBE_PROXY_ENABLED': 'vtab-network',
    'YOUTUBE_PROXY_PASSWORD': 'vtab-network',
    'YOUTUBE_PROXY_URL': 'vtab-network',
    'YOUTUBE_PROXY_USERNAME': 'vtab-network',
    'YOUTUBE_THROTTLED_RATE': 'vtab-network',
    'YOUTUBE_UPLOADER_AS_FIRST_TAG': 'vtab-publish',
    'acfun_cookies_file': 'vtab-accounts',
    'bilibili_cookies_file': 'vtab-accounts',
    'confirm_password': 'vtab-ops',
    'new_password': 'vtab-ops',
    'password_protection_enabled': 'vtab-ops',
    'youtube_cookies_file': 'vtab-accounts',
}

# 设置页全部控件 name 的固化集合(唯一值)
ALL_FIELD_NAMES = frozenset([
    'ACFUN_COOKIES_PATH',
    'AI_FAILOVER_TIMEOUT_SECONDS',
    'AI_SEGMENTATION_API_KEY',
    'AI_SEGMENTATION_BASE_URL',
    'AI_SEGMENTATION_BATCH_WINDOW_S',
    'AI_SEGMENTATION_BOUNDARY_REFINE_ENABLED',
    'AI_SEGMENTATION_ENABLED',
    'AI_SEGMENTATION_MAX_CHARS_PER_BATCH',
    'AI_SEGMENTATION_MAX_CPS',
    'AI_SEGMENTATION_MAX_CUE_DURATION_S',
    'AI_SEGMENTATION_MAX_RETRIES',
    'AI_SEGMENTATION_MIN_CUE_DURATION_S',
    'AI_SEGMENTATION_MODEL_NAME',
    'AI_SEGMENTATION_RHYTHM_ENABLED',
    'AI_SEGMENTATION_TEMPERATURE',
    'AI_SEGMENTATION_THINKING_ENABLED',
    'ALIYUN_ACCESS_KEY_ID',
    'ALIYUN_ACCESS_KEY_SECRET',
    'ALIYUN_CONTENT_MODERATION_REGION',
    'ALIYUN_TEXT_MODERATION_SERVICE',
    'AUDIO_CHUNK_OVERLAP_S',
    'AUDIO_CHUNK_WINDOW_S',
    'AUTO_MODE_ENABLED',
    'BILIBILI_COOKIES_PATH',
    'CONTENT_MODERATION_ENABLED',
    'COOKIECLOUD_ALLOW_PLAINTEXT_EXPORT',
    'COOKIECLOUD_CRYPTO_TYPE',
    'COOKIECLOUD_ENABLED',
    'COOKIECLOUD_PASSWORD',
    'COOKIECLOUD_SERVER_URL',
    'COOKIECLOUD_UUID',
    'COVER_PROCESSING_MODE',
    'DELETE_DOWNLOAD_FILES_AFTER_UPLOAD',
    'DOWNLOAD_CLEANUP_ENABLED',
    'DOWNLOAD_CLEANUP_HOURS',
    'DOWNLOAD_CLEANUP_INTERVAL',
    'FALLBACK_OPENAI_API_KEY',
    'FALLBACK_OPENAI_BASE_URL',
    'FALLBACK_OPENAI_MODEL_NAME',
    'FFMPEG_AUTO_DOWNLOAD',
    'FFMPEG_LOCATION',
    'FIXED_PARTITION_ID',
    'FIXED_PARTITION_ID_BILIBILI',
    'GENERATE_TAGS',
    'LOGIN_LOCKOUT_MINUTES',
    'LOGIN_MAX_FAILED_ATTEMPTS',
    'LOGIN_SESSION_TIMEOUT_MINUTES',
    'LOG_CLEANUP_ENABLED',
    'LOG_CLEANUP_HOURS',
    'LOG_CLEANUP_INTERVAL',
    'MAX_CONCURRENT_TASKS',
    'MAX_CONCURRENT_UPLOADS',
    'METADATA_DESC_RETRY_MODE',
    'METADATA_DESC_RETRY_TEXT',
    'METADATA_TRANSLATE_MODE',
    'METADATA_TRANSLATE_TEXT',
    'NOTIFY_ENABLED',
    'NOTIFY_EVENT_LOGIN_LOCKED',
    'NOTIFY_EVENT_LOGIN_SUCCESS',
    'NOTIFY_EVENT_QR_LOGIN_FAILED',
    'NOTIFY_EVENT_QR_LOGIN_SUCCESS',
    'NOTIFY_EVENT_TASK_ADDED',
    'NOTIFY_EVENT_TASK_COMPLETED',
    'NOTIFY_EVENT_TASK_FAILED',
    'NOTIFY_MESSAGE_PUSHER_CHANNEL',
    'NOTIFY_MESSAGE_PUSHER_ENABLED',
    'NOTIFY_MESSAGE_PUSHER_SERVER',
    'NOTIFY_MESSAGE_PUSHER_TOKEN',
    'NOTIFY_MESSAGE_PUSHER_USERNAME',
    'NOTIFY_SERVERCHAN_ENABLED',
    'NOTIFY_SERVERCHAN_SENDKEY',
    'NOTIFY_WECOM_ENABLED',
    'NOTIFY_WECOM_WEBHOOK_URL',
    'OPENAI_API_KEY',
    'OPENAI_BASE_URL',
    'OPENAI_MODEL_NAME',
    'OPENAI_THINKING_ENABLED',
    'RECOMMEND_PARTITION',
    'RECOMMEND_PARTITION_WITH_COVER',
    'SPEECH_RECOGNITION_ENABLED',
    'SPEECH_RECOGNITION_PROVIDER',
    'SUBTITLE_BATCH_SIZE',
    'SUBTITLE_EMBED_IN_VIDEO',
    'SUBTITLE_FILTER_FILLER_WORDS',
    'SUBTITLE_FONT_NAME',
    'SUBTITLE_KEEP_ORIGINAL',
    'SUBTITLE_MAX_LINES',
    'SUBTITLE_MAX_LINES_ENABLED',
    'SUBTITLE_MAX_LINE_LENGTH',
    'SUBTITLE_MAX_LINE_LENGTH_ENABLED',
    'SUBTITLE_MAX_RETRIES',
    'SUBTITLE_MAX_WORKERS',
    'SUBTITLE_MERGE_GAP_ENABLED',
    'SUBTITLE_MERGE_GAP_S',
    'SUBTITLE_MIN_CUE_DURATION_ENABLED',
    'SUBTITLE_MIN_CUE_DURATION_S',
    'SUBTITLE_MIN_TEXT_LENGTH',
    'SUBTITLE_MIN_TEXT_LENGTH_ENABLED',
    'SUBTITLE_NORMALIZE_PUNCTUATION',
    'SUBTITLE_OPENAI_API_KEY',
    'SUBTITLE_OPENAI_BASE_URL',
    'SUBTITLE_OPENAI_MODEL_NAME',
    'SUBTITLE_OPENAI_THINKING_ENABLED',
    'SUBTITLE_PREFER_SINGLE_LINE',
    'SUBTITLE_BACKGROUND_COLOR',
    'SUBTITLE_BACKGROUND_ENABLED',
    'SUBTITLE_BACKGROUND_OPACITY',
    'SUBTITLE_FONT_COLOR',
    'SUBTITLE_FONT_SIZE_SCALE',
    'SUBTITLE_MARGIN_V_SCALE',
    'SUBTITLE_OUTLINE_COLOR',
    'SUBTITLE_OUTLINE_ENABLED',
    'SUBTITLE_OUTLINE_SCALE',
    'SUBTITLE_SHADOW_ENABLED',
    'SUBTITLE_SHADOW_SCALE',
    'SUBTITLE_TEXT_BOLD',
    'SUBTITLE_QC_API_KEY',
    'SUBTITLE_QC_BASE_URL',
    'SUBTITLE_QC_ENABLED',
    'SUBTITLE_QC_MAX_CHARS',
    'SUBTITLE_QC_MODEL_NAME',
    'SUBTITLE_QC_SAMPLE_MAX_ITEMS',
    'SUBTITLE_QC_THINKING_ENABLED',
    'SUBTITLE_QC_THRESHOLD',
    'SUBTITLE_QC_TIMEOUT_SECONDS',
    'SUBTITLE_RETRY_DELAY',
    'SUBTITLE_SINGLE_LINE_MIN_FONT_SCALE',
    'SUBTITLE_SOURCE_LANGUAGE',
    'SUBTITLE_TARGET_LANGUAGE',
    'SUBTITLE_TIME_OFFSET_ENABLED',
    'SUBTITLE_TIME_OFFSET_S',
    'SUBTITLE_TRANSLATE_MODE',
    'SUBTITLE_TRANSLATE_STRICT_MODE',
    'SUBTITLE_TRANSLATE_STRICT_TEXT',
    'SUBTITLE_TRANSLATE_TEXT',
    'SUBTITLE_TRANSLATION_ALLOW_PARTIAL',
    'SUBTITLE_TRANSLATION_ENABLED',
    'ASR_FAILURE_BLOCKS_EMBED',
    'SUBTITLE_QC_TIMELINE_ENABLED',
    'VAD_DROP_ISOLATED_SHORT',
    'WHISPER_CONDITION_ON_PREVIOUS_TEXT',
    'TRANSLATE_DESCRIPTION',
    'TRANSLATE_TITLE',
    'UPLOAD_APPEND_REPOST_NOTICE',
    'UPLOAD_TARGET_DEFAULT',
    'VAD_ENABLED',
    'VAD_MAX_SEGMENT_S',
    'VAD_MIN_SPEECH_COVERAGE_RATIO',
    'VAD_REFINEMENT_ENABLED',
    'VAD_SILERO_MIN_SPEECH_MS',
    'VAD_SILERO_THRESHOLD',
    'VIDEO_CPU_CODEC',
    'VIDEO_CPU_PRESET',
    'VIDEO_CPU_PRESET_HD',
    'VIDEO_COLOR_METADATA_MODE',
    'VIDEO_CUSTOM_PARAMS',
    'VIDEO_CUSTOM_PARAMS_ENABLED',
    'VIDEO_ENCODER',
    'VIDEO_HW_QUALITY_BOOST',
    'VIDEO_HW_QUALITY_LEVEL',
    'VIDEO_QUALITY_MODE',
    'VIDEO_QUALITY_VALUE',
    'VIDEO_X264_TUNE',
    'VOXTRAL_API_KEY',
    'VOXTRAL_BASE_URL',
    'VOXTRAL_CONTEXT_BIAS',
    'VOXTRAL_DIARIZE',
    'VOXTRAL_ENFORCE_MAX_DURATION',
    'VOXTRAL_LANGUAGE',
    'VOXTRAL_LONG_AUDIO_MARGIN_S',
    'VOXTRAL_MAX_AUDIO_DURATION_S',
    'VOXTRAL_MODEL_NAME',
    'VOXTRAL_TIMESTAMP_GRANULARITIES',
    'WHISPER_API_KEY',
    'WHISPER_BASE_URL',
    'WHISPER_LANGUAGE',
    'WHISPER_MAX_RETRIES',
    'WHISPER_MODEL_NAME',
    'WHISPER_PROMPT',
    'WHISPER_TIMESTAMP_GRANULARITIES',
    'WHISPER_TRANSLATE',
    'YOUTUBE_API_KEY',
    'YOUTUBE_API_PROXY_ENABLED',
    'YOUTUBE_API_PROXY_PASSWORD',
    'YOUTUBE_API_PROXY_URL',
    'YOUTUBE_API_PROXY_USERNAME',
    'YOUTUBE_AUTO_GENERATED_SUBTITLES_ENABLED',
    'YOUTUBE_COOKIES_PATH',
    'YOUTUBE_DOWNLOAD_MAX_HEIGHT',
    'YOUTUBE_DOWNLOAD_QUALITY_MODE',
    'YOUTUBE_DOWNLOAD_THREADS',
    'YOUTUBE_PROXY_ENABLED',
    'YOUTUBE_PROXY_PASSWORD',
    'YOUTUBE_PROXY_URL',
    'YOUTUBE_PROXY_USERNAME',
    'YOUTUBE_THROTTLED_RATE',
    'YOUTUBE_UPLOADER_AS_FIRST_TAG',
    'acfun_cookies_file',
    'bilibili_cookies_file',
    'confirm_password',
    'hours',
    'new_password',
    'password_protection_enabled',
    'youtube_cookies_file',
])

# 允许出现在多个表单中的重复 name(两个独立清理表单各有一个 hours 隐藏域)
EXPECTED_DUPLICATE_NAMES = {"hours": 2}

# 每个分组至少应有的折叠区数量:高级项必须默认折叠,避免再次平铺
MIN_COLLAPSES_PER_PANE = {
    'vtab-publish': 2,
    'vtab-accounts': 0,
    'vtab-network': 3,
    'vtab-ai': 3,
    'vtab-asr': 3,
    'vtab-subtitle': 8,
    'vtab-media': 0,
    'vtab-notify': 1,
    'vtab-ops': 1,
}


class SettingsTemplateLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web_app.app.config.update(TESTING=True)
        client = web_app.app.test_client()
        with client.session_transaction() as session:
            session['logged_in'] = True
        response = client.get('/settings')
        if response.status_code != 200:
            raise AssertionError(f'设置页渲染失败: {response.status_code}')
        cls.page = response.get_data(as_text=True)
        cls.doc = lxml_html.fromstring(cls.page)

    def test_分组齐全且顺序稳定(self):
        panes = self.doc.xpath(
            "//div[contains(@class,'tab-pane') and starts-with(@id,'vtab-')]")
        self.assertEqual([p.get('id') for p in panes], EXPECTED_PANES)
        # 每个分组都必须有对应的导航项
        for pane_id in EXPECTED_PANES:
            links = self.doc.xpath(f"//a[@href='#{pane_id}']")
            self.assertTrue(links, f'缺少分组导航: {pane_id}')

    def test_字段零丢失且无意外重复(self):
        found = set()
        counts = {}
        for el in self.doc.xpath(
                "//form[@id='settings-form']//input[@name] | "
                "//form[@id='settings-form']//select[@name] | "
                "//form[@id='settings-form']//textarea[@name]"):
            name = el.get('name')
            found.add(name)
            counts[name] = counts.get(name, 0) + 1

        missing = sorted(ALL_FIELD_NAMES - set(EXPECTED_DUPLICATE_NAMES) - found)
        self.assertEqual(missing, [], f'设置页丢失字段: {missing}')

        unexpected = sorted(name for name in found if name not in ALL_FIELD_NAMES)
        self.assertEqual(unexpected, [], f'出现未登记字段: {unexpected}')

        duplicated = {n: c for n, c in counts.items() if c > 1}
        self.assertEqual(
            duplicated, {},
            '设置表单内出现重复 name,提交时后者会覆盖前者')

        # 独立清理表单仍须保留各自的 hours 隐藏域
        outside = self.doc.xpath("//form[not(@id='settings-form')]//input[@name]/@name")
        expected_outside = []
        for name, count in EXPECTED_DUPLICATE_NAMES.items():
            expected_outside.extend([name] * count)
        self.assertEqual(
            sorted(outside), sorted(expected_outside),
            '独立清理表单的隐藏字段发生变化')

    def test_每个字段都落在预期分组(self):
        wrong = {}
        for name, expected_tab in FIELD_TAB_MAP.items():
            panes = self.doc.xpath(
                f"//*[@name='{name}']/ancestor::div[contains(@class,'tab-pane')][1]/@id")
            if not panes:
                wrong[name] = '<未找到>'
            elif panes[0] != expected_tab:
                wrong[name] = panes[0]
        self.assertEqual(wrong, {}, f'字段归属与预期不符: {wrong}')

    def test_所有开关都能被保存为关闭(self):
        whitelist = set(SETTINGS_CHECKBOX_FIELDS)
        offenders = []
        for el in self.doc.xpath(
                "//form[@id='settings-form']//input[@type='checkbox'][@name]"):
            if el.get('disabled') is not None:
                continue
            if el.get('name') not in whitelist:
                offenders.append(el.get('name'))
        self.assertEqual(
            sorted(set(offenders)), [],
            '这些开关不在保存白名单里,取消勾选后不会生效')

    def test_白名单里的开关在页面上都有控件(self):
        on_page = {
            el.get('name')
            for el in self.doc.xpath(
                "//form[@id='settings-form']//input[@type='checkbox'][@name]")
        }
        hidden_supported = {
            el.get('name')
            for el in self.doc.xpath(
                "//form[@id='settings-form']//input[@type='hidden'][@name]")
        }
        # 这些键是内部实现细节,刻意不暴露开关
        intentionally_not_exposed = {
            'SUBTITLE_MAX_LINE_LENGTH_ENABLED', 'SUBTITLE_MAX_LINES_ENABLED',
        }
        missing = sorted(
            set(SETTINGS_CHECKBOX_FIELDS) - on_page - hidden_supported - intentionally_not_exposed)
        self.assertEqual(
            missing, [],
            '白名单里的开关在设置页没有任何控件,每次保存都会被强制关闭')

    def test_高级参数默认折叠(self):
        for pane_id, minimum in MIN_COLLAPSES_PER_PANE.items():
            pane = self.doc.get_element_by_id(pane_id)
            collapses = pane.xpath(".//*[@data-bs-toggle='collapse']")
            self.assertGreaterEqual(
                len(collapses), minimum,
                f'{pane_id} 的折叠区少于 {minimum} 个,高级参数可能又被平铺')
            for button in collapses:
                expanded = button.get('aria-expanded')
                if expanded is not None:
                    self.assertEqual(
                        expanded, 'false',
                        '折叠区默认应为收起状态(aria-expanded=false)')

    def test_旧分组_hash_仍可跳转(self):
        for legacy, current in (
                ('#vtab-general', '#vtab-publish'),
                ('#vtab-ai-models', '#vtab-ai'),
                ('#vtab-subtitle-voice', '#vtab-subtitle'),
                ('#vtab-notifications', '#vtab-notify')):
            alias = f"'{legacy}': '{current}'"
            pane_id = current.lstrip('#')
            self.assertTrue(alias in self.page, f'缺少旧 hash 别名 {legacy} -> {current}')
            self.assertTrue(f'id="{pane_id}"' in self.page, f'缺少目标分组 {current}')

    def test_共享JS辅助函数定义在顶层作用域(self):
        # 卡片级重置与字段级搜索分别位于两个独立的 DOMContentLoaded 回调中；
        # 若把 settingsFieldLabel 定义在任一回调内部,另一个回调会抛 ReferenceError,
        # 导致搜索索引构建中断、搜索功能整体失效(历史上出现过该回归)。
        for symbol in ('const settingsEscapeSelector', 'const settingsFieldLabel'):
            line = next((ln for ln in self.page.splitlines() if ln.startswith(symbol)), None)
            self.assertIsNotNone(
                line,
                f'{symbol} 必须顶格定义在 <script> 顶层(不能在 DOMContentLoaded 回调内)')
        # 两个消费方(卡片重置、字段级搜索)都应引用它
        self.assertGreaterEqual(
            self.page.count('settingsFieldLabel('), 2,
            'settingsFieldLabel 应同时被卡片重置与字段级搜索使用')

    def test_模板数值控件都在数值白名单里(self):
        # 镜像守卫:数值控件若不在白名单,保存时会被原样存成字符串
        # (main 上 DOWNLOAD_CLEANUP_HOURS / INTERVAL 就属于这种情况)。
        whitelist = set(SETTINGS_INT_FIELDS) | set(SETTINGS_FLOAT_FIELDS)
        offenders = []
        for el in self.doc.xpath(
                "//form[@id='settings-form']//input[@type='number'][@name]"):
            if el.get('name') not in whitelist:
                offenders.append(el.get('name'))
        self.assertEqual(
            sorted(set(offenders)), [],
            '这些数值控件不在归一化白名单里,保存时不会被转成数值')

    def test_数值白名单的键都存在于DEFAULT_CONFIG(self):
        # _settings_fallback_default 依赖键存在于 DEFAULT_CONFIG,
        # 缺失时会静默退化成 1 / 0.0(例如批字符数变成 1 会把批次切碎)。
        missing = sorted(
            key for key in (SETTINGS_INT_FIELDS + SETTINGS_FLOAT_FIELDS)
            if key not in DEFAULT_CONFIG)
        self.assertEqual(missing, [], f'白名单键不在 DEFAULT_CONFIG 中: {missing}')

    # 注:「整数与浮点白名单不相交」这条不变量没有对应的测试,而是在 app.py 顶层用
    # 显式 raise 拦截(_NUMERIC_WHITELIST_OVERLAP)。之所以不在这里再写一条:
    # 一旦交叉,app.py 在 import 期就会抛错,本文件根本收集不到,那条测试永远
    # 不可能失败,属于「死测试」。raise 的错误信息里已带上冲突的键名。

    def test_换行上限字段是真实控件且不再被钉死(self):
        # 历史实现用 hidden 输入把 SUBTITLE_MAX_LINE_LENGTH / SUBTITLE_MAX_LINES
        # 钉死为 999 / 1，与 DEFAULT_CONFIG 的 42 / 2 分叉：从未保存过设置页
        # 的安装用 42/2，保存过一次就变成「永不换行的单行字幕」。
        # 钉死表的语义是「回退值不跟随 DEFAULT_CONFIG」，所以它必须保持为空；
        # 一旦非空，_settings_fallback_default 就会与页面展示的默认值再次分叉。
        # （不再断言 fallback == DEFAULT_CONFIG：钉死表为空时那些断言恒真，等于没测。）
        self.assertEqual(
            _PINNED_SETTINGS_DEFAULTS, {},
            '钉死表非空会让部分键的回退值脱离 DEFAULT_CONFIG')

        guarded = {key: (lo, hi) for key, lo, hi in SETTINGS_RANGE_GUARDS}
        for key in ('SUBTITLE_MAX_LINE_LENGTH', 'SUBTITLE_MAX_LINES'):
            self.assertIn(key, guarded, f'{key} 缺少服务端范围校验')
            self.assertEqual(
                self.doc.xpath(
                    f"//form[@id='settings-form']//input[@type='hidden'][@name='{key}']/@value"),
                [], f'{key} 不应再被 hidden 输入钉死')
            controls = self.doc.xpath(
                f"//form[@id='settings-form']//input[@type='number'][@name='{key}']")
            self.assertEqual(len(controls), 1, f'{key} 应以唯一的数值控件呈现')
            # 渲染值来自运行机的 config.json，不能假定它等于默认值（环境差异会误失败）；
            # 只要求它是可解析的有限数值，说明控件真的渲染出了值而不是空壳。
            rendered = float(controls[0].get('value'))
            self.assertTrue(math.isfinite(rendered), f'{key} 渲染值不是有限数值: {rendered}')

    def test_数值控件的min_max与服务端guard边界一致(self):
        # 页面声明与服务端 guard 出现两套数字时，用户按页面提示填的值仍会被服务端回退
        # （或反过来：页面拦住了服务端本来允许的值）。同一个键的边界必须是同一个数。
        controls = {
            el.get('name'): el
            for el in self.doc.xpath(
                "//form[@id='settings-form']//input[@type='number'][@name]")
        }
        self.assertTrue(controls, '设置页没有任何数值控件')
        mismatched = {}
        for key, guard_min, guard_max in SETTINGS_RANGE_GUARDS:
            control = controls.get(key)
            if control is None:
                # 无控件键（手工提交才可达）由 tests/test_settings_guards.py 的豁免表覆盖
                continue
            # 按**数值**比较而不是字符串：`2` 与 `2.0`、`0` 与 `0.0` 是同一个边界，
            # 字符串比较会把纯粹的字面写法差异报成不一致（假阳性）。
            declared = (
                _bound_float(control.get('min')),
                _bound_float(control.get('max')),
            )
            expected = (float(guard_min), float(guard_max))
            if declared != expected:
                mismatched[key] = {
                    '页面': (control.get('min'), control.get('max')),
                    'guard': (_bound_text(guard_min), _bound_text(guard_max)),
                }
        self.assertEqual(
            mismatched, {},
            f'数值控件的 min/max 与服务端 guard 边界不一致: {mismatched}')

    def test_JS_toggle依赖的id对在模板中齐全(self):
        # 模板脚本用 {checkbox, input} 配对驱动「勾选开关 → 启用数值框」，
        # 一旦 id 或配对关系被改坏，开关会静默失效（数值框永远 disabled，提交不上值）。
        pairs = re.findall(
            r"\{\s*checkbox:\s*'([^']+)',\s*input:\s*'([^']+)'\s*\}", self.page)
        self.assertTrue(pairs, '未在设置页脚本里找到 toggle 配对表')
        by_id = {el.get('id'): el for el in self.doc.xpath('//*[@id]')}
        problems = []
        for checkbox_id, input_id in pairs:
            checkbox = by_id.get(checkbox_id)
            number = by_id.get(input_id)
            if checkbox is None or number is None:
                problems.append(f'{checkbox_id} / {input_id} 缺少对应元素')
                continue
            if (checkbox.get('type') or '').lower() != 'checkbox':
                problems.append(f'{checkbox_id} 不是复选框')
            if (number.get('type') or '').lower() != 'number':
                problems.append(f'{input_id} 不是数值控件')
            checkbox_name = checkbox.get('name') or ''
            number_name = number.get('name') or ''
            if not checkbox_name.endswith('_ENABLED'):
                problems.append(f'{checkbox_id} 的 name 不以 _ENABLED 结尾: {checkbox_name}')
                continue
            base = checkbox_name[:-len('_ENABLED')]
            if number_name not in (base, f'{base}_S'):
                problems.append(
                    f'{checkbox_id}({checkbox_name}) 与 {input_id}({number_name}) 不是同一字段的开关/数值对')
        self.assertEqual(problems, [], f'toggle 配对回归: {problems}')

    def test_模板兜底字面量与DEFAULT_CONFIG一致(self):
        # 模板里的 config.get('KEY', 字面量) 与 DEFAULT_CONFIG 分叉时,
        # 页面展示的默认值会与后端实际回退值不一致。
        template_source = (
            pathlib.Path(__file__).resolve().parents[1] / 'templates' / 'settings.html'
        ).read_text(encoding='utf-8')
        pattern = re.compile(r"config\.get\(\s*'([A-Z0-9_]+)'\s*,\s*([^)]+?)\s*\)")
        mismatched = {}
        for match in pattern.finditer(template_source):
            key, raw = match.group(1), match.group(2).strip()
            if key not in DEFAULT_CONFIG or raw in ("None", "''", '\"\"', 'True', 'False'):
                continue
            try:
                literal = float(raw)
            except ValueError:
                continue
            if abs(literal - float(DEFAULT_CONFIG[key])) > 1e-9:
                mismatched[key] = (literal, DEFAULT_CONFIG[key])
        self.assertEqual(mismatched, {},
                         f'模板兜底字面量与 DEFAULT_CONFIG 不一致: {mismatched}')


if __name__ == '__main__':
    unittest.main()
