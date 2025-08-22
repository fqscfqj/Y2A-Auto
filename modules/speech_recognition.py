#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import tempfile
import subprocess
import logging
from dataclasses import dataclass
from typing import Optional, Tuple
import re


def _setup_task_logger(task_id: str) -> logging.Logger:
    """Create a task-scoped logger that writes into logs/task_{task_id}.log."""
    from .utils import get_app_subdir
    from logging.handlers import RotatingFileHandler

    log_dir = get_app_subdir('logs')
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger(f'speech_recognition_{task_id}')
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, f'task_{task_id}.log'),
            maxBytes=10485760,
            backupCount=5,
            encoding='utf-8'
        )
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
        logger.propagate = False
    return logger


def _get_openai_client(openai_config: dict):
    """Create an OpenAI client using the same approach as subtitle_translator.py."""
    import openai
    api_key = openai_config.get('OPENAI_API_KEY', '')
    options = {}
    if openai_config.get('OPENAI_BASE_URL'):
        options['base_url'] = openai_config.get('OPENAI_BASE_URL')
    return openai.OpenAI(api_key=api_key, **options)


@dataclass
class SpeechRecognitionConfig:
    provider: str = 'whisper'
    api_key: str = ''
    base_url: str = ''
    model_name: str = 'whisper-1'
    output_format: str = 'srt'  # srt | vtt
    # Separate whisper API for language detection (optional)
    detect_api_key: str = ''
    detect_base_url: str = ''
    detect_model_name: str = ''
    # Gating: treat as no subtitles if cues less than threshold
    min_lines_enabled: bool = True
    min_lines_threshold: int = 5
    # Parakeet API compatibility mode
    parakeet_compatibility_mode: bool = False


