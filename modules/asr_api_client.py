#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
ASR API Client Module – OpenAI-Compatible Whisper Transcription.

This module handles all communication with OpenAI-compatible speech-to-text
APIs.  It always requests ``response_format="srt"`` so that the returned
timestamps are the ASR engine's own precise alignment, **not** the physical
boundaries of the audio segment.

Features:
  - Retry with exponential back-off.
  - Concurrent / parallel segment transcription via ThreadPoolExecutor.
  - Language detection helper via Whisper.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class AsrConfig:
    """Configuration for the ASR API client."""
    api_key: str = ''
    base_url: str = ''
    model_name: str = 'whisper-1'
    language: str = ''       # Force language (e.g. 'en', 'zh', 'ja'); empty = auto
    prompt: str = ''         # Custom prompt to guide transcription
    translate: bool = False  # Translate to English
    max_retries: int = 3
    retry_delay_s: float = 2.0
    max_workers: int = 3     # Concurrent segment uploads


# ---------------------------------------------------------------------------
# AsrApiClient
# ---------------------------------------------------------------------------

class AsrApiClient:
    """Client for OpenAI-compatible Whisper transcription APIs.

    All transcription calls use ``response_format="srt"`` so that the API
    returns SRT text with its own precise internal timestamps (relative to
    the start of the submitted audio clip).
    """

    def __init__(self, config: AsrConfig, logger: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.client: Any = None
        self._language_hint: str = ''
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
            self.logger.error("Missing Whisper/OpenAI API key – ASR client not initialised")
            return
        try:
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

    def transcribe_segment(self, wav_path: str, segment_info: Optional[str] = None) -> Optional[str]:
        """Transcribe a single audio file and return raw SRT text.

        Args:
            wav_path: Path to the audio file
            segment_info: Optional description for logging (e.g., "125.20s-130.50s, 5.3s")

        Returns:
            Raw SRT string (relative timestamps), or *None* on failure.
        """
        model = self.config.model_name or 'whisper-1'
        segment_desc = segment_info or wav_path

        for attempt in range(self.config.max_retries):
            try:
                with open(wav_path, 'rb') as f:
                    params: Dict[str, Any] = {
                        'model': model,
                        'file': f,
                        'response_format': 'srt',
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

                # Extract text payload
                srt_text = self._extract_text(resp)
                if srt_text:
                    return srt_text

                self.logger.warning(
                    f"Empty ASR response for segment [{segment_desc}] "
                    f"(attempt {attempt + 1}/{self.config.max_retries})"
                )
                if attempt < self.config.max_retries - 1:
                    delay = self.config.retry_delay_s * (2 ** attempt)
                    self.logger.info(f"Retrying segment [{segment_desc}] in {delay:.1f}s")
                    time.sleep(delay)
                else:
                    self.logger.error(
                        f"ASR returned empty response for segment [{segment_desc}] "
                        f"after {self.config.max_retries} attempts"
                    )
            except Exception as exc:
                error_type = type(exc).__name__
                self.logger.warning(
                    f"ASR request failed for segment [{segment_desc}] with {error_type}: {exc} "
                    f"(attempt {attempt + 1}/{self.config.max_retries})"
                )
                if attempt < self.config.max_retries - 1:
                    delay = self.config.retry_delay_s * (2 ** attempt)
                    self.logger.info(f"Retrying segment [{segment_desc}] in {delay:.1f}s")
                    time.sleep(delay)
                else:
                    self.logger.error(
                        f"ASR failed for segment [{segment_desc}] after {self.config.max_retries} attempts"
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
