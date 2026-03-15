#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

from .subtitle_pipeline_types import (
    AsrSegmentTiming,
    AsrTranscriptionResult,
    AsrWordTiming,
    DetectedSpeechWindow,
)


_LATIN_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)?")
_CJK_CHAR_RE = re.compile(r'[\u3400-\u9fff]')
_VISIBLE_TEXT_RE = re.compile(r'[\w\u3400-\u9fff]', re.UNICODE)
_INVALID_DURATION_FALLBACK = 0.5


def _format_srt_timestamp(seconds: float) -> str:
    total_millis = int(round(float(seconds or 0.0) * 1000))
    hours = total_millis // 3_600_000
    remaining = total_millis % 3_600_000
    minutes = remaining // 60_000
    remaining %= 60_000
    secs = remaining // 1000
    millis = remaining % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


@dataclass
class AsrConfig:
    provider: str = 'whisper'
    api_key: str = ''
    base_url: str = ''
    model_name: str = 'whisper-1'
    language: str = ''
    prompt: str = ''
    translate: bool = False
    timestamp_granularities: str = 'segment,word'
    diarize: bool = False
    context_bias: str = ''
    max_retries: int = 3
    retry_delay_s: float = 2.0
    max_workers: int = 3
    request_timeout_s: float = 300.0
    voxtral_max_audio_duration_s: float = 10800.0
    voxtral_enforce_max_duration: bool = True
    word_timestamps_enabled: bool = True


@dataclass
class _AsrCapabilityCache:
    transcription_format: Optional[str] = None
    language_detection_format: Optional[str] = None
    transcription_granularities: Tuple[str, ...] = field(default_factory=tuple)


@dataclass
class _AsrCapabilityProbeResult:
    transcription_format: Optional[str] = None
    language_detection_format: Optional[str] = None
    transcription_granularities: Tuple[str, ...] = field(default_factory=tuple)
    transcription_result: Optional[AsrTranscriptionResult] = None
    language_data: Optional[Dict[str, Any]] = None


class AsrFormatIncompatibleError(RuntimeError):
    pass


class ImplausibleAsrResultError(RuntimeError):
    pass