class SpeechRecognizer:
    """Abstracted speech recognizer with a Whisper(OpenAI compatible) implementation."""

    def __init__(self, config: SpeechRecognitionConfig, task_id: Optional[str] = None):
        self.config = config
        self.task_id = task_id or 'unknown'
        self.logger = _setup_task_logger(self.task_id)
        self.client = None  # for transcription
        self.detect_client = None  # for language detection
        self._init_client()

    # --- ISO 639-1 language normalization helpers ---
    # Complete ISO 639-1 2-letter code set (including legacy aliases mapped later)
    ISO_639_1_CODES = {
        'aa','ab','ae','af','ak','am','an','ar','as','av','ay','az',
        'ba','be','bg','bh','bi','bm','bn','bo','br','bs',
        'ca','ce','ch','co','cr','cs','cu','cv','cy',
        'da','de','dv','dz',
        'ee','el','en','eo','es','et','eu',
        'fa','ff','fi','fj','fo','fr','fy',
        'ga','gd','gl','gn','gu','gv',
        'ha','he','hi','ho','hr','ht','hu','hy','hz',
        'ia','id','ie','ig','ii','ik','io','is','it','iu',
        'ja','jv',
        'ka','kg','ki','kj','kk','kl','km','kn','ko','kr','ks','ku','kv','kw','ky',
        'la','lb','lg','li','ln','lo','lt','lu','lv',
        'mg','mh','mi','mk','ml','mn','mr','ms','mt','my',
        'na','nb','nd','ne','ng','nl','nn','no','nr','nv','ny',
        'oc','oj','om','or','os',
        'pa','pi','pl','ps','pt',
        'qu',
        'rm','rn','ro','ru','rw',
        'sa','sc','sd','se','sg','si','sk','sl','sm','sn','so','sq','sr','ss','st','su','sv','sw',
        'ta','te','tg','th','ti','tk','tl','tn','to','tr','ts','tt','tw','ty',
        'ug','uk','ur','uz',
        've','vi','vo',
        'wa','wo',
        'xh',
        'yi','yo',
        'za','zh','zu'
    }

    # Common language-name and alias mapping to ISO 639-1 codes (English and a few Chinese names)
    LANG_NAME_TO_ISO639_1 = {
        # Top/common languages
        'english': 'en', 'en-us': 'en', 'en-gb': 'en',
        'chinese': 'zh', 'chinese (simplified)': 'zh', 'chinese (traditional)': 'zh',
        'mandarin': 'zh', 'cantonese': 'zh', 'zh-cn': 'zh', 'zh-hans': 'zh', 'zh-tw': 'zh', 'zh-hant': 'zh',
        'japanese': 'ja', 'jpn': 'ja',
        'korean': 'ko', 'kor': 'ko',
        'spanish': 'es', 'castilian': 'es',
        'french': 'fr', 'francais': 'fr', 'français': 'fr',
        'german': 'de',
        'russian': 'ru',
        'portuguese': 'pt', 'brazilian portuguese': 'pt', 'português': 'pt',
        'italian': 'it',
        'arabic': 'ar',
        'hindi': 'hi',
        'bengali': 'bn', 'bangla': 'bn',
        'urdu': 'ur',
        'turkish': 'tr',
        'vietnamese': 'vi',
        'thai': 'th',
        'indonesian': 'id', 'bahasa indonesia': 'id',
        'malay': 'ms', 'bahasa melayu': 'ms',
        'dutch': 'nl',
        'polish': 'pl',
        'ukrainian': 'uk',
        'romanian': 'ro',
        'greek': 'el', 'modern greek': 'el',
        'czech': 'cs',
        'slovak': 'sk',
        'slovenian': 'sl', 'slovene': 'sl',
        'croatian': 'hr',
        'serbian': 'sr',
        'bosnian': 'bs',
        'bulgarian': 'bg',
        'hungarian': 'hu',
        'finnish': 'fi',
        'swedish': 'sv',
        'norwegian': 'no', 'bokmal': 'nb', 'bokmål': 'nb', 'nynorsk': 'nn',
        'danish': 'da',
        'icelandic': 'is',
        'estonian': 'et',
        'latvian': 'lv',
        'lithuanian': 'lt',
        'hebrew': 'he', 'iw': 'he',
        'yiddish': 'yi', 'ji': 'yi',
        'persian': 'fa', 'farsi': 'fa',
        'pashto': 'ps',
        'kurdish': 'ku',
        'amharic': 'am',
        'swahili': 'sw',
        'afrikaans': 'af',
        'albanian': 'sq',
        'armenian': 'hy',
        'azerbaijani': 'az',
        'basque': 'eu',
        'belarusian': 'be',
        'catalan': 'ca',
        'filipino': 'tl', 'tagalog': 'tl',
        'georgian': 'ka',
        'irish': 'ga',
        'kazakh': 'kk',
        'kyrgyz': 'ky',
        'lao': 'lo',
        'macedonian': 'mk',
        'mongolian': 'mn',
        'nepali': 'ne',
        'sinhala': 'si', 'sinhalese': 'si',
        'somali': 'so',
        'tamil': 'ta',
        'telugu': 'te',
        'tatar': 'tt',
        'tigrinya': 'ti',
        'turkmen': 'tk',
        'uzbek': 'uz',
        'welsh': 'cy',
        'yoruba': 'yo',
        'zulu': 'zu',
        'xhosa': 'xh',
        # Chinese names (简体/繁体)
        '中文': 'zh', '中文（简体）': 'zh', '简体中文': 'zh', '中文（繁體）': 'zh', '繁体中文': 'zh', '普通话': 'zh', '粤语': 'zh',
        # Legacy 2-letter aliases used historically
        'in': 'id', 'jw': 'jv'
    }

    # Some common ISO 639-3 or legacy codes to ISO 639-1
    ISO_639_3_TO_1 = {
        'zho': 'zh', 'chi': 'zh', 'eng': 'en', 'jpn': 'ja', 'kor': 'ko', 'fra': 'fr', 'fre': 'fr',
        'deu': 'de', 'ger': 'de', 'spa': 'es', 'ita': 'it', 'rus': 'ru', 'por': 'pt', 'hin': 'hi',
        'ben': 'bn', 'urd': 'ur', 'tur': 'tr', 'vie': 'vi', 'tha': 'th', 'ind': 'id', 'msa': 'ms', 'may': 'ms',
        'nld': 'nl', 'dut': 'nl', 'pol': 'pl', 'ukr': 'uk', 'ron': 'ro', 'rum': 'ro', 'ell': 'el', 'gre': 'el',
        'ces': 'cs', 'cze': 'cs', 'slk': 'sk', 'slo': 'sk', 'slv': 'sl', 'hrv': 'hr', 'srp': 'sr', 'bos': 'bs',
        'bul': 'bg', 'hun': 'hu', 'fin': 'fi', 'swe': 'sv', 'nor': 'no', 'dan': 'da', 'isl': 'is', 'est': 'et',
        'lav': 'lv', 'lit': 'lt', 'heb': 'he', 'yid': 'yi', 'fas': 'fa', 'pus': 'ps', 'kur': 'ku', 'amh': 'am',
        'swa': 'sw', 'afr': 'af', 'sqi': 'sq', 'hye': 'hy', 'aze': 'az', 'eus': 'eu', 'bel': 'be', 'cat': 'ca',
        'tgl': 'tl', 'gle': 'ga', 'kat': 'ka', 'kaz': 'kk', 'kir': 'ky', 'lao': 'lo', 'mkd': 'mk', 'mon': 'mn',
        'nep': 'ne', 'sin': 'si', 'som': 'so', 'tam': 'ta', 'tel': 'te', 'tat': 'tt', 'tir': 'ti', 'tuk': 'tk',
        'uzb': 'uz', 'cym': 'cy', 'yor': 'yo', 'zul': 'zu', 'xho': 'xh', 'cmn': 'zh', 'yue': 'zh'
    }

    @classmethod
    def _normalize_language_to_iso639_1(cls, lang: Optional[str]) -> Optional[str]:
        """Normalize various language inputs to ISO 639-1 2-letter code.

        Accepts codes (en, zh, zh-CN, en-US), names (English, 中文), and some 3-letter/legacy codes.
        Returns lowercased 2-letter code if resolvable; otherwise None.
        """
        if not lang:
            return None
        # Basic cleanup
        s = str(lang).strip().lower()
        if not s:
            return None
        # If pure 2-letter code and valid
        if len(s) == 2 and s in cls.ISO_639_1_CODES:
            return s
        # Legacy aliases that are already 2 letters
        if s in ('iw', 'ji', 'in', 'jw'):
            return {'iw': 'he', 'ji': 'yi', 'in': 'id', 'jw': 'jv'}[s]

        # Normalize BCP-47 like tags (e.g., zh-CN, en_US)
        s_tag = re.sub(r'[ _]', '-', s)
        if '-' in s_tag:
            primary = s_tag.split('-', 1)[0]
            if len(primary) == 2 and primary in cls.ISO_639_1_CODES:
                return primary
            # Handle zh-Hans/zh-Hant etc.
            if primary in ('zh', 'cmn', 'yue'):
                return 'zh'

        # Map common names/aliases
        if s in cls.LANG_NAME_TO_ISO639_1:
            return cls.LANG_NAME_TO_ISO639_1[s]

        # 3-letter to 2-letter
        if len(s) == 3 and s in cls.ISO_639_3_TO_1:
            return cls.ISO_639_3_TO_1[s]

        # Last attempt: if someone passed something like "english (us)"
        s_base = re.split(r'[()\-_/ ]+', s)[0]
        if s_base in cls.LANG_NAME_TO_ISO639_1:
            return cls.LANG_NAME_TO_ISO639_1[s_base]

        return None

    def _init_client(self):
        try:
            if self.config.provider != 'whisper':
                self.logger.error(f"暂不支持的语音识别提供商: {self.config.provider}")
                return
            if not self.config.api_key:
                self.logger.error("缺少Whisper/OpenAI API密钥")
                return
            openai_config = {
                'OPENAI_API_KEY': self.config.api_key,
                'OPENAI_BASE_URL': self.config.base_url,
            }
            self.client = _get_openai_client(openai_config)
            # setup detect client (fallback to main if not provided)
            detect_api_key = self.config.detect_api_key or self.config.api_key
            detect_base_url = self.config.detect_base_url or self.config.base_url
            detect_openai_config = {
                'OPENAI_API_KEY': detect_api_key,
                'OPENAI_BASE_URL': detect_base_url,
            }
            self.detect_client = _get_openai_client(detect_openai_config)
            mode_info = " (parakeet兼容模式)" if self.config.parakeet_compatibility_mode else ""
            self.logger.info(f"语音识别客户端初始化成功(含语言检测){mode_info}")
        except Exception as e:
            self.logger.error(f"初始化语音识别客户端失败: {e}")

    def _extract_audio_wav(self, video_path: str) -> Optional[str]:
        """Extract 16kHz mono WAV from video using ffmpeg. Returns temp file path."""
        try:
            tmp_dir = tempfile.mkdtemp(prefix='y2a_audio_')
            audio_path = os.path.join(tmp_dir, 'audio.wav')
            cmd = [
                'ffmpeg', '-y',
                '-i', video_path,
                '-vn',
                '-ac', '1',
                '-ar', '16000',
                '-f', 'wav',
                audio_path
            ]
            self.logger.info(f"提取音频: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=600
            )
            if result.returncode != 0 or not os.path.exists(audio_path):
                self.logger.error(f"提取音频失败: {result.stderr}\n{result.stdout}")
                return None
            return audio_path
        except Exception as e:
            self.logger.error(f"提取音频异常: {e}")
            return None

    def _extract_audio_wav_clip(self, video_path: str, seconds: int = 60) -> Optional[str]:
        """Extract only the first N seconds of audio as 16kHz mono WAV for fast language detection."""
        try:
            tmp_dir = tempfile.mkdtemp(prefix='y2a_audio_clip_')
            audio_path = os.path.join(tmp_dir, f'audio_first_{seconds}s.wav')
            cmd = [
                'ffmpeg', '-y',
                '-i', video_path,
                '-t', str(seconds),
                '-vn',
                '-ac', '1',
                '-ar', '16000',
                '-f', 'wav',
                audio_path
            ]
            self.logger.info(f"提取音频片段用于语言检测: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=180
            )
            if result.returncode != 0 or not os.path.exists(audio_path):
                self.logger.warning(f"提取音频片段失败，将跳过语言检测: {result.stderr}\n{result.stdout}")
                return None
            return audio_path
        except Exception as e:
            self.logger.warning(f"提取语言检测音频片段异常，将跳过语言检测: {e}")
            return None

    def transcribe_video_to_subtitles(self, video_path: str, output_path: str) -> Optional[str]:
        """
        Transcribe a video file into SRT/VTT subtitles using Whisper-compatible API.

        Returns the output_path on success, otherwise None.
        """
        try:
            if not self.client:
                self.logger.error("语音识别客户端未初始化")
                return None
            if not os.path.exists(video_path):
                self.logger.error(f"视频文件不存在: {video_path}")
                return None

            # 先用前60秒做语言检测（若失败则忽略，进入自动语言识别；若明确不支持语言，则直接停止）
            detected_language = None
            try:
                clip_wav = self._extract_audio_wav_clip(video_path, seconds=60)
                if clip_wav:
                    detected_language, confidence = self._detect_language(clip_wav)
                    if detected_language:
                        self.logger.info(f"语言检测结果: {detected_language} (confidence={confidence})")
            except SpeechRecognizer.UnsupportedLanguageError as e:
                self.logger.warning(f"语言检测被明确拒绝（不支持语言），将停止转写：{e}")
                return None
            except SpeechRecognizer.ServerFatalError as e:
                self.logger.warning(f"语言检测出现服务端错误(500)，将停止转写：{e}")
                return None
            except Exception as e:
                self.logger.warning(f"语言检测失败，继续自动识别: {e}")

            # 再提取整段音频进行完整转写
            audio_wav = self._extract_audio_wav(video_path)
            if not audio_wav:
                return None

            model_name = self.config.model_name or 'whisper-1'
            response_format = (self.config.output_format or 'srt').lower()
            if response_format not in ('srt', 'vtt'):
                response_format = 'srt'

            self.logger.info(f"开始语音转写，模型: {model_name}，输出格式: {response_format}")
            with open(audio_wav, 'rb') as f:
                # OpenAI new SDK: returns string when response_format is text-like
                kwargs = {
                    'model': model_name,
                    'file': f,
                    'response_format': response_format,
                }
                if detected_language:
                    # Provide language hint to improve accuracy and speed (normalize to ISO 639-1 code)
                    lang_code = self._normalize_language_to_iso639_1(detected_language)
                    if lang_code:
                        kwargs['language'] = lang_code

                try:
                    resp = self.client.audio.transcriptions.create(**kwargs)
                except Exception as e:
                    # If server rejects unsupported language, stop without retrying
                    msg = str(e)
                    if (
                        ('Unsupported language' in msg)
                        or ('unsupported_language' in msg)
                        or ('语言不支持' in msg)
                        or ('不支持的语言' in msg)
                        or ('param' in msg and 'language' in msg)
                    ):
                        bad_lang = kwargs.get('language', detected_language)
                        self.logger.warning(
                            f"语音转写被拒绝：不支持的语言（{bad_lang}）。将停止转写且不再重试。原始错误：{msg}"
                        )
                        return None
                    # Other errors will bubble to outer except
                    else:
                        raise

            # Parse response using compatibility-aware method
            text = self._parse_transcription_response(resp, kwargs)

            if not text or len(text.strip()) == 0:
                self.logger.error("语音转写API返回为空")
                return None

            # Ensure VTT header if needed
            if response_format == 'vtt' and not text.lstrip().upper().startswith('WEBVTT'):
                text = 'WEBVTT\n\n' + text

            with open(output_path, 'w', encoding='utf-8') as out:
                out.write(text)

            # Post-check: count subtitle cues; if below threshold and gating enabled, treat as no subtitles
            try:
                if self.config.min_lines_enabled:
                    cues = self._count_subtitle_cues(output_path, response_format)
                    self.logger.info(f"ASR字幕条目数: {cues}")
                    if isinstance(cues, int) and cues < max(0, int(self.config.min_lines_threshold)):
                        try:
                            os.remove(output_path)
                        except Exception:
                            pass
                        self.logger.info(
                            f"ASR字幕少于阈值({self.config.min_lines_threshold})，视为无字幕，丢弃文件")
                        return None
            except Exception as _e:
                self.logger.warning(f"ASR字幕条目数统计失败，忽略门槛检查: {_e}")

            self.logger.info(f"语音转写完成，字幕已保存: {output_path}")
            return output_path
        except Exception as e:
            self.logger.error(f"语音转写失败: {e}")
            return None

    def _parse_transcription_response(self, resp, kwargs: Optional[dict] = None) -> Optional[str]:
        """
        Parse transcription response with support for multiple API formats.
        Handles both OpenAI standard format and parakeet-api-docker format.
        
        Args:
            resp: Response from API call
            kwargs: Original request parameters for error context
            
        Returns:
            Transcription text or None if error/empty
        """
        kwargs = kwargs or {}
        
        # Handle SDK variations: sometimes resp is str, sometimes has .text
        text = None
        try:
            # Some compatible servers may return an error payload instead of raising
            # Normalize and early-stop if an error is present
            if isinstance(resp, dict) and 'error' in resp:
                err = resp.get('error')
                # Try to identify unsupported language specifically
                if isinstance(err, dict):
                    code = str(err.get('code', '')).lower()
                    etype = str(err.get('type', '')).lower()
                    message = str(err.get('message', ''))
                    param = str(err.get('param', ''))
                    if (
                        'unsupported_language' in code
                        or 'unsupported language' in message
                        or ('language' == param)
                    ):
                        bad_lang = kwargs.get('language', 'unknown')
                        self.logger.warning(
                            f"语音转写被拒绝：不支持的语言（{bad_lang}）。将停止转写且不再重试。错误：{err}"
                        )
                        return None
                self.logger.error(f"语音转写失败：{err}")
                return None
                
            # Parakeet compatibility: check for parakeet-specific response format
            if self.config.parakeet_compatibility_mode and isinstance(resp, dict):
                # Handle parakeet-api-docker specific response structure
                if 'text' in resp:
                    text = resp['text']
                    self.logger.debug("使用parakeet兼容模式解析响应：找到text字段")
                elif 'result' in resp:
                    result = resp['result']
                    if isinstance(result, dict) and 'text' in result:
                        text = result['text']
                        self.logger.debug("使用parakeet兼容模式解析响应：找到result.text字段")
                    elif isinstance(result, str):
                        text = result
                        self.logger.debug("使用parakeet兼容模式解析响应：使用result字符串")
                elif 'transcription' in resp:
                    transcription = resp['transcription']
                    if isinstance(transcription, str):
                        text = transcription
                        self.logger.debug("使用parakeet兼容模式解析响应：找到transcription字段")
            
            # Standard OpenAI response handling
            if not text:
                if isinstance(resp, str):
                    # Try parse JSON error
                    import json as _json
                    try:
                        maybe = _json.loads(resp)
                        if isinstance(maybe, dict) and 'error' in maybe:
                            err = maybe.get('error')
                            if isinstance(err, dict):
                                code = str(err.get('code', '')).lower()
                                etype = str(err.get('type', '')).lower()
                                message = str(err.get('message', ''))
                                param = str(err.get('param', ''))
                                if (
                                    'unsupported_language' in code
                                    or 'unsupported language' in message
                                    or ('language' == param)
                                ):
                                    bad_lang = kwargs.get('language', 'unknown')
                                    self.logger.warning(
                                        f"语音转写被拒绝：不支持的语言（{bad_lang}）。将停止转写且不再重试。错误：{err}"
                                    )
                                    return None
                            self.logger.error(f"语音转写失败：{err}")
                            return None
                    except Exception:
                        pass
                    text = resp
                elif not isinstance(resp, dict) and hasattr(resp, 'text'):
                    text = getattr(resp, 'text', None)
                else:
                    # Fallback to string representation
                    text = str(resp)
        except Exception as e:
            self.logger.warning(f"解析转写响应时出错: {e}")
            text = None

        return text if text and len(text.strip()) > 0 else None

    class UnsupportedLanguageError(Exception):
        pass

    class ServerFatalError(Exception):
        pass

    def _detect_language(self, audio_wav_path: str) -> Tuple[Optional[str], Optional[float]]:
        """Detect language using Whisper-compatible API via verbose_json.

        Returns (language_code, confidence) if available, else (None, None).
        """
        try:
            if not self.detect_client:
                return None, None
            detect_model = self.config.detect_model_name or self.config.model_name or 'whisper-1'
            with open(audio_wav_path, 'rb') as f:
                try:
                    resp = self.detect_client.audio.transcriptions.create(
                        model=detect_model,
                        file=f,
                        response_format='verbose_json'
                    )
                except Exception as e:
                    # 如果返回明确不支持语言或 500 服务端错误，则直接终止
                    msg = str(e)
                    if (
                        ('Unsupported language' in msg)
                        or ('unsupported_language' in msg)
                        or ('语言不支持' in msg)
                        or ('不支持的语言' in msg)
                        or ('param' in msg and 'language' in msg)
                    ):
                        self.logger.warning(
                            f"语言检测被拒绝：不支持的语言。原始错误：{msg}"
                        )
                        raise SpeechRecognizer.UnsupportedLanguageError(msg)
                    if (
                        'status code 500' in msg.lower()
                        or 'error code: 500' in msg.lower()
                        or 'unknown_error' in msg.lower()
                    ):
                        self.logger.warning(
                            f"语言检测出现服务端错误(500)，将停止转写。原始错误：{msg}"
                        )
                        raise SpeechRecognizer.ServerFatalError(msg)
                    # 其他错误走原有流程，由上层忽略检测失败
                    raise
            
            # Use compatibility-aware parsing
            return self._parse_language_detection_response(resp)
            
        except Exception as e:
            # 明确不支持语言或服务端致命错误时向上传递，其他异常仅告警
            if isinstance(e, (SpeechRecognizer.UnsupportedLanguageError, SpeechRecognizer.ServerFatalError)):
                raise
            self.logger.warning(f"语言检测异常: {e}")
            return None, None

    def _parse_language_detection_response(self, resp) -> Tuple[Optional[str], Optional[float]]:
        """
        Parse language detection response with support for multiple API formats.
        Handles both OpenAI standard format and parakeet-api-docker format.
        
        Returns:
            Tuple of (language_code, confidence)
        """
        # Some SDKs return dict-like, some return object; normalize
        data = None
        if isinstance(resp, dict):
            # 检查错误载荷
            if 'error' in resp:
                err = resp.get('error')
                if isinstance(err, dict):
                    code = str(err.get('code', '')).lower()
                    message = str(err.get('message', ''))
                    param = str(err.get('param', ''))
                    if (
                        'unsupported_language' in code
                        or 'unsupported language' in message
                        or ('language' == param)
                    ):
                        self.logger.warning(f"语言检测被拒绝：不支持的语言。错误：{err}")
                        raise SpeechRecognizer.UnsupportedLanguageError(str(err))
                    if (
                        'unknown_error' in code
                        or 'status code 500' in message.lower()
                        or '500' == code
                    ):
                        self.logger.warning(f"语言检测出现服务端错误(500)。错误：{err}")
                        raise SpeechRecognizer.ServerFatalError(str(err))
                        
            # Parakeet compatibility: check for parakeet-specific response format
            if self.config.parakeet_compatibility_mode:
                # Handle parakeet-api-docker specific language detection response
                if 'language' in resp and 'confidence' in resp:
                    # Direct language/confidence format
                    data = resp
                    self.logger.debug("使用parakeet兼容模式解析语言检测响应：直接格式")
                elif 'result' in resp:
                    result = resp['result']
                    if isinstance(result, dict):
                        data = result
                        self.logger.debug("使用parakeet兼容模式解析语言检测响应：result对象")
                elif 'detection' in resp:
                    detection = resp['detection']
                    if isinstance(detection, dict):
                        data = detection
                        self.logger.debug("使用parakeet兼容模式解析语言检测响应：detection对象")
                        
            # Fall back to standard response if parakeet parsing didn't work
            if not data:
                data = resp
        else:
            try:
                # OpenAI SDK returns pydantic models; use .model_dump if available, else vars()
                if hasattr(resp, 'model_dump'):
                    data = resp.model_dump()
                elif hasattr(resp, 'to_dict'):
                    data = resp.to_dict()
                else:
                    data = getattr(resp, '__dict__', None)
            except Exception:
                data = None

        if not data:
            # Last resort, try to parse as string JSON-like (unlikely)
            try:
                import json as _json
                data = _json.loads(str(resp))
                if isinstance(data, dict) and 'error' in data:
                    err = data.get('error')
                    if isinstance(err, dict):
                        code = str(err.get('code', '')).lower()
                        message = str(err.get('message', ''))
                        param = str(err.get('param', ''))
                        if (
                            'unsupported_language' in code
                            or 'unsupported language' in message
                            or ('language' == param)
                        ):
                            self.logger.warning(f"语言检测被拒绝：不支持的语言。错误：{err}")
                            raise SpeechRecognizer.UnsupportedLanguageError(str(err))
                        if (
                            'unknown_error' in code
                            or 'status code 500' in message.lower()
                            or code == '500'
                        ):
                            self.logger.warning(f"语言检测出现服务端错误(500)。错误：{err}")
                            raise SpeechRecognizer.ServerFatalError(str(err))
            except Exception:
                data = None

        language = None
        confidence = None
        if isinstance(data, dict):
            # Standard OpenAI format
            language = data.get('language') or data.get('detected_language')
            
            # Parakeet compatibility: check alternative field names
            if not language and self.config.parakeet_compatibility_mode:
                language = data.get('lang') or data.get('language_code') or data.get('predicted_language')
                
            # try to derive confidence from avg segment probs
            segs = data.get('segments') or []
            if isinstance(segs, list) and segs:
                probs = []
                for s in segs:
                    p = s.get('avg_logprob') if isinstance(s, dict) else None
                    if isinstance(p, (int, float)):
                        probs.append(p)
                if probs:
                    # Convert logprob to a rough [0,1] scale (heuristic)
                    import math as _math
                    confidence = sum(_math.exp(p) for p in probs) / len(probs)
            
            # Parakeet compatibility: check for direct confidence field
            if confidence is None and self.config.parakeet_compatibility_mode:
                confidence = data.get('confidence') or data.get('score') or data.get('probability')
                
        # Normalize language code (e.g., 'zh', 'en')
        if language is not None:
            lang_str = str(language) if isinstance(language, (str, bytes)) else None
            language = self._normalize_language_to_iso639_1(lang_str) if lang_str else None
        # ensure precise types for return
        lang_ret: Optional[str] = language if isinstance(language, str) and language else None
        conf_ret: Optional[float] = confidence if isinstance(confidence, (int, float)) else None
        return lang_ret, conf_ret

    def _count_subtitle_cues(self, file_path: str, fmt: str) -> Optional[int]:
        """Count number of subtitle cues in SRT or VTT.

        Args:
            file_path: path to subtitle file
            fmt: 'srt' or 'vtt'
        Returns:
            int or None on parse error
        """
        try:
            fmt_l = (fmt or '').lower()
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            if fmt_l == 'srt':
                # Split blocks by blank lines and count blocks that have a time range
                blocks = re.split(r"\n\s*\n", content.strip())
                cnt = 0
                for b in blocks:
                    if '-->' in b:
                        cnt += 1
                return cnt
            else:
                # VTT: ensure header removed then count cue lines with -->
                # Remove header WEBVTT lines
                body = re.sub(r'^WEBVTT.*?\n+', '', content, flags=re.IGNORECASE | re.DOTALL)
                # Split by blank lines; count blocks containing a --> line
                blocks = re.split(r"\n\s*\n", body.strip())
                cnt = 0
                for b in blocks:
                    if '-->' in b:
                        cnt += 1
                return cnt
        except Exception:
            return None


