#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
ASR API Client Module – OpenAI-Compatible Whisper Transcription.

This module handles all communication with OpenAI-compatible speech-to-text
APIs. It supports format degradation: verbose_json → srt, stopping if neither
format is supported by the API.

Features:
  - Format degradation with verbose_json preferred for better compatibility
  - Retry with exponential back-off
  - Concurrent / parallel segment transcription via ThreadPoolExecutor
  - Language detection helper via Whisper
"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _format_srt_timestamp(seconds: float) -> str:
    """Format a timestamp in seconds as SRT format (HH:MM:SS,mmm).
    
    Uses millisecond-based rounding to avoid float precision flooring issues.
    Consistent with SrtTransformEngine._format_timestamp().
    """
    # Use millisecond-based rounding to avoid float precision flooring issues.
    total_millis = int(round(seconds * 1000))
    hours = total_millis // 3_600_000
    remaining = total_millis % 3_600_000
    minutes = remaining // 60_000
    remaining %= 60_000
    secs = remaining // 1000
    millis = remaining % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Supported response formats in order of preference
_SUPPORTED_FORMATS = ['verbose_json', 'srt']

@dataclass
class AsrConfig:
    """Configuration for the ASR API client."""
    provider: str = 'whisper'
    api_key: str = ''
    base_url: str = ''
    model_name: str = 'whisper-1'
    language: str = ''       # Force language (e.g. 'en', 'zh', 'ja'); empty = auto
    prompt: str = ''         # Custom prompt to guide transcription
    translate: bool = False  # Translate to English
    timestamp_granularities: str = 'segment'
    diarize: bool = False
    context_bias: str = ''
    max_retries: int = 3
    retry_delay_s: float = 2.0
    max_workers: int = 3     # Concurrent segment uploads
    request_timeout_s: float = 300.0
    voxtral_max_audio_duration_s: float = 10800.0
    voxtral_enforce_max_duration: bool = True


@dataclass
class _AsrCapabilityCache:
    transcription_format: Optional[str] = None
    language_detection_format: Optional[str] = None


@dataclass
class _AsrCapabilityProbeResult:
    transcription_format: Optional[str] = None
    language_detection_format: Optional[str] = None
    srt_text: Optional[str] = None
    language_data: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# AsrApiClient
# ---------------------------------------------------------------------------

class AsrFormatIncompatibleError(RuntimeError):
    """Raised when the ASR API supports neither verbose_json nor srt."""