class AsrApiClient:
    def __init__(self, config: AsrConfig, logger: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.client: Any = None
        self._language_hint: str = ''
        self._capability_cache = _AsrCapabilityCache()
        self._capability_probe_condition = threading.Condition()
        self._capability_probe_in_progress = False
        self._capability_probe_incompatible = False
        self._logged_capability_signature: Optional[Tuple[str, str, Tuple[str, ...]]] = None
        self._init_client()

    def set_language_hint(self, lang: str):
        self._language_hint = str(lang or '').strip()

    def _init_client(self):
        if not self.config.api_key and self.config.provider not in ('fireredasr2s',):
            self.logger.error("Missing ASR API key - ASR client not initialised")
            return
        try:
            if self.config.provider in ('fireredasr2s', 'voxtral'):
                if not self.config.base_url:
                    self.logger.error("Missing ASR base URL - ASR client not initialised")
                    return
                self.client = True
                self.logger.info("%s client initialised successfully", self.config.provider)
                return

            import openai

            opts: Dict[str, Any] = {}
            if self.config.base_url:
                opts['base_url'] = self.config.base_url
            self.client = openai.OpenAI(api_key=self.config.api_key, **opts)
            self.logger.info("ASR API client initialised successfully")
        except Exception as exc:
            self.logger.error("Failed to initialise ASR API client: %s", exc)

    @staticmethod
    def _is_format_error(exc: Exception) -> bool:
        err_str = str(exc).lower()
        return (
            'response_format' in err_str
            or 'timestamp_granularities' in err_str
            or 'unsupported format' in err_str
            or ('format' in err_str and ('not supported' in err_str or 'invalid' in err_str))
        )

    @staticmethod
    def _text_density_metrics(text: str) -> Tuple[int, int]:
        normalized = str(text or '').strip()
        if not normalized:
            return 0, 0
        visible_chars = len(_VISIBLE_TEXT_RE.findall(normalized))
        word_like_units = len(_LATIN_WORD_RE.findall(normalized)) + len(_CJK_CHAR_RE.findall(normalized))
        return visible_chars, word_like_units

    @classmethod
    def _is_implausible_for_duration(cls, text: str, duration_s: float) -> bool:
        normalized = str(text or '').strip()
        if not normalized:
            return False
        visible_chars, word_like_units = cls._text_density_metrics(normalized)
        safe_duration = max(float(duration_s or 0.0), 0.1)
        chars_per_second = visible_chars / safe_duration
        units_per_second = word_like_units / safe_duration
        if safe_duration < 8.0 and visible_chars > 280:
            return True
        if safe_duration < 15.0 and visible_chars > 420:
            return True
        if chars_per_second > 45.0 or units_per_second > 8.0:
            return True
        return False

    def _cache_capabilities(
        self,
        *,
        transcription_fmt: Optional[str],
        language_detection_fmt: Optional[str],
        transcription_granularities: Tuple[str, ...],
    ):
        with self._capability_probe_condition:
            if transcription_fmt:
                self._capability_cache.transcription_format = transcription_fmt
            if language_detection_fmt:
                self._capability_cache.language_detection_format = language_detection_fmt
            self._capability_cache.transcription_granularities = tuple(transcription_granularities or ())
            self._capability_probe_incompatible = False
            signature = (
                self._capability_cache.transcription_format or '',
                self._capability_cache.language_detection_format or '',
                tuple(self._capability_cache.transcription_granularities),
            )
            if signature != self._logged_capability_signature:
                self._logged_capability_signature = signature
                self.logger.info(
                    "Cached ASR response mode: format=%s, language_detection=%s, granularity=%s",
                    signature[0],
                    signature[1],
                    ','.join(signature[2]) if signature[2] else 'none',
                )

    def _invalidate_capabilities(self):
        with self._capability_probe_condition:
            self._capability_cache = _AsrCapabilityCache()
            self._capability_probe_incompatible = False
            self._logged_capability_signature = None

    def _needs_serial_format_probe(self) -> bool:
        with self._capability_probe_condition:
            return not self._capability_cache.transcription_format and not self._capability_probe_incompatible

    def _build_incompatible_error(self) -> RuntimeError:
        return AsrFormatIncompatibleError(
            "ASR API incompatible: unable to negotiate a supported transcription format."
        )

    def _parse_requested_granularities(self) -> Tuple[str, ...]:
        raw = str(self.config.timestamp_granularities or '').strip()
        if not raw:
            return ('segment', 'word') if self.config.word_timestamps_enabled else ('segment',)
        allowed = {'segment', 'word'}
        result: List[str] = []
        for part in raw.replace('\n', ',').split(','):
            token = part.strip().lower()
            if token in allowed and token not in result:
                result.append(token)
        if not result:
            return ('segment', 'word') if self.config.word_timestamps_enabled else ('segment',)
        if self.config.word_timestamps_enabled and 'word' not in result:
            result.append('word')
        if 'segment' not in result:
            result.insert(0, 'segment')
        return tuple(result)

    def _whisper_granularity_candidates(self) -> List[Tuple[str, ...]]:
        requested = self._parse_requested_granularities()
        candidates: List[Tuple[str, ...]] = []
        if requested:
            candidates.append(requested)
        if 'word' in requested:
            candidates.append(tuple(gran for gran in requested if gran != 'word'))
        if ('segment',) not in candidates:
            candidates.append(('segment',))
        candidates.append(tuple())
        deduped: List[Tuple[str, ...]] = []
        for candidate in candidates:
            normalized = tuple(gran for gran in candidate if gran)
            if normalized not in deduped:
                deduped.append(normalized)
        return deduped

    def _probe_capabilities(
        self,
        wav_path: str,
        model: str,
        *,
        window: Optional[DetectedSpeechWindow],
        include_language_hint: bool = True,
        include_prompt: bool = True,
    ) -> _AsrCapabilityProbeResult:
        if self.config.provider == 'voxtral':
            result = self._transcribe_segment_voxtral(
                wav_path,
                window=window,
                granularity_candidates=self._voxtral_granularity_candidates(),
                include_language_hint=include_language_hint,
            )
            if result and result.ok:
                return _AsrCapabilityProbeResult(
                    transcription_format='verbose_json',
                    language_detection_format='json',
                    transcription_granularities=tuple(result.metadata.get('granularities') or ()),
                    transcription_result=result,
                    language_data={'language': result.language} if result.language else None,
                )
            raise self._build_incompatible_error()

        format_errors: List[Exception] = []
        for granularities in self._whisper_granularity_candidates():
            try:
                response = self._request_whisper_response(
                    wav_path,
                    model,
                    'verbose_json',
                    granularities=granularities,
                    include_language_hint=include_language_hint,
                    include_prompt=include_prompt,
                )
                payload = self._as_dict(response) or {}
                result = self._payload_to_transcription_result(
                    payload,
                    provider='whisper',
                    response_format='verbose_json',
                    timestamp_mode='word' if self._payload_has_words(payload) else 'segment',
                    window=window,
                    granularities=granularities,
                )
                if result.ok:
                    return _AsrCapabilityProbeResult(
                        transcription_format='verbose_json',
                        language_detection_format='verbose_json',
                        transcription_granularities=granularities,
                        transcription_result=result,
                        language_data=payload,
                    )
            except Exception as exc:
                if self._is_format_error(exc):
                    format_errors.append(exc)
                    continue
                raise

        try:
            srt_text = self._extract_text(
                self._request_whisper_response(
                    wav_path,
                    model,
                    'srt',
                    granularities=tuple(),
                    include_language_hint=include_language_hint,
                    include_prompt=include_prompt,
                )
            )
            if srt_text:
                return _AsrCapabilityProbeResult(
                    transcription_format='srt',
                    language_detection_format='json',
                    transcription_granularities=tuple(),
                    transcription_result=AsrTranscriptionResult(
                        provider='whisper',
                        response_format='srt',
                        timestamp_mode='srt',
                        text=srt_text,
                        window=window,
                        failure_token='asr_no_timestamps',
                    ),
                )
        except Exception as exc:
            if self._is_format_error(exc):
                format_errors.append(exc)
            else:
                raise

        if format_errors:
            raise self._build_incompatible_error()
        return _AsrCapabilityProbeResult()

    def _get_or_probe_capabilities(
        self,
        wav_path: str,
        model: str,
        *,
        window: Optional[DetectedSpeechWindow],
        include_language_hint: bool = True,
        include_prompt: bool = True,
    ) -> _AsrCapabilityProbeResult:
        while True:
            with self._capability_probe_condition:
                if self._capability_cache.transcription_format:
                    return _AsrCapabilityProbeResult(
                        transcription_format=self._capability_cache.transcription_format,
                        language_detection_format=self._capability_cache.language_detection_format,
                        transcription_granularities=self._capability_cache.transcription_granularities,
                    )
                if self._capability_probe_incompatible:
                    raise self._build_incompatible_error()
                if self._capability_probe_in_progress:
                    self._capability_probe_condition.wait()
                    continue
                self._capability_probe_in_progress = True
                break

        try:
            probe_result = self._probe_capabilities(
                wav_path,
                model,
                window=window,
                include_language_hint=include_language_hint,
                include_prompt=include_prompt,
            )
        except AsrFormatIncompatibleError:
            with self._capability_probe_condition:
                self._capability_probe_in_progress = False
                self._capability_probe_incompatible = True
                self._capability_probe_condition.notify_all()
            raise
        except Exception:
            with self._capability_probe_condition:
                self._capability_probe_in_progress = False
                self._capability_probe_condition.notify_all()
            raise

        with self._capability_probe_condition:
            self._capability_probe_in_progress = False
            self._capability_probe_condition.notify_all()
        self._cache_capabilities(
            transcription_fmt=probe_result.transcription_format,
            language_detection_fmt=probe_result.language_detection_format,
            transcription_granularities=probe_result.transcription_granularities,
        )
        return probe_result

    def _request_whisper_response(
        self,
        wav_path: str,
        model: str,
        response_format: str,
        *,
        granularities: Tuple[str, ...],
        temperature: Optional[float] = None,
        include_language_hint: bool = True,
        include_prompt: bool = True,
        use_translation_endpoint: Optional[bool] = None,
    ):
        with open(wav_path, 'rb') as file_obj:
            params: Dict[str, Any] = {
                'model': model,
                'file': file_obj,
                'response_format': response_format,
            }
            if temperature is not None:
                params['temperature'] = temperature
            if include_language_hint:
                language = self._language_hint or self.config.language
                if language and language.lower() != 'unknown':
                    params['language'] = language
            if include_prompt:
                prompt = str(self.config.prompt or '').strip()
                if prompt:
                    params['prompt'] = prompt
            if response_format == 'verbose_json' and granularities:
                params['timestamp_granularities'] = list(granularities)

            if use_translation_endpoint is None:
                use_translation_endpoint = bool(self.config.translate)
            if use_translation_endpoint:
                return self.client.audio.translations.create(**params)
            return self.client.audio.transcriptions.create(**params)

    def transcribe_window(
        self,
        wav_path: str,
        window: Optional[DetectedSpeechWindow] = None,
        segment_info: Optional[str] = None,
    ) -> AsrTranscriptionResult:
        if self.config.provider == 'fireredasr2s':
            return self._transcribe_segment_firered(wav_path, window=window, segment_info=segment_info)
        if self.config.provider == 'voxtral':
            return self._transcribe_segment_voxtral(wav_path, window=window, segment_info=segment_info)

        model = self.config.model_name or 'whisper-1'
        segment_desc = segment_info or wav_path
        implausible_retry_used = False
        for attempt in range(self.config.max_retries):
            try:
                probe_result = self._get_or_probe_capabilities(
                    wav_path,
                    model,
                    window=window,
                )
                if probe_result.transcription_result and probe_result.transcription_result.ok:
                    return probe_result.transcription_result
                result = self._transcribe_with_cached_capabilities(
                    wav_path,
                    model,
                    window=window,
                )
                if result.ok or result.timestamp_mode == 'srt':
                    return result
            except ImplausibleAsrResultError as exc:
                if implausible_retry_used:
                    self.logger.warning("Implausible ASR result persists for [%s]: %s", segment_desc, exc)
                else:
                    implausible_retry_used = True
                    self.logger.warning(
                        "Implausible ASR result for [%s]; retrying once without prompt/language hint: %s",
                        segment_desc,
                        exc,
                    )
                    try:
                        result = self._transcribe_with_cached_capabilities(
                            wav_path,
                            model,
                            window=window,
                            include_language_hint=False,
                            include_prompt=False,
                        )
                        if result.ok:
                            return result
                    except Exception as retry_exc:
                        self.logger.warning("Clean retry failed for [%s]: %s", segment_desc, retry_exc)
            except AsrFormatIncompatibleError:
                raise
            except Exception as exc:
                self.logger.warning(
                    "ASR request failed for [%s] with %s: %s (attempt %d/%d)",
                    segment_desc,
                    type(exc).__name__,
                    exc,
                    attempt + 1,
                    self.config.max_retries,
                )
            if attempt < self.config.max_retries - 1:
                delay = min(self.config.retry_delay_s * (2 ** attempt), 30.0)
                time.sleep(delay)

        return AsrTranscriptionResult(
            provider=self.config.provider,
            response_format='',
            timestamp_mode='none',
            window=window,
            failure_token='asr_failed',
        )

    def _transcribe_with_cached_capabilities(
        self,
        wav_path: str,
        model: str,
        *,
        window: Optional[DetectedSpeechWindow],
        include_language_hint: bool = True,
        include_prompt: bool = True,
    ) -> AsrTranscriptionResult:
        fmt = self._capability_cache.transcription_format
        granularities = tuple(self._capability_cache.transcription_granularities or ())
        if not fmt:
            self._invalidate_capabilities()
            probe_result = self._get_or_probe_capabilities(
                wav_path,
                model,
                window=window,
                include_language_hint=include_language_hint,
                include_prompt=include_prompt,
            )
            if probe_result.transcription_result:
                return probe_result.transcription_result
            fmt = probe_result.transcription_format
            granularities = tuple(probe_result.transcription_granularities or ())

        if fmt == 'srt':
            response = self._request_whisper_response(
                wav_path,
                model,
                'srt',
                granularities=tuple(),
                include_language_hint=include_language_hint,
                include_prompt=include_prompt,
            )
            return AsrTranscriptionResult(
                provider='whisper',
                response_format='srt',
                timestamp_mode='srt',
                text=self._extract_text(response),
                window=window,
                failure_token='asr_no_timestamps',
            )

        response = self._request_whisper_response(
            wav_path,
            model,
            fmt,
            granularities=granularities,
            include_language_hint=include_language_hint,
            include_prompt=include_prompt,
        )
        payload = self._as_dict(response) or {}
        return self._payload_to_transcription_result(
            payload,
            provider='whisper',
            response_format=fmt,
            timestamp_mode='word' if self._payload_has_words(payload) else 'segment',
            window=window,
            granularities=granularities,
        )

    def _voxtral_granularity_candidates(self) -> List[Tuple[str, ...]]:
        requested = self._parse_requested_granularities()
        candidates: List[Tuple[str, ...]] = []
        if requested:
            candidates.append(requested)
        segment_only = tuple(gran for gran in requested if gran == 'segment')
        if segment_only and segment_only not in candidates:
            candidates.append(segment_only)
        if ('segment',) not in candidates:
            candidates.append(('segment',))
        candidates.append(tuple())
        deduped: List[Tuple[str, ...]] = []
        for candidate in candidates:
            normalized = tuple(gran for gran in candidate if gran)
            if normalized not in deduped:
                deduped.append(normalized)
        return deduped

    def _transcribe_segment_voxtral(
        self,
        wav_path: str,
        *,
        window: Optional[DetectedSpeechWindow],
        segment_info: Optional[str] = None,
        granularity_candidates: Optional[Sequence[Tuple[str, ...]]] = None,
        include_language_hint: bool = True,
    ) -> AsrTranscriptionResult:
        endpoint_url = self._build_voxtral_transcriptions_url(self.config.base_url)
        if not endpoint_url:
            return AsrTranscriptionResult(
                provider='voxtral',
                response_format='',
                timestamp_mode='none',
                window=window,
                failure_token='asr_failed',
            )
        duration_s = self._probe_wav_duration(wav_path)
        if (
            self.config.voxtral_enforce_max_duration
            and duration_s is not None
            and duration_s > max(1.0, float(self.config.voxtral_max_audio_duration_s or 10800.0))
        ):
            return AsrTranscriptionResult(
                provider='voxtral',
                response_format='',
                timestamp_mode='none',
                window=window,
                failure_token='asr_failed',
                fallback_token='voxtral_max_duration_exceeded',
            )

        headers: Dict[str, str] = {}
        if self.config.api_key:
            headers['x-api-key'] = self.config.api_key

        candidates = list(granularity_candidates or self._voxtral_granularity_candidates())
        model = self.config.model_name or 'voxtral-mini-latest'
        lang_hint = (self._language_hint or self.config.language or '').strip()
        for attempt in range(self.config.max_retries):
            for granularities in candidates:
                try:
                    form_data: List[Tuple[str, str]] = [('model', model)]
                    for granularity in granularities:
                        form_data.append(('timestamp_granularities', granularity))
                    if self.config.diarize:
                        form_data.append(('diarize', 'true'))
                    for item in self._parse_context_bias(self.config.context_bias):
                        form_data.append(('context_bias', item))
                    if include_language_hint and lang_hint and lang_hint.lower() != 'unknown' and not granularities:
                        form_data.append(('language', lang_hint))

                    with open(wav_path, 'rb') as file_obj:
                        response = requests.post(
                            endpoint_url,
                            headers=headers,
                            files={'file': (os.path.basename(wav_path), file_obj, 'audio/wav')},
                            data=form_data,
                            timeout=max(30.0, float(self.config.request_timeout_s or 300.0)),
                        )
                    if response.status_code != 200:
                        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
                    payload = response.json()
                    result = self._payload_to_transcription_result(
                        payload,
                        provider='voxtral',
                        response_format='verbose_json',
                        timestamp_mode='word' if self._payload_has_words(payload) else 'segment',
                        window=window,
                        granularities=granularities,
                    )
                    result.metadata['granularities'] = list(granularities)
                    return result
                except Exception as exc:
                    if self._is_format_error(exc):
                        continue
                    if attempt >= self.config.max_retries - 1:
                        return AsrTranscriptionResult(
                            provider='voxtral',
                            response_format='',
                            timestamp_mode='none',
                            window=window,
                            failure_token='asr_failed',
                        )
                    delay = min(self.config.retry_delay_s * (2 ** attempt), 30.0)
                    self.logger.warning("Voxtral request failed: %s, retrying in %.1fs", exc, delay)
                    time.sleep(delay)
                    break
        return AsrTranscriptionResult(
            provider='voxtral',
            response_format='',
            timestamp_mode='none',
            window=window,
            failure_token='asr_failed',
        )

    def _transcribe_segment_firered(
        self,
        wav_path: str,
        *,
        window: Optional[DetectedSpeechWindow],
        segment_info: Optional[str] = None,
    ) -> AsrTranscriptionResult:
        endpoint_url = self._build_firered_process_all_url(self.config.base_url)
        if not endpoint_url:
            return AsrTranscriptionResult(
                provider='fireredasr2s',
                response_format='',
                timestamp_mode='none',
                window=window,
                failure_token='asr_failed',
            )
        headers: Dict[str, str] = {}
        if self.config.api_key:
            headers['X-API-Key'] = self.config.api_key
        for attempt in range(self.config.max_retries):
            try:
                with open(wav_path, 'rb') as file_obj:
                    response = requests.post(
                        endpoint_url,
                        headers=headers,
                        files={'file': (os.path.basename(wav_path), file_obj, 'audio/wav')},
                        timeout=max(30.0, float(self.config.request_timeout_s or 300.0)),
                    )
                if response.status_code != 200:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
                payload = response.json()
                return self._firered_payload_to_result(payload, window=window)
            except Exception as exc:
                if attempt >= self.config.max_retries - 1:
                    self.logger.error("FireRed request failed: %s", exc)
                    break
                delay = min(self.config.retry_delay_s * (2 ** attempt), 30.0)
                time.sleep(delay)
        return AsrTranscriptionResult(
            provider='fireredasr2s',
            response_format='json',
            timestamp_mode='none',
            window=window,
            failure_token='asr_failed',
        )

    def transcribe_segment(self, wav_path: str, segment_info: Optional[str] = None) -> Optional[str]:
        result = self.transcribe_window(wav_path, window=None, segment_info=segment_info)
        return self._render_result_to_srt(result)

    def transcribe_segments_concurrent(
        self,
        segments: List[Tuple[float, str]],
    ) -> List[Tuple[float, Optional[str]]]:
        windows = []
        for offset, wav_path in segments:
            duration_s = self._probe_wav_duration(wav_path) or 0.0
            window = DetectedSpeechWindow(
                start_s=float(offset),
                end_s=float(offset) + duration_s,
                ownership_start_s=float(offset),
                ownership_end_s=float(offset) + duration_s,
            )
            result = self.transcribe_window(wav_path, window=window)
            windows.append((offset, self._render_result_to_srt(result, relative_to_window=True)))
        return windows

    def transcribe_windows_concurrent(
        self,
        windows: List[Tuple[DetectedSpeechWindow, str]],
    ) -> List[AsrTranscriptionResult]:
        if not windows:
            return []

        results: Dict[int, AsrTranscriptionResult] = {}
        workers = max(1, int(self.config.max_workers or 1))
        total_failures = 0
        max_total_failures = max(5, len(windows) // 2)
        jobs: List[Tuple[int, DetectedSpeechWindow, str, str]] = []
        for idx, (window, wav_path) in enumerate(windows):
            jobs.append((idx, window, wav_path, f"{window.start_s:.2f}s-{window.end_s:.2f}s"))

        remaining_jobs = jobs
        if self._needs_serial_format_probe():
            idx, window, wav_path, segment_info = jobs[0]
            result = self.transcribe_window(wav_path, window=window, segment_info=segment_info)
            results[idx] = result
            if not result.ok and result.timestamp_mode != 'srt':
                total_failures += 1
            remaining_jobs = jobs[1:]

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self.transcribe_window, wav_path, window, segment_info): (idx, window)
                for idx, window, wav_path, segment_info in remaining_jobs
            }
            for future in as_completed(futures):
                idx, window = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = AsrTranscriptionResult(
                        provider=self.config.provider,
                        response_format='',
                        timestamp_mode='none',
                        window=window,
                        failure_token='asr_failed',
                        metadata={'exception': str(exc)},
                    )
                results[idx] = result
                if not result.ok and result.timestamp_mode != 'srt':
                    total_failures += 1
                    if total_failures >= max_total_failures:
                        for pending in futures:
                            pending.cancel()
                        break

        ordered: List[AsrTranscriptionResult] = []
        for idx in range(len(windows)):
            ordered.append(results.get(
                idx,
                AsrTranscriptionResult(
                    provider=self.config.provider,
                    response_format='',
                    timestamp_mode='none',
                    window=windows[idx][0],
                    failure_token='asr_failed',
                ),
            ))
        return ordered

    def detect_language(self, wav_path: str) -> str:
        if self.config.provider == 'fireredasr2s':
            return ''
        if self.config.provider == 'voxtral':
            return self._detect_language_voxtral(wav_path)
        try:
            response = self._request_whisper_response(
                wav_path,
                self.config.model_name or 'whisper-1',
                'verbose_json',
                granularities=('segment',),
                temperature=0,
                include_language_hint=False,
                include_prompt=False,
                use_translation_endpoint=False,
            )
            data = self._as_dict(response) or {}
            return self._extract_language_from_data(data)
        except Exception as exc:
            self.logger.warning("Language detection failed: %s", exc)
            return ''

    def detect_language_from_segments(
        self,
        audio_wav: str,
        segments: Iterable[Any],
        extract_clip_fn,
    ) -> str:
        normalized_segments: List[Tuple[float, float]] = []
        for item in segments or []:
            if isinstance(item, DetectedSpeechWindow):
                normalized_segments.append((item.start_s, item.end_s))
            else:
                try:
                    normalized_segments.append((float(item[0]), float(item[1])))
                except Exception:
                    continue
        if not normalized_segments:
            return ''
        sorted_segments = sorted(normalized_segments, key=lambda segment: segment[0])
        picks = [sorted_segments[0]]
        if len(sorted_segments) > 1:
            picks.append(sorted_segments[-1])

        detected: List[str] = []
        for start_s, end_s in picks:
            clip = extract_clip_fn(audio_wav, start_s, end_s)
            if not clip:
                continue
            lang = self.detect_language(clip)
            if lang:
                detected.append(lang)
        if len(detected) >= 2 and detected[0] == detected[1]:
            return detected[0]
        return detected[0] if len(detected) == 1 else ''

    def _payload_to_transcription_result(
        self,
        payload: Dict[str, Any],
        *,
        provider: str,
        response_format: str,
        timestamp_mode: str,
        window: Optional[DetectedSpeechWindow],
        granularities: Tuple[str, ...] = tuple(),
    ) -> AsrTranscriptionResult:
        if not isinstance(payload, dict):
            return AsrTranscriptionResult(
                provider=provider,
                response_format=response_format,
                timestamp_mode='none',
                window=window,
                failure_token='asr_failed',
            )

        language = self._extract_language_from_data(payload)
        segments_data = payload.get('segments')
        segments: List[AsrSegmentTiming] = []

        if isinstance(segments_data, list):
            for raw_segment in segments_data:
                if not isinstance(raw_segment, dict):
                    continue
                text = str(raw_segment.get('text') or '').strip()
                if not text:
                    continue
                start_s = float(raw_segment.get('start', 0.0) or 0.0)
                end_s = float(raw_segment.get('end', 0.0) or 0.0)
                if end_s <= start_s:
                    end_s = start_s + _INVALID_DURATION_FALLBACK
                words = self._extract_words(raw_segment.get('words') or raw_segment.get('tokens') or [])
                if self._is_implausible_for_duration(text, max(end_s - start_s, 0.1)):
                    raise ImplausibleAsrResultError(
                        f"segment is implausible for returned timing {start_s:.2f}s-{end_s:.2f}s"
                    )
                segments.append(
                    AsrSegmentTiming(
                        start_s=start_s,
                        end_s=end_s,
                        text=text,
                        words=words,
                        confidence=self._to_optional_float(raw_segment.get('avg_logprob') or raw_segment.get('confidence')),
                        metadata={'id': raw_segment.get('id')},
                    )
                )

        text = str(payload.get('text') or '').strip()
        if not segments and text:
            duration = self._to_optional_float(payload.get('duration')) or self._probe_result_duration(payload) or 1.0
            if self._is_implausible_for_duration(text, duration):
                raise ImplausibleAsrResultError("fallback cue without segments is implausible")
            segments.append(AsrSegmentTiming(start_s=0.0, end_s=duration, text=text))

        if not text:
            text = ' '.join(segment.text for segment in segments).strip()

        result_mode = 'word' if any(segment.words for segment in segments) else timestamp_mode
        return AsrTranscriptionResult(
            provider=provider,
            response_format=response_format,
            timestamp_mode=result_mode,
            text=text,
            language=language,
            segments=segments,
            window=window,
            failure_token='' if segments or text else 'asr_failed',
            metadata={'granularities': list(granularities)},
        )

    def _firered_payload_to_result(
        self,
        payload: Dict[str, Any],
        *,
        window: Optional[DetectedSpeechWindow],
    ) -> AsrTranscriptionResult:
        sentences = payload.get('sentences') or []
        segments: List[AsrSegmentTiming] = []
        for sentence in sentences:
            if not isinstance(sentence, dict):
                continue
            text = str(sentence.get('text') or '').strip()
            if not text:
                continue
            start_s = float(sentence.get('start_ms', 0.0) or 0.0) / 1000.0
            end_s = float(sentence.get('end_ms', 0.0) or 0.0) / 1000.0
            if end_s <= start_s:
                end_s = start_s + 0.5
            segments.append(AsrSegmentTiming(start_s=start_s, end_s=end_s, text=text))
        return AsrTranscriptionResult(
            provider='fireredasr2s',
            response_format='json',
            timestamp_mode='segment',
            text=' '.join(segment.text for segment in segments).strip(),
            segments=segments,
            window=window,
            language=str(payload.get('language') or '').strip(),
        )

    @staticmethod
    def _payload_has_words(payload: Dict[str, Any]) -> bool:
        segments = payload.get('segments') or []
        if isinstance(segments, list):
            for segment in segments:
                if isinstance(segment, dict) and (segment.get('words') or segment.get('tokens')):
                    return True
        return False

    @staticmethod
    def _extract_words(raw_words: Iterable[Any]) -> List[AsrWordTiming]:
        words: List[AsrWordTiming] = []
        for raw_word in raw_words or []:
            if not isinstance(raw_word, dict):
                continue
            text = str(raw_word.get('word') or raw_word.get('text') or '').strip()
            if not text:
                continue
            start_s = float(raw_word.get('start', raw_word.get('start_s', 0.0)) or 0.0)
            end_s = float(raw_word.get('end', raw_word.get('end_s', 0.0)) or 0.0)
            if end_s <= start_s:
                continue
            words.append(AsrWordTiming(start_s=start_s, end_s=end_s, text=text))
        return words

    @staticmethod
    def _extract_language_from_data(data: Dict[str, Any]) -> str:
        language = data.get('language', '')
        if language:
            return str(language).strip()
        segments = data.get('segments') or []
        if segments and isinstance(segments, list):
            first = segments[0]
            if isinstance(first, dict):
                return str(first.get('language', '')).strip()
        return ''

    def _detect_language_voxtral(self, wav_path: str) -> str:
        endpoint_url = self._build_voxtral_transcriptions_url(self.config.base_url)
        if not endpoint_url:
            return ''
        headers: Dict[str, str] = {}
        if self.config.api_key:
            headers['x-api-key'] = self.config.api_key
        try:
            with open(wav_path, 'rb') as file_obj:
                response = requests.post(
                    endpoint_url,
                    headers=headers,
                    files={'file': (os.path.basename(wav_path), file_obj, 'audio/wav')},
                    data=[('model', self.config.model_name or 'voxtral-mini-latest')],
                    timeout=max(30.0, float(self.config.request_timeout_s or 300.0)),
                )
            if response.status_code != 200:
                return ''
            return self._extract_language_from_data(response.json() if response.content else {})
        except Exception:
            return ''

    @staticmethod
    def _render_result_to_srt(
        result: AsrTranscriptionResult,
        *,
        relative_to_window: bool = False,
    ) -> Optional[str]:
        if not result:
            return None
        if result.timestamp_mode == 'srt':
            return str(result.text or '').strip() or None
        if not result.segments:
            return None
        base_offset = result.window.start_s if (relative_to_window and result.window) else 0.0
        lines: List[str] = []
        for idx, segment in enumerate(result.segments, start=1):
            lines.append(str(idx))
            lines.append(
                f"{_format_srt_timestamp(max(0.0, segment.start_s - base_offset))} --> "
                f"{_format_srt_timestamp(max(0.0, segment.end_s - base_offset))}"
            )
            lines.append(segment.text)
            lines.append('')
        return '\n'.join(lines).strip() + '\n'

    @staticmethod
    def _extract_text(resp: Any) -> str:
        if resp is None:
            return ''
        if isinstance(resp, str):
            return resp.strip()
        text = getattr(resp, 'text', None)
        if isinstance(text, str):
            return text.strip()
        if hasattr(resp, 'read'):
            try:
                return str(resp.read() or '').strip()
            except Exception:
                return ''
        return str(resp).strip()

    @staticmethod
    def _as_dict(resp: Any) -> Optional[Dict[str, Any]]:
        if isinstance(resp, dict):
            return resp
        if hasattr(resp, 'model_dump'):
            try:
                data = resp.model_dump()
                if isinstance(data, dict):
                    return data
            except Exception:
                return None
        if hasattr(resp, 'to_dict'):
            try:
                data = resp.to_dict()
                if isinstance(data, dict):
                    return data
            except Exception:
                return None
        payload = getattr(resp, '__dict__', None)
        if isinstance(payload, dict):
            return payload
        return None

    @staticmethod
    def _parse_context_bias(value: str) -> List[str]:
        raw = str(value or '').strip()
        if not raw:
            return []
        deduped: List[str] = []
        for item in raw.replace('\n', ',').split(','):
            piece = item.strip()
            if piece and piece not in deduped:
                deduped.append(piece)
        return deduped

    @staticmethod
    def _build_firered_process_all_url(base_url: str) -> str:
        raw = (base_url or '').strip()
        if not raw:
            return ''
        if '://' not in raw:
            raw = f"http://{raw}"
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.netloc:
            return ''
        path = parsed.path or ''
        query = parse_qs(parsed.query, keep_blank_values=True)
        if path.endswith('/v1/process_all'):
            normalized_path = path
        else:
            normalized_path = f"{path.rstrip('/')}/v1/process_all".replace('//', '/')
        if 'force_refresh' not in query:
            query['force_refresh'] = ['false']
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            normalized_path,
            parsed.params,
            urlencode(query, doseq=True),
            parsed.fragment,
        ))

    @staticmethod
    def _build_voxtral_transcriptions_url(base_url: str) -> str:
        raw = (base_url or '').strip()
        if not raw:
            return ''
        if '://' not in raw:
            raw = f"https://{raw}"
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.netloc:
            return ''
        path = (parsed.path or '').rstrip('/')
        if path.endswith('/audio/transcriptions'):
            normalized_path = path
        elif path.endswith('/v1'):
            normalized_path = f"{path}/audio/transcriptions"
        elif path:
            normalized_path = f"{path}/v1/audio/transcriptions"
        else:
            normalized_path = '/v1/audio/transcriptions'
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            normalized_path,
            parsed.params,
            '',
            parsed.fragment,
        ))

    @staticmethod
    def _probe_wav_duration(wav_path: str) -> Optional[float]:
        try:
            import wave

            with wave.open(wav_path, 'rb') as file_obj:
                rate = file_obj.getframerate()
                if rate <= 0:
                    return None
                return file_obj.getnframes() / rate
        except Exception:
            return None

    @staticmethod
    def _probe_result_duration(payload: Dict[str, Any]) -> Optional[float]:
        duration = payload.get('duration')
        try:
            if duration is None:
                return None
            return float(duration)
        except Exception:
            return None

    @staticmethod
    def _to_optional_float(value: Any) -> Optional[float]:
        try:
            if value is None or value == '':
                return None
            return float(value)
        except Exception:
            return None