def create_speech_recognizer_from_config(app_config: dict, task_id: Optional[str] = None) -> Optional[SpeechRecognizer]:
    """Factory: Build recognizer from app settings."""
    try:
        if not app_config.get('SPEECH_RECOGNITION_ENABLED', False):
            return None

        provider = (app_config.get('SPEECH_RECOGNITION_PROVIDER') or 'whisper').lower()
        output_format = (app_config.get('SPEECH_RECOGNITION_OUTPUT_FORMAT') or 'srt').lower()

        # Prefer dedicated Whisper settings; fallback to general OpenAI settings
        whisper_base_url = app_config.get('WHISPER_BASE_URL') or app_config.get('OPENAI_BASE_URL', 'https://api.openai.com/v1')
        whisper_api_key = app_config.get('WHISPER_API_KEY') or app_config.get('OPENAI_API_KEY', '')
        whisper_model = app_config.get('WHISPER_MODEL_NAME') or 'whisper-1'

        config = SpeechRecognitionConfig(
            provider=provider,
            api_key=whisper_api_key,
            base_url=whisper_base_url,
            model_name=whisper_model,
            output_format=output_format,
            # dedicated detect settings (optional)
            detect_api_key=app_config.get('WHISPER_DETECT_API_KEY', ''),
            detect_base_url=app_config.get('WHISPER_DETECT_BASE_URL', ''),
            detect_model_name=app_config.get('WHISPER_DETECT_MODEL_NAME', ''),
            min_lines_enabled=app_config.get('SPEECH_RECOGNITION_MIN_SUBTITLE_LINES_ENABLED', True),
            min_lines_threshold=int(app_config.get('SPEECH_RECOGNITION_MIN_SUBTITLE_LINES', 5) or 0),
            # parakeet compatibility mode
            parakeet_compatibility_mode=app_config.get('WHISPER_PARAKEET_COMPATIBILITY_MODE', False)
        )
        return SpeechRecognizer(config, task_id)
    except Exception:
        return None