class AsrApiClient:
    """Client for OpenAI-compatible Whisper transcription APIs.

    Attempts transcription with format degradation: verbose_json → srt.
    If neither format is supported, transcription stops with an error.
    """

    def __init__(self, config: AsrConfig, logger: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.client: Any = None
        self._language_hint: str = ''
        self._capability_cache = _AsrCapabilityCache()
        self._capability_probe_condition = threading.Condition()
        self._capability_probe_in_progress = False
        self._capability_probe_incompatible = False
        self._fallback_warning_logged = False
        self._logged_capability_signature: Optional[Tuple[str, str]] = None
        self._init_client()

    # ------------------------------------------------------------------
    # Language hint setter
    # ------------------------------------------------------------------

    def set_language_hint(self, lang: str):
        """Set the language hint for subsequent transcription calls."""
        self._language_hint = lang

    # ------------------------------------------------------------------
    # Client initialisation
    # ------------------------------------------------------------------

    def _init_client(self):
        if not self.config.api_key:
            # FireRed API key can be optional depending on server settings.
            if self.config.provider != 'fireredasr2s':
                self.logger.error("Missing ASR API key - ASR client not initialised")
                return
        try:
            if self.config.provider == 'fireredasr2s':
                if not self.config.base_url:
                    self.logger.error("Missing FireRedASR2S base URL - ASR client not initialised")
                    return
                # FireRed uses direct HTTP calls instead of OpenAI SDK.
                self.client = True
                self.logger.info("FireRedASR2S client initialised successfully")
                return
            if self.config.provider == 'voxtral':
                if not self.config.base_url:
                    self.logger.error("Missing Voxtral base URL - ASR client not initialised")
                    return
                # Voxtral uses direct HTTP calls instead of OpenAI SDK.
                self.client = True
                self.logger.info("Voxtral client initialised successfully")
                return

            import openai
            opts: Dict[str, Any] = {}
            if self.config.base_url:
                opts['base_url'] = self.config.base_url
            self.client = openai.OpenAI(api_key=self.config.api_key, **opts)
            self.logger.info("ASR API client initialised successfully")
        except Exception as exc:
            self.logger.error(f"Failed to initialise ASR API client: {exc}")

    # ------------------------------------------------------------------
    # Public API – single segment
    # ------------------------------------------------------------------

    @staticmethod
    def _is_format_error(exc: Exception) -> bool:
        err_str = str(exc).lower()
        return (
            'response_format' in err_str or
            'invalid response format' in err_str or
            'unsupported format' in err_str or
            ('format' in err_str and ('not supported' in err_str or 'invalid' in err_str))
        )

    def _log_fallback_warning_once(self, exc: Exception):
        with self._capability_probe_condition:
            if self._fallback_warning_logged:
                return
            self._fallback_warning_logged = True
        self.logger.warning(
            "ASR server does not support response_format='verbose_json'; "
            "falling back to 'srt'. Last error: %s",
            exc,
        )

    @staticmethod
    def _derive_language_detection_format(transcription_fmt: str) -> str:
        return 'verbose_json' if transcription_fmt == 'verbose_json' else 'json'

    def _cache_capabilities(
        self,
        transcription_fmt: Optional[str] = None,
        language_detection_fmt: Optional[str] = None,
        force_log_transcription: bool = False,
    ):
        should_log_transcription = False
        capability_signature: Optional[Tuple[str, str]] = None

        with self._capability_probe_condition:
            if transcription_fmt and self._capability_cache.transcription_format != transcription_fmt:
                should_log_transcription = self._capability_cache.transcription_format is None
                self._capability_cache.transcription_format = transcription_fmt
            elif transcription_fmt and force_log_transcription:
                should_log_transcription = True

            if language_detection_fmt:
                self._capability_cache.language_detection_format = language_detection_fmt
            elif transcription_fmt and not self._capability_cache.language_detection_format:
                self._capability_cache.language_detection_format = self._derive_language_detection_format(
                    transcription_fmt
                )

            if self._capability_cache.transcription_format:
                self._capability_probe_incompatible = False

            if (
                self._capability_cache.transcription_format
                and self._capability_cache.language_detection_format
            ):
                signature = (
                    self._capability_cache.transcription_format,
                    self._capability_cache.language_detection_format,
                )
                if signature != self._logged_capability_signature:
                    self._logged_capability_signature = signature
                    capability_signature = signature

        if should_log_transcription and transcription_fmt:
            self.logger.info(f"Successfully using {transcription_fmt} format")
        if capability_signature:
            self.logger.info(
                "Cached ASR response formats: transcription=%s, language_detection=%s",
                capability_signature[0],
                capability_signature[1],
            )

    def _invalidate_capabilities(
        self,
        expected_transcription_fmt: Optional[str] = None,
        expected_language_detection_fmt: Optional[str] = None,
    ):
        with self._capability_probe_condition:
            if (
                expected_transcription_fmt is not None
                and self._capability_cache.transcription_format != expected_transcription_fmt
            ):
                return
            if (
                expected_language_detection_fmt is not None
                and self._capability_cache.language_detection_format != expected_language_detection_fmt
            ):
                return
            self._capability_cache = _AsrCapabilityCache()
            self._capability_probe_incompatible = False
            self._logged_capability_signature = None

    def _needs_serial_format_probe(self) -> bool:
        with self._capability_probe_condition:
            return (
                not self._capability_cache.transcription_format
                and not self._capability_probe_incompatible
            )

    def _build_incompatible_error(self) -> RuntimeError:
        return AsrFormatIncompatibleError(
            "ASR API incompatible: neither verbose_json nor srt format is supported. "
            "Cannot proceed with transcription."
        )

    def _probe_capabilities(
        self,
        wav_path: str,
        model: str,
    ) -> _AsrCapabilityProbeResult:
        format_errors: List[Tuple[str, Exception]] = []
        verbose_json_data: Optional[Dict[str, Any]] = None

        try:
            verbose_resp = self._request_transcription_response(
                wav_path,
                model,
                'verbose_json',
            )
            verbose_json_data = self._as_dict(verbose_resp) or {}
            srt_text = self._verbose_json_to_srt(verbose_resp)
            if srt_text:
                return _AsrCapabilityProbeResult(
                    transcription_format='verbose_json',
                    language_detection_format='verbose_json',
                    srt_text=srt_text,
                    language_data=verbose_json_data,
                )
        except Exception as exc:
            if self._is_format_error(exc):
                format_errors.append(('verbose_json', exc))
            else:
                raise

        try:
            srt_text = self._try_format(wav_path, 'srt', model)
            if srt_text:
                if format_errors:
                    self._log_fallback_warning_once(format_errors[-1][1])
                language_fmt = 'verbose_json' if verbose_json_data else 'json'
                return _AsrCapabilityProbeResult(
                    transcription_format='srt',
                    language_detection_format=language_fmt,
                    srt_text=srt_text,
                    language_data=verbose_json_data,
                )
        except Exception as exc:
            if self._is_format_error(exc):
                format_errors.append(('srt', exc))
            else:
                raise

        if len(format_errors) == len(_SUPPORTED_FORMATS):
            last_error = format_errors[-1][1]
            self.logger.error(
                "ASR API does not support required formats (verbose_json, srt). "
                "Last error: %s",
                last_error,
            )
            raise self._build_incompatible_error()

        return _AsrCapabilityProbeResult()

    def _get_or_probe_capabilities(
        self,
        wav_path: str,
        model: str,
    ) -> _AsrCapabilityProbeResult:
        while True:
            with self._capability_probe_condition:
                if (
                    self._capability_cache.transcription_format
                    and self._capability_cache.language_detection_format
                ):
                    return _AsrCapabilityProbeResult(
                        transcription_format=self._capability_cache.transcription_format,
                        language_detection_format=self._capability_cache.language_detection_format,
                    )
                if self._capability_probe_incompatible:
                    raise self._build_incompatible_error()
                if self._capability_probe_in_progress:
                    self._capability_probe_condition.wait()
                    continue
                self._capability_probe_in_progress = True
                break

        try:
            probe_result = self._probe_capabilities(wav_path, model)
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
            if probe_result.transcription_format:
                self._capability_cache.transcription_format = probe_result.transcription_format
            if probe_result.language_detection_format:
                self._capability_cache.language_detection_format = probe_result.language_detection_format
            if self._capability_cache.transcription_format:
                self._capability_probe_incompatible = False
            self._capability_probe_condition.notify_all()

        self._cache_capabilities(
            transcription_fmt=probe_result.transcription_format,
            language_detection_fmt=probe_result.language_detection_format,
            force_log_transcription=bool(probe_result.transcription_format),
        )
        return probe_result

    def _transcribe_with_negotiated_format(
        self,
        wav_path: str,
        model: str,
        segment_desc: str,
        allow_reprobe: bool = True,
    ) -> Optional[str]:
        probe_result = self._get_or_probe_capabilities(wav_path, model)
        if probe_result.srt_text:
            return probe_result.srt_text

        fmt = probe_result.transcription_format
        if not fmt:
            return None

        try:
            srt_text = self._try_format(wav_path, fmt, model)
            if srt_text:
                return srt_text
            self.logger.warning(
                f"Cached format '{fmt}' returned empty result for segment [{segment_desc}], clearing cache"
            )
            if allow_reprobe:
                self._invalidate_capabilities(expected_transcription_fmt=fmt)
                return self._transcribe_with_negotiated_format(
                    wav_path, model, segment_desc, allow_reprobe=False,
                )
            return None
        except Exception as exc:
            if not self._is_format_error(exc):
                raise
            self.logger.warning(
                f"Cached format '{fmt}' is no longer supported for segment [{segment_desc}], "
                f"clearing cache: {exc}"
            )
            if allow_reprobe:
                self._invalidate_capabilities(expected_transcription_fmt=fmt)
                return self._transcribe_with_negotiated_format(
                    wav_path, model, segment_desc, allow_reprobe=False,
                )
            raise

    def _request_transcription_response(
        self,
        wav_path: str,
        model: str,
        fmt: str,
        *,
        temperature: Optional[float] = None,
        include_language_hint: bool = True,
        include_prompt: bool = True,
        use_translation_endpoint: Optional[bool] = None,
    ):
        with open(wav_path, 'rb') as f:
            params: Dict[str, Any] = {
                'model': model,
                'file': f,
                'response_format': fmt,
            }
            if temperature is not None:
                params['temperature'] = temperature

            if include_language_hint:
                lang = self._language_hint or self.config.language
                if lang and lang.lower() != 'unknown':
                    params['language'] = lang

            if include_prompt:
                prompt = (self.config.prompt or '').strip()
                if prompt:
                    params['prompt'] = prompt

            if use_translation_endpoint is None:
                use_translation_endpoint = bool(self.config.translate)

            if use_translation_endpoint:
                return self.client.audio.translations.create(**params)
            return self.client.audio.transcriptions.create(**params)

    def _try_format(self, wav_path: str, fmt: str, model: str) -> Optional[str]:
        """Try transcribing with a specific format.
        
        Args:
            wav_path: Path to the audio file
            fmt: Response format ('verbose_json' or 'srt')
            model: Model name
            
        Returns:
            SRT text if successful, None otherwise
        """
        resp = self._request_transcription_response(wav_path, model, fmt)

        # Extract or convert to SRT
        if fmt == 'verbose_json':
            return self._verbose_json_to_srt(resp)
        else:  # srt
            return self._extract_text(resp)

    def transcribe_segment(self, wav_path: str, segment_info: Optional[str] = None) -> Optional[str]:
        """Transcribe a single audio file and return raw SRT text.
        
        Implements format degradation: verbose_json → srt → error.
        If the API doesn't support these formats, transcription is stopped.

        Args:
            wav_path: Path to the audio file
            segment_info: Optional description for logging (e.g., "125.20s-130.50s, 5.3s")

        Returns:
            Raw SRT string (relative timestamps), or *None* on failure.
        """
        if self.config.provider == 'fireredasr2s':
            return self._transcribe_segment_firered(wav_path, segment_info)
        if self.config.provider == 'voxtral':
            return self._transcribe_segment_voxtral(wav_path, segment_info)

        model = self.config.model_name or 'whisper-1'
        segment_desc = segment_info or wav_path

        for attempt in range(self.config.max_retries):
            srt_text = None
            try:
                srt_text = self._transcribe_with_negotiated_format(
                    wav_path, model, segment_desc,
                )
                if srt_text:
                    return srt_text
            except AsrFormatIncompatibleError:
                raise
            except Exception as exc:
                error_type = type(exc).__name__
                self.logger.warning(
                    f"ASR request failed for segment [{segment_desc}] with {error_type}: {exc} "
                    f"(attempt {attempt + 1}/{self.config.max_retries})"
                )

            if not srt_text:
                if attempt < self.config.max_retries - 1:
                    delay = min(self.config.retry_delay_s * (2 ** attempt), 30.0)
                    self.logger.info(f"Retrying segment [{segment_desc}] in {delay:.1f}s")
                    time.sleep(delay)
                else:
                    self.logger.error(
                        f"ASR failed for segment [{segment_desc}] after {self.config.max_retries} attempts"
                    )
                    
        return None

    def _transcribe_segment_firered(
        self,
        wav_path: str,
        segment_info: Optional[str] = None,
    ) -> Optional[str]:
        """Transcribe one segment via FireRed `/v1/process_all`."""
        segment_desc = segment_info or wav_path
        endpoint_url = self._build_firered_process_all_url(self.config.base_url)
        if not endpoint_url:
            self.logger.error("FireRedASR2S base URL is empty")
            return None

        headers: Dict[str, str] = {}
        if self.config.api_key:
            headers['X-API-Key'] = self.config.api_key

        for attempt in range(self.config.max_retries):
            try:
                with open(wav_path, 'rb') as f:
                    files = {
                        'file': (os.path.basename(wav_path), f, 'audio/wav')
                    }
                    resp = requests.post(
                        endpoint_url,
                        headers=headers,
                        files=files,
                        timeout=max(30.0, float(self.config.request_timeout_s or 300.0)),
                    )

                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")

                payload = resp.json()
                srt_text = self._firered_response_to_srt(payload)
                if srt_text:
                    return srt_text
                raise RuntimeError("FireRed response has no usable subtitle content")
            except Exception as exc:
                if attempt < self.config.max_retries - 1:
                    delay = min(self.config.retry_delay_s * (2 ** attempt), 30.0)
                    self.logger.warning(
                        f"FireRed request failed for segment [{segment_desc}]: {exc} "
                        f"(attempt {attempt + 1}/{self.config.max_retries}), retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
                else:
                    self.logger.error(
                        f"FireRed request failed for segment [{segment_desc}] after "
                        f"{self.config.max_retries} attempts: {exc}"
                    )
        return None

    def _transcribe_segment_voxtral(
        self,
        wav_path: str,
        segment_info: Optional[str] = None,
    ) -> Optional[str]:
        """Transcribe one segment via Mistral Voxtral `/v1/audio/transcriptions`."""
        segment_desc = segment_info or wav_path
        if self.config.voxtral_enforce_max_duration:
            duration_s = self._probe_wav_duration(wav_path)
            max_duration_s = max(1.0, float(self.config.voxtral_max_audio_duration_s or 10800.0))
            if duration_s is not None and duration_s > max_duration_s:
                self.logger.error(
                    "Voxtral segment exceeds max duration: %.2fs > %.2fs [%s]. "
                    "Refusing local upload; split audio into shorter chunks first.",
                    duration_s,
                    max_duration_s,
                    segment_desc,
                )
                return None
        endpoint_url = self._build_voxtral_transcriptions_url(self.config.base_url)
        if not endpoint_url:
            self.logger.error("Voxtral base URL is empty or invalid")
            return None

        headers: Dict[str, str] = {}
        if self.config.api_key:
            headers['x-api-key'] = self.config.api_key

        model = self.config.model_name or 'voxtral-mini-latest'
        timestamp_granularities = self._parse_voxtral_timestamp_granularities(
            self.config.timestamp_granularities,
        )
        context_bias_items = self._parse_voxtral_context_bias(self.config.context_bias)
        lang_hint = (self._language_hint or self.config.language or '').strip()
        use_language = bool(lang_hint and lang_hint.lower() != 'unknown')

        # Mistral docs note: timestamp_granularities and language are incompatible.
        if timestamp_granularities and use_language:
            self.logger.warning(
                "Voxtral conflict detected for segment [%s]: both timestamp_granularities and language were set. "
                "Ignoring language and using automatic language detection.",
                segment_desc,
            )
            use_language = False

        for attempt in range(self.config.max_retries):
            try:
                form_data: List[Tuple[str, str]] = [('model', model)]
                for gran in timestamp_granularities:
                    form_data.append(('timestamp_granularities', gran))
                if self.config.diarize:
                    form_data.append(('diarize', 'true'))
                for item in context_bias_items:
                    form_data.append(('context_bias', item))
                if use_language:
                    form_data.append(('language', lang_hint))

                with open(wav_path, 'rb') as f:
                    files = {
                        'file': (os.path.basename(wav_path), f, 'audio/wav')
                    }
                    resp = requests.post(
                        endpoint_url,
                        headers=headers,
                        files=files,
                        data=form_data,
                        timeout=max(30.0, float(self.config.request_timeout_s or 300.0)),
                    )

                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")

                payload = resp.json()
                srt_text = self._verbose_json_to_srt(payload)
                if srt_text:
                    return srt_text
                raise RuntimeError("Voxtral response has no usable subtitle content")
            except Exception as exc:
                if attempt < self.config.max_retries - 1:
                    delay = min(self.config.retry_delay_s * (2 ** attempt), 30.0)
                    self.logger.warning(
                        f"Voxtral request failed for segment [{segment_desc}]: {exc} "
                        f"(attempt {attempt + 1}/{self.config.max_retries}), retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
                else:
                    self.logger.error(
                        f"Voxtral request failed for segment [{segment_desc}] after "
                        f"{self.config.max_retries} attempts: {exc}"
                    )
        return None

    # ------------------------------------------------------------------
    # Public API – concurrent multi-segment
    # ------------------------------------------------------------------

    def transcribe_segments_concurrent(
        self,
        segments: List[Tuple[float, str]],
    ) -> List[Tuple[float, Optional[str]]]:
        """Transcribe multiple segments concurrently.

        Args:
            segments: List of ``(segment_start_offset_s, wav_path)`` tuples.

        Returns:
            List of ``(segment_start_offset_s, srt_text_or_None)`` in the
            same order as the input.
        """
        results: Dict[int, Tuple[float, Optional[str]]] = {}
        if not segments:
            return []

        workers = max(1, self.config.max_workers)
        total_failures = 0
        max_total_failures = max(5, len(segments) // 2)

        self.logger.info(
            f"Transcribing {len(segments)} segments with {workers} workers"
        )

        segment_jobs: List[Tuple[int, float, str, str]] = []
        for idx, (offset, wav_path) in enumerate(segments):
            try:
                import wave
                with wave.open(wav_path, 'rb') as wf:
                    duration = wf.getnframes() / wf.getframerate() if wf.getframerate() > 0 else 0.0
                segment_info = f"{offset:.2f}s-{offset+duration:.2f}s, {duration:.2f}s"
            except Exception:
                segment_info = f"{offset:.2f}s"
            segment_jobs.append((idx, offset, wav_path, segment_info))

        remaining_jobs = segment_jobs
        if self._needs_serial_format_probe():
            idx, offset, wav_path, segment_info = segment_jobs[0]
            self.logger.info(
                "ASR format not cached yet; probing capabilities with the first segment serially before enabling concurrency"
            )
            try:
                srt_text = self.transcribe_segment(wav_path, segment_info)
                results[idx] = (offset, srt_text)
                if not srt_text:
                    total_failures += 1
                    self.logger.warning(
                        f"Segment {idx+1}/{len(segments)} [{segment_info}] transcription returned empty result"
                    )
            except Exception as exc:
                self.logger.warning(
                    f"Segment {idx+1}/{len(segments)} [{segment_info}] transcription error: {exc}"
                )
                results[idx] = (offset, None)
                total_failures += 1
            remaining_jobs = segment_jobs[1:]

        if total_failures >= max_total_failures:
            self.logger.error(
                f"ASR failed on {total_failures}/{len(segments)} segments (threshold: {max_total_failures}) – aborting"
            )
            ordered: List[Tuple[float, Optional[str]]] = []
            for idx in range(len(segments)):
                if idx in results:
                    ordered.append(results[idx])
                else:
                    ordered.append((segments[idx][0], None))
            return ordered

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for idx, offset, wav_path, segment_info in remaining_jobs:
                future = pool.submit(self.transcribe_segment, wav_path, segment_info)
                futures[future] = (idx, offset, segment_info)

            for future in as_completed(futures):
                idx, offset, segment_info = futures[future]
                try:
                    srt_text = future.result()
                    results[idx] = (offset, srt_text)
                    if not srt_text:
                        total_failures += 1
                        self.logger.warning(
                            f"Segment {idx+1}/{len(segments)} [{segment_info}] transcription returned empty result"
                        )
                except Exception as exc:
                    self.logger.warning(
                        f"Segment {idx+1}/{len(segments)} [{segment_info}] transcription error: {exc}"
                    )
                    results[idx] = (offset, None)
                    total_failures += 1

                if total_failures >= max_total_failures:
                    self.logger.error(
                        f"ASR failed on {total_failures}/{len(segments)} segments (threshold: {max_total_failures}) – aborting"
                    )
                    # Cancel remaining futures
                    for pending in futures:
                        pending.cancel()
                    break

        # Return in original order
        ordered: List[Tuple[float, Optional[str]]] = []
        for idx in range(len(segments)):
            if idx in results:
                ordered.append(results[idx])
            else:
                ordered.append((segments[idx][0], None))
        return ordered

    # ------------------------------------------------------------------
    # Language detection
    # ------------------------------------------------------------------

    def detect_language(self, wav_path: str) -> str:
        """Probe language of a short audio clip via the active ASR provider."""
        if self.config.provider == 'fireredasr2s':
            # FireRed `/v1/process_all` already runs LID internally.
            return ''
        if self.config.provider == 'voxtral':
            return self._detect_language_voxtral(wav_path)
        try:
            model = self.config.model_name or 'whisper-1'
            return self._detect_language_with_negotiated_format(wav_path, model)
        except Exception as exc:
            self.logger.warning(f"Language detection failed: {exc}")
            return ''

    def _detect_language_with_negotiated_format(
        self,
        wav_path: str,
        model: str,
        allow_reprobe: bool = True,
    ) -> str:
        probe_result = self._get_or_probe_capabilities(wav_path, model)
        if probe_result.language_data:
            lang = self._extract_language_from_data(probe_result.language_data)
            if lang:
                return lang

        fmt = probe_result.language_detection_format
        if not fmt:
            return ''

        try:
            data = self._request_language_detection_payload(wav_path, model, fmt)
        except Exception as exc:
            if not self._is_format_error(exc):
                raise
            self.logger.warning(
                "Cached language-detection format '%s' is no longer supported; clearing cache: %s",
                fmt,
                exc,
            )
            if allow_reprobe:
                self._invalidate_capabilities(
                    expected_language_detection_fmt=fmt,
                )
                return self._detect_language_with_negotiated_format(
                    wav_path, model, allow_reprobe=False,
                )
            raise

        return self._extract_language_from_data(data)

    def _request_language_detection_payload(
        self,
        wav_path: str,
        model: str,
        fmt: str,
    ) -> Dict[str, Any]:
        resp = self._request_transcription_response(
            wav_path,
            model,
            fmt,
            temperature=0,
            include_language_hint=False,
            include_prompt=False,
            use_translation_endpoint=False,
        )
        return self._as_dict(resp) or {}

    @staticmethod
    def _extract_language_from_data(data: Dict[str, Any]) -> str:
        lang = data.get('language', '')
        if lang:
            return str(lang).strip()
        segs = data.get('segments') or []
        if segs and isinstance(segs, list):
            return str(segs[0].get('language', '')).strip()
        return ''

    def _detect_language_voxtral(self, wav_path: str) -> str:
        endpoint_url = self._build_voxtral_transcriptions_url(self.config.base_url)
        if not endpoint_url:
            return ''

        headers: Dict[str, str] = {}
        if self.config.api_key:
            headers['x-api-key'] = self.config.api_key

        model = self.config.model_name or 'voxtral-mini-latest'
        try:
            with open(wav_path, 'rb') as f:
                files = {
                    'file': (os.path.basename(wav_path), f, 'audio/wav')
                }
                resp = requests.post(
                    endpoint_url,
                    headers=headers,
                    files=files,
                    data=[('model', model)],
                    timeout=max(30.0, float(self.config.request_timeout_s or 300.0)),
                )

            if resp.status_code != 200:
                return ''
            payload = resp.json()
            if not isinstance(payload, dict):
                return ''

            lang = payload.get('language', '')
            if lang:
                return str(lang).strip()
            segs = payload.get('segments') or []
            if segs and isinstance(segs, list):
                return str(segs[0].get('language', '')).strip()
            return ''
        except Exception as exc:
            self.logger.warning(f"Voxtral language detection failed: {exc}")
            return ''

    def detect_language_from_segments(
        self,
        audio_wav: str,
        segments: List[Tuple[float, float]],
        extract_clip_fn,
    ) -> str:
        """Detect language using first & last VAD segments; require agreement."""
        try:
            if not segments:
                return ''
            sorted_segs = sorted(segments, key=lambda x: x[0])
            picks = [sorted_segs[0]]
            if len(sorted_segs) > 1:
                picks.append(sorted_segs[-1])

            detected = []
            for idx, (s, e) in enumerate(picks):
                clip = extract_clip_fn(audio_wav, s, e)
                if not clip:
                    continue
                lang = self.detect_language(clip)
                if lang:
                    detected.append(lang)
                    label = 'first' if idx == 0 else 'last'
                    self.logger.info(f"Language probe ({label} segment): {lang} [{s:.2f}s–{e:.2f}s]")

            if len(detected) >= 2 and detected[0] == detected[1]:
                return detected[0]
            return ''
        except Exception as exc:
            self.logger.warning(f"Language detection from segments failed: {exc}")
            return ''

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _verbose_json_to_srt(self, resp) -> Optional[str]:
        """Convert verbose_json response to SRT format.
        
        Args:
            resp: API response in verbose_json format
            
        Returns:
            SRT formatted string, or None if conversion fails
        """
        try:
            # Convert response to dict
            if isinstance(resp, dict):
                data = resp
            elif hasattr(resp, 'model_dump'):
                data = resp.model_dump()
            elif hasattr(resp, 'to_dict'):
                data = resp.to_dict()
            else:
                d = getattr(resp, '__dict__', None)
                if isinstance(d, dict):
                    data = d
                else:
                    return None
            
            # Extract segments
            segments = data.get('segments')
            if not segments or not isinstance(segments, list):
                # No segments, try to get text and duration directly
                text = data.get('text', '')
                if text and isinstance(text, str):
                    # Use duration from response if available, fallback to 1.0s
                    duration = data.get('duration', 1.0)
                    if not isinstance(duration, (int, float)) or duration <= 0:
                        # Fallback: 1.0s may not reflect actual audio duration but provides a valid SRT entry
                        duration = 1.0
                    end_ts = _format_srt_timestamp(duration)
                    # Return simple SRT with single segment
                    return f"1\n00:00:00,000 --> {end_ts}\n{text.strip()}\n"
                return None
            
            # Build SRT from segments
            srt_lines = []
            cue_number = 1  # Use separate counter for emitted cues
            for seg in segments:
                if not isinstance(seg, dict):
                    continue
                
                start = seg.get('start', 0.0)
                end = seg.get('end', 0.0)
                text = seg.get('text', '').strip()
                
                if not text:
                    continue
                
                # Format timestamps as SRT (HH:MM:SS,mmm)
                start_ts = _format_srt_timestamp(start)
                end_ts = _format_srt_timestamp(end)
                
                srt_lines.append(f"{cue_number}")
                srt_lines.append(f"{start_ts} --> {end_ts}")
                srt_lines.append(text)
                srt_lines.append("")  # Empty line between entries
                cue_number += 1  # Increment only for emitted cues
            
            if srt_lines:
                return '\n'.join(srt_lines)
            return None
            
        except Exception as exc:
            self.logger.warning(
                f"Failed to convert verbose_json to SRT: {exc}"
            )
            return None

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
            normalized_path = f"{path.rstrip('/')}/v1/process_all"
            normalized_path = normalized_path.replace('//', '/')

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
    def _parse_voxtral_timestamp_granularities(value: str) -> List[str]:
        raw = str(value or '').strip()
        if not raw:
            return []
        pieces = [p.strip().lower() for p in raw.replace('\n', ',').split(',')]
        allowed = {'segment', 'word'}
        result: List[str] = []
        for piece in pieces:
            if piece in allowed and piece not in result:
                result.append(piece)
        return result

    @staticmethod
    def _parse_voxtral_context_bias(value: str) -> List[str]:
        raw = str(value or '').strip()
        if not raw:
            return []
        items = [p.strip() for p in raw.replace('\n', ',').split(',')]
        deduped: List[str] = []
        for item in items:
            if item and item not in deduped:
                deduped.append(item)
        return deduped

    @staticmethod
    def _probe_wav_duration(wav_path: str) -> Optional[float]:
        try:
            import wave
            with wave.open(wav_path, 'rb') as wf:
                rate = wf.getframerate()
                if rate <= 0:
                    return None
                return wf.getnframes() / rate
        except Exception:
            return None

    def _firered_response_to_srt(self, payload: Dict[str, Any]) -> Optional[str]:
        """Convert FireRed `/v1/process_all` response JSON to SRT."""
        if not isinstance(payload, dict):
            return None

        sentences = payload.get('sentences') or []
        srt_lines: List[str] = []
        cue_number = 1

        if isinstance(sentences, list):
            for sent in sentences:
                if not isinstance(sent, dict):
                    continue
                text = str(sent.get('text') or '').strip()
                if not text:
                    continue

                start_ms = sent.get('start_ms', 0)
                end_ms = sent.get('end_ms', start_ms)
                try:
                    start_s = max(0.0, float(start_ms) / 1000.0)
                except (ValueError, TypeError):
                    start_s = 0.0
                try:
                    end_s = max(start_s, float(end_ms) / 1000.0)
                except (ValueError, TypeError):
                    end_s = start_s
                if end_s <= start_s:
                    end_s = start_s + 0.6

                srt_lines.append(f"{cue_number}")
                srt_lines.append(
                    f"{_format_srt_timestamp(start_s)} --> {_format_srt_timestamp(end_s)}"
                )
                srt_lines.append(text)
                srt_lines.append("")
                cue_number += 1

        if srt_lines:
            return '\n'.join(srt_lines)

        # Fallback: single cue from top-level text/duration.
        text = str(payload.get('text') or '').strip()
        if not text:
            return None
        try:
            dur_s = float(payload.get('dur_s') or 1.0)
        except (ValueError, TypeError):
            dur_s = 1.0
        if dur_s <= 0:
            dur_s = 1.0
        end_ts = _format_srt_timestamp(dur_s)
        return f"1\n00:00:00,000 --> {end_ts}\n{text}\n"

    @staticmethod
    def _extract_text(resp) -> Optional[str]:
        """Extract the text/SRT payload from various API response shapes."""
        if isinstance(resp, str):
            return resp.strip() or None
        if hasattr(resp, 'text') and isinstance(getattr(resp, 'text'), str):
            text = getattr(resp, 'text').strip()
            return text or None
        # Try dict-like access
        try:
            d = resp if isinstance(resp, dict) else getattr(resp, '__dict__', None)
            if isinstance(d, dict):
                t = d.get('text', '')
                if isinstance(t, str) and t.strip():
                    return t.strip()
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "Failed to extract 'text' from response via dict-like access: %r", exc,
            )
        # Last resort
        try:
            import json
            return json.loads(str(resp)).get('text', '') or None
        except Exception:
            return None

    @staticmethod
    def _as_dict(resp) -> Optional[dict]:
        if isinstance(resp, dict):
            return resp
        if hasattr(resp, 'model_dump'):
            return resp.model_dump()
        if hasattr(resp, 'to_dict'):
            return resp.to_dict()
        d = getattr(resp, '__dict__', None)
        if isinstance(d, dict):
            return d
        return None
