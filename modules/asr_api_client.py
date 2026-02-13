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
    max_retries: int = 3
    retry_delay_s: float = 2.0
    max_workers: int = 3     # Concurrent segment uploads
    request_timeout_s: float = 300.0


# ---------------------------------------------------------------------------
# AsrApiClient
# ---------------------------------------------------------------------------

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
        self._supported_format: Optional[str] = None  # Cache supported format
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
                self.logger.error("Missing Whisper/OpenAI API key - ASR client not initialised")
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

    def _try_format(self, wav_path: str, fmt: str, model: str) -> Optional[str]:
        """Try transcribing with a specific format.
        
        Args:
            wav_path: Path to the audio file
            fmt: Response format ('verbose_json' or 'srt')
            model: Model name
            
        Returns:
            SRT text if successful, None otherwise
        """
        with open(wav_path, 'rb') as f:
            params: Dict[str, Any] = {
                'model': model,
                'file': f,
                'response_format': fmt,
            }
            lang = self._language_hint or self.config.language
            if lang and lang.lower() != 'unknown':
                params['language'] = lang

            base_prompt = "Transcribe speech only. Ignore noise."
            if self.config.prompt:
                params['prompt'] = f"{base_prompt} {self.config.prompt}"
            else:
                params['prompt'] = base_prompt

            if self.config.translate:
                resp = self.client.audio.translations.create(**params)
            else:
                resp = self.client.audio.transcriptions.create(**params)

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

        model = self.config.model_name or 'whisper-1'
        segment_desc = segment_info or wav_path

        for attempt in range(self.config.max_retries):
            srt_text = None
            format_error = None
            last_was_format_error = False
            cache_invalidated = False
            
            # Try formats in order: verbose_json → srt
            # If cached format is set, try it first but fall back to full sequence if it fails
            using_cached_format = self._supported_format is not None
            formats_to_try = [self._supported_format] if using_cached_format else _SUPPORTED_FORMATS
            
            for fmt in formats_to_try:
                try:
                    srt_text = self._try_format(wav_path, fmt, model)
                    
                    if srt_text:
                        if self._supported_format is None:
                            self.logger.info(f"Successfully using {fmt} format")
                        self._supported_format = fmt  # Cache successful format
                        return srt_text
                    
                    # No SRT text from cached format - clear cache and signal to retry
                    if using_cached_format and not cache_invalidated:
                        self.logger.warning(
                            f"Cached format '{self._supported_format}' returned empty result, "
                            f"clearing cache"
                        )
                        self._supported_format = None
                        cache_invalidated = True
                        break  # Exit format loop to retry with all formats
                        
                except Exception as exc:
                    err_str = str(exc).lower()
                    # Check if it's a format-related error (more specific patterns)
                    is_format_error = (
                        'response_format' in err_str or
                        'invalid response format' in err_str or
                        'unsupported format' in err_str or
                        ('format' in err_str and ('not supported' in err_str or 'invalid' in err_str))
                    )
                    if is_format_error:
                        format_error = exc
                        last_was_format_error = True
                        self.logger.warning(
                            f"Format '{fmt}' not supported for segment [{segment_desc}]: {exc}"
                        )
                        # If cached format failed with format error, clear cache and signal to retry
                        if using_cached_format and not cache_invalidated:
                            self.logger.warning(
                                f"Cached format '{self._supported_format}' no longer supported, "
                                f"clearing cache"
                            )
                            self._supported_format = None
                            cache_invalidated = True
                            break  # Exit format loop to retry with all formats
                        continue  # Try next format
                    else:
                        # Non-format error - log and break format loop to trigger retry
                        last_was_format_error = False
                        error_type = type(exc).__name__
                        self.logger.warning(
                            f"ASR request failed for segment [{segment_desc}] with {error_type}: {exc} "
                            f"(attempt {attempt + 1}/{self.config.max_retries})"
                        )
                        break  # Break format loop to trigger outer retry loop
            
            # If cache was invalidated, retry with all formats once before giving up
            if cache_invalidated and not srt_text:
                self.logger.info("Retrying with all formats after cache invalidation")
                for fmt in _SUPPORTED_FORMATS:
                    try:
                        srt_text = self._try_format(wav_path, fmt, model)
                        
                        if srt_text:
                            self.logger.info(f"Successfully using {fmt} format after cache invalidation")
                            self._supported_format = fmt
                            return srt_text
                            
                    except Exception as exc:
                        err_str = str(exc).lower()
                        is_format_error = (
                            'response_format' in err_str or
                            'invalid response format' in err_str or
                            'unsupported format' in err_str or
                            ('format' in err_str and ('not supported' in err_str or 'invalid' in err_str))
                        )
                        if is_format_error:
                            format_error = exc
                            last_was_format_error = True
                            self.logger.warning(
                                f"Format '{fmt}' not supported for segment [{segment_desc}]: {exc}"
                            )
                            continue
                        else:
                            last_was_format_error = False
                            break
            
            # Only raise RuntimeError if all formats failed specifically due to format support
            if format_error and last_was_format_error:
                self.logger.error(
                    f"ASR API does not support required formats (verbose_json, srt) for segment [{segment_desc}]. "
                    f"Last error: {format_error}"
                )
                raise RuntimeError(
                    f"ASR API incompatible: neither verbose_json nor srt format is supported. "
                    f"Cannot proceed with transcription."
                )
            
            # Empty response or error - retry if attempts remain
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
        workers = max(1, self.config.max_workers)

        self.logger.info(
            f"Transcribing {len(segments)} segments with {workers} workers"
        )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for idx, (offset, wav_path) in enumerate(segments):
                # Create segment info for better logging
                try:
                    import wave
                    with wave.open(wav_path, 'rb') as wf:
                        duration = wf.getnframes() / wf.getframerate() if wf.getframerate() > 0 else 0.0
                    segment_info = f"{offset:.2f}s-{offset+duration:.2f}s, {duration:.2f}s"
                except Exception:
                    segment_info = f"{offset:.2f}s"

                future = pool.submit(self.transcribe_segment, wav_path, segment_info)
                futures[future] = (idx, offset, segment_info)

            total_failures = 0
            max_total_failures = max(5, len(segments) // 2)
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
        """Probe language of a short audio clip via Whisper."""
        if self.config.provider == 'fireredasr2s':
            # FireRed `/v1/process_all` already runs LID internally.
            return ''
        try:
            model = self.config.model_name or 'whisper-1'
            with open(wav_path, 'rb') as f:
                params: Dict[str, Any] = {
                    'model': model,
                    'file': f,
                    'response_format': 'verbose_json',
                    'temperature': 0,
                }
                try:
                    resp = self.client.audio.transcriptions.create(**params)
                except Exception as exc:
                    err_str = str(exc).lower()
                    if 'response_format' in err_str:
                        params['response_format'] = 'json'
                        resp = self.client.audio.transcriptions.create(**params)
                    else:
                        raise

            data = self._as_dict(resp) or {}
            lang = data.get('language', '')
            if lang:
                return str(lang).strip()
            segs = data.get('segments') or []
            if segs and isinstance(segs, list):
                return str(segs[0].get('language', '')).strip()
            return ''
        except Exception as exc:
            self.logger.warning(f"Language detection failed: {exc}")
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
