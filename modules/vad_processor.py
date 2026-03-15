#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
VAD Processor Module – adaptive two-pass voice activity detection.
"""

import os
import subprocess
import tempfile
import threading
import wave
from dataclasses import dataclass, replace
import logging
from typing import Any, List, Optional, Tuple

from .ffmpeg_manager import get_ffmpeg_path
from .subtitle_pipeline_types import DetectedSpeechWindow


@dataclass
class VadConfig:
    provider: str = 'silero-vad'
    threshold: float = 0.55
    min_speech_ms: int = 300
    min_silence_ms: int = 320
    max_speech_s: int = 120
    speech_pad_ms: int = 120
    chunk_window_s: float = 30.0
    chunk_overlap_s: float = 0.4
    merge_gap_s: float = 0.35
    min_segment_s: float = 0.8
    max_segment_s: float = 30.0
    max_segment_s_for_split: float = 30.0
    refinement_enabled: bool = True
    min_speech_coverage_ratio: float = 0.015


class VadProcessor:
    _silero_vad_model: Any = None
    _silero_vad_utils: Any = None
    _silero_vad_lock = threading.Lock()

    def __init__(self, config: VadConfig, logger: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self._temp_dirs: List[str] = []
        self.last_result_state: str = 'unknown'
        self.last_failure_reason: str = ''
        self.last_speech_coverage_ratio: float = 0.0
        self._last_run_failed: bool = False
        self._last_run_failure_reason: str = ''

    def detect_speech_segments(
        self,
        wav_path: str,
        total_duration_s: float,
    ) -> Optional[List[Tuple[float, float]]]:
        windows = self.detect_speech_windows(wav_path, total_duration_s)
        if windows is None:
            return None
        return [(w.start_s, w.end_s) for w in windows]

    def detect_speech_windows(
        self,
        wav_path: str,
        total_duration_s: float,
    ) -> Optional[List[DetectedSpeechWindow]]:
        try:
            self.last_result_state = 'unknown'
            self.last_failure_reason = ''
            self.last_speech_coverage_ratio = 0.0
            self.logger.info(
                "VAD effective limits: cap %.2fs, chunk_window %.2fs, max_speech %.2fs, split %.2fs, merge_gap %.2fs",
                self._effective_vad_cap_s(),
                self._effective_chunk_window_s(),
                self._effective_vad_max_speech_s(),
                self._effective_split_limit_s(),
                max(0.0, float(self.config.merge_gap_s or 0.0)),
            )

            primary_windows = self._detect_windows_with_config(
                wav_path,
                total_duration_s,
                self.config,
                source_pass='scan',
            )
            primary_coverage = self._windows_coverage_ratio(primary_windows, total_duration_s)
            self.last_speech_coverage_ratio = primary_coverage

            if primary_windows and primary_coverage >= max(0.0, float(self.config.min_speech_coverage_ratio or 0.0)):
                self.last_result_state = 'success'
                return primary_windows

            relaxed_config = self._build_relaxed_retry_config()
            should_retry_relaxed = (
                primary_windows is None
                or not primary_windows
                or primary_coverage < max(0.0, float(self.config.min_speech_coverage_ratio or 0.0))
            )
            if should_retry_relaxed:
                relaxed_windows = self._detect_windows_with_config(
                    wav_path,
                    total_duration_s,
                    relaxed_config,
                    source_pass='relaxed_retry',
                )
                relaxed_coverage = self._windows_coverage_ratio(relaxed_windows, total_duration_s)
                if relaxed_windows and relaxed_coverage > primary_coverage:
                    self.last_speech_coverage_ratio = relaxed_coverage
                    self.last_result_state = 'success'
                    return relaxed_windows
                if primary_windows and primary_coverage > 0.0:
                    self.last_result_state = 'low_coverage'
                    self.last_failure_reason = 'vad_low_coverage'
                    return primary_windows
                if relaxed_windows == [] and primary_windows == []:
                    self.last_result_state = 'no_speech'
                    self.last_failure_reason = 'vad_no_speech'
                    return []

            if primary_windows is None:
                self.last_result_state = 'failure'
                self.last_failure_reason = self._last_run_failure_reason or 'vad_failed'
                return None
            if not primary_windows:
                self.last_result_state = 'no_speech'
                self.last_failure_reason = 'vad_no_speech'
                return []

            self.last_result_state = 'success'
            return primary_windows
        except Exception as exc:
            self.last_result_state = 'failure'
            self.last_failure_reason = str(exc)
            self.logger.warning("VAD processing failed: %s", exc)
            return None

    def cleanup(self):
        import shutil

        for d in self._temp_dirs:
            try:
                if os.path.exists(d):
                    shutil.rmtree(d)
            except Exception as exc:
                self.logger.warning("Failed to remove temporary directory %s: %s", d, exc)
        self._temp_dirs.clear()

    def _load_silero_vad(self):
        if VadProcessor._silero_vad_model is not None and VadProcessor._silero_vad_utils is not None:
            return VadProcessor._silero_vad_model, VadProcessor._silero_vad_utils
        with VadProcessor._silero_vad_lock:
            if VadProcessor._silero_vad_model is not None and VadProcessor._silero_vad_utils is not None:
                return VadProcessor._silero_vad_model, VadProcessor._silero_vad_utils
            try:
                from silero_vad import get_speech_timestamps, load_silero_vad

                model = load_silero_vad()
                utils = {'get_speech_timestamps': get_speech_timestamps}
                VadProcessor._silero_vad_model = model
                VadProcessor._silero_vad_utils = utils
                self.logger.info("Silero VAD model loaded successfully")
                return model, utils
            except ImportError:
                self.logger.error("Missing silero-vad dependency – pip install silero-vad torch")
                raise

    def _detect_windows_with_config(
        self,
        wav_path: str,
        total_duration_s: float,
        config: VadConfig,
        *,
        source_pass: str,
    ) -> Optional[List[DetectedSpeechWindow]]:
        if total_duration_s > self._effective_chunk_window_s(config):
            return self._detect_chunked(wav_path, total_duration_s, config, source_pass=source_pass)

        raw_pairs = self._run_vad_on_audio(wav_path, total_duration_s, config)
        if raw_pairs is None:
            return None
        constrained_pairs = self._apply_constraints(raw_pairs, config=config)
        ownership = (0.0, total_duration_s)
        windows = [
            self._build_window(
                start,
                end,
                ownership_start=ownership[0],
                ownership_end=ownership[1],
                chunk_index=0,
                total_chunks=1,
                source_pass=source_pass,
                threshold=config.threshold,
                raw_spans=[(start, end)],
            )
            for start, end in constrained_pairs
        ]
        return self._refine_windows(wav_path, windows, config)

    def _run_vad_on_audio(
        self,
        wav_path: str,
        total_duration_s: float,
        config: VadConfig,
    ) -> Optional[List[Tuple[float, float]]]:
        try:
            self._set_run_state(False)
            import numpy as np
            import torch

            model, utils = self._load_silero_vad()
            get_speech_timestamps = utils['get_speech_timestamps']

            with wave.open(wav_path, 'rb') as wf:
                sample_rate = wf.getframerate()
                channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                total_frames = wf.getnframes()
                if sample_rate != 16000 or channels != 1:
                    reason = f"unsupported audio format: {sample_rate} Hz {channels}-ch"
                    self._set_run_state(True, reason)
                    self.logger.warning("VAD requires 16 kHz mono; got %s", reason)
                    return None
                audio_bytes = wf.readframes(total_frames)

            duration_from_wav = total_frames / float(sample_rate)
            if duration_from_wav < 0.5:
                reason = f"audio too short: {duration_from_wav:.3f}s"
                self._set_run_state(True, reason)
                self.logger.warning("Audio too short for VAD: %s", reason)
                return None

            if sample_width == 2:
                audio_array = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            elif sample_width == 4:
                audio_array = np.frombuffer(audio_bytes, dtype=np.int32).astype(np.float32) / 2147483648.0
            else:
                reason = f"unsupported sample width: {sample_width} bytes"
                self._set_run_state(True, reason)
                self.logger.warning(reason)
                return None

            audio_tensor = torch.from_numpy(audio_array)
            speech_timestamps = get_speech_timestamps(
                audio_tensor,
                model,
                threshold=config.threshold,
                min_speech_duration_ms=config.min_speech_ms,
                min_silence_duration_ms=config.min_silence_ms,
                speech_pad_ms=config.speech_pad_ms,
                max_speech_duration_s=self._effective_vad_max_speech_s(config),
                sampling_rate=sample_rate,
                return_seconds=True,
            )
            if not speech_timestamps:
                self._set_run_state(False)
                return []

            raw_pairs = [
                (float(seg['start']), float(seg['end']))
                for seg in speech_timestamps
                if float(seg['end']) > float(seg['start'])
            ]
            self.logger.info(
                "VAD %s pass detected %d raw spans over %.2fs (coverage %.3f)",
                getattr(config, 'provider', 'silero-vad'),
                len(raw_pairs),
                total_duration_s,
                self._pairs_coverage_ratio(raw_pairs, total_duration_s),
            )
            self._set_run_state(False)
            return raw_pairs
        except ImportError:
            self._set_run_state(True, "missing silero-vad or torch dependency")
            return None
        except Exception as exc:
            self._set_run_state(True, str(exc))
            self.logger.warning("VAD exception: %s", exc)
            return None

    def _detect_chunked(
        self,
        wav_path: str,
        total_duration_s: float,
        config: VadConfig,
        *,
        source_pass: str,
    ) -> Optional[List[DetectedSpeechWindow]]:
        chunks = self._create_chunks(total_duration_s, config)
        self.logger.info(
            "VAD chunked processing: %.1fs, window %.2fs, overlap %.2fs, %d chunks",
            total_duration_s,
            self._effective_chunk_window_s(config),
            float(config.chunk_overlap_s or 0.0),
            len(chunks),
        )

        chunk_windows: List[DetectedSpeechWindow] = []
        consecutive_failures = 0
        total_chunks = len(chunks)

        for chunk_index, (chunk_start, chunk_end) in enumerate(chunks):
            chunk_wav = self._extract_audio_clip(wav_path, chunk_start, chunk_end)
            if not chunk_wav:
                consecutive_failures += 1
                self._set_run_state(True, "audio clip extraction failed")
                if consecutive_failures >= 3 and not chunk_windows:
                    return None
                continue

            chunk_duration = chunk_end - chunk_start
            raw_pairs = self._run_vad_on_audio(chunk_wav, chunk_duration, config)
            if raw_pairs is None:
                consecutive_failures += 1
                if consecutive_failures >= 3 and not chunk_windows:
                    return None
                continue

            consecutive_failures = 0
            adjusted = [(s + chunk_start, e + chunk_start) for s, e in raw_pairs]
            adjusted = self._clip_chunk_segments(
                adjusted,
                config=config,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                chunk_start=chunk_start,
                chunk_end=chunk_end,
            )
            constrained = self._apply_constraints(adjusted, config=config, allow_gap_merge=False)
            keep_start, keep_end = self._ownership_range(
                config,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                chunk_start=chunk_start,
                chunk_end=chunk_end,
            )
            coverage_ratio = self._pairs_coverage_ratio(constrained, max(chunk_end - chunk_start, 0.01))
            for start, end in constrained:
                chunk_windows.append(
                    self._build_window(
                        start,
                        end,
                        ownership_start=keep_start,
                        ownership_end=keep_end,
                        chunk_index=chunk_index,
                        total_chunks=total_chunks,
                        source_pass=source_pass,
                        threshold=config.threshold,
                        raw_spans=[(start, end)],
                        coverage_ratio=coverage_ratio,
                    )
                )

        if not chunk_windows:
            return []
        merged_windows = self._merge_windows(chunk_windows, config=config, allow_gap_merge=False)
        return self._refine_windows(wav_path, merged_windows, config)

    def _refine_windows(
        self,
        wav_path: str,
        windows: List[DetectedSpeechWindow],
        config: VadConfig,
    ) -> List[DetectedSpeechWindow]:
        if not windows:
            return []
        if not bool(config.refinement_enabled):
            return windows

        refined_config = self._build_refinement_config(config)
        refined_windows: List[DetectedSpeechWindow] = []
        for window in windows:
            clip = self._extract_audio_clip(wav_path, window.start_s, window.end_s)
            if not clip:
                refined_windows.append(window)
                continue
            local_pairs = self._run_vad_on_audio(clip, window.duration_s, refined_config)
            if not local_pairs:
                refined_windows.append(window)
                continue

            refined_start = window.start_s + min(max(pair[0], 0.0) for pair in local_pairs)
            refined_end = window.start_s + max(max(pair[1], pair[0]) for pair in local_pairs)
            refined_start = max(window.start_s, refined_start)
            refined_end = min(window.end_s, refined_end)
            if refined_end <= refined_start:
                refined_windows.append(window)
                continue

            refined_pairs = [(window.start_s + s, window.start_s + e) for s, e in local_pairs]
            refined_windows.append(
                replace(
                    window,
                    start_s=refined_start,
                    end_s=refined_end,
                    source_pass='refine',
                    threshold=refined_config.threshold,
                    coverage_ratio=self._pairs_coverage_ratio(local_pairs, max(window.duration_s, 0.01)),
                    speech_duration_s=sum(max(0.0, e - s) for s, e in local_pairs),
                    refined=True,
                    raw_spans=refined_pairs,
                )
            )
        return self._merge_windows(refined_windows, config=config, allow_gap_merge=False)

    def _build_window(
        self,
        start_s: float,
        end_s: float,
        *,
        ownership_start: float,
        ownership_end: float,
        chunk_index: int,
        total_chunks: int,
        source_pass: str,
        threshold: float,
        raw_spans: List[Tuple[float, float]],
        coverage_ratio: Optional[float] = None,
    ) -> DetectedSpeechWindow:
        speech_duration_s = sum(max(0.0, e - s) for s, e in raw_spans)
        window_duration = max(0.01, float(end_s) - float(start_s))
        return DetectedSpeechWindow(
            start_s=float(start_s),
            end_s=float(end_s),
            ownership_start_s=float(ownership_start),
            ownership_end_s=float(ownership_end),
            chunk_index=int(chunk_index),
            total_chunks=int(total_chunks),
            source_pass=source_pass,
            threshold=float(threshold),
            coverage_ratio=float(coverage_ratio if coverage_ratio is not None else speech_duration_s / window_duration),
            speech_duration_s=float(speech_duration_s),
            raw_spans=[(float(s), float(e)) for s, e in raw_spans],
        )

    def _create_chunks(self, total_duration_s: float, config: VadConfig) -> List[Tuple[float, float]]:
        window = self._effective_chunk_window_s(config)
        overlap = max(0.0, min(float(config.chunk_overlap_s or 0.0), max(window - 0.01, 0.0)))
        if total_duration_s <= window:
            return [(0.0, total_duration_s)]
        chunks: List[Tuple[float, float]] = []
        current = 0.0
        while current < total_duration_s:
            end = min(current + window, total_duration_s)
            chunks.append((current, end))
            if end >= total_duration_s:
                break
            current = end - overlap
        return chunks

    def _ownership_range(
        self,
        config: VadConfig,
        *,
        chunk_index: int,
        total_chunks: int,
        chunk_start: float,
        chunk_end: float,
    ) -> Tuple[float, float]:
        half_overlap = max(0.0, float(config.chunk_overlap_s or 0.0) / 2.0)
        keep_start = chunk_start + (half_overlap if chunk_index > 0 else 0.0)
        keep_end = chunk_end - (half_overlap if chunk_index < total_chunks - 1 else 0.0)
        if keep_end <= keep_start:
            return chunk_start, chunk_end
        return keep_start, keep_end

    def _clip_chunk_segments(
        self,
        segments: List[Tuple[float, float]],
        *,
        config: VadConfig,
        chunk_index: int,
        total_chunks: int,
        chunk_start: float,
        chunk_end: float,
    ) -> List[Tuple[float, float]]:
        if not segments:
            return []
        keep_start, keep_end = self._ownership_range(
            config,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            chunk_start=chunk_start,
            chunk_end=chunk_end,
        )
        clipped: List[Tuple[float, float]] = []
        for start, end in segments:
            clipped_start = max(float(start), keep_start)
            clipped_end = min(float(end), keep_end)
            if clipped_end > clipped_start:
                clipped.append((clipped_start, clipped_end))
        return clipped

    def _apply_constraints(
        self,
        segments: List[Tuple[float, float]],
        *,
        config: VadConfig,
        allow_gap_merge: bool = True,
    ) -> List[Tuple[float, float]]:
        if not segments:
            return []
        segments = sorted((float(start), float(end)) for start, end in segments if end > start)
        if not segments:
            return []
        max_dur = self._effective_split_limit_s(config)
        merge_gap = max(0.0, float(config.merge_gap_s or 0.0))
        chunk_stitch_gap = max(0.0, float(config.chunk_overlap_s or 0.0) / 2.0 + 0.02)

        merged: List[List[float]] = []
        for start, end in segments:
            if not merged:
                merged.append([start, end])
                continue
            last = merged[-1]
            boundary_gap = start - last[1]
            combined_duration = max(last[1], end) - last[0]
            can_merge = boundary_gap < merge_gap if allow_gap_merge else (
                boundary_gap <= chunk_stitch_gap and combined_duration <= max_dur
            )
            if can_merge:
                last[1] = max(last[1], end)
            else:
                merged.append([start, end])

        min_dur = max(0.0, float(config.min_segment_s or 0.0))
        filtered: List[List[float]] = []
        idx = 0
        while idx < len(merged):
            seg = merged[idx]
            duration = seg[1] - seg[0]
            if duration < min_dur:
                if filtered:
                    filtered[-1][1] = seg[1]
                elif idx < len(merged) - 1:
                    merged[idx + 1][0] = seg[0]
                else:
                    filtered.append(seg)
            else:
                filtered.append(seg)
            idx += 1

        final: List[Tuple[float, float]] = []
        for start, end in filtered:
            duration = end - start
            if duration <= max_dur:
                final.append((start, end))
                continue
            self.logger.info("Force-splitting long VAD window: %.2fs > %.2fs", duration, max_dur)
            cursor = start
            while cursor < end:
                next_end = min(cursor + max_dur, end)
                final.append((cursor, next_end))
                cursor = next_end

        return final

    def _merge_windows(
        self,
        windows: List[DetectedSpeechWindow],
        *,
        config: VadConfig,
        allow_gap_merge: bool,
    ) -> List[DetectedSpeechWindow]:
        if not windows:
            return []
        merged_segments = self._apply_constraints(
            [(w.start_s, w.end_s) for w in windows],
            config=config,
            allow_gap_merge=allow_gap_merge,
        )
        merged_windows: List[DetectedSpeechWindow] = []
        for start, end in merged_segments:
            matched = [w for w in windows if not (w.end_s <= start or w.start_s >= end)]
            if not matched:
                continue
            merged_windows.append(
                DetectedSpeechWindow(
                    start_s=float(start),
                    end_s=float(end),
                    ownership_start_s=min(w.ownership_start_s for w in matched),
                    ownership_end_s=max(w.ownership_end_s for w in matched),
                    chunk_index=min(w.chunk_index for w in matched),
                    total_chunks=max(w.total_chunks for w in matched),
                    source_pass=matched[-1].source_pass,
                    threshold=max(w.threshold for w in matched),
                    coverage_ratio=self._pairs_coverage_ratio(
                        [(w.start_s, w.end_s) for w in matched],
                        max(end - start, 0.01),
                    ),
                    speech_duration_s=sum(max(0.0, min(end, w.end_s) - max(start, w.start_s)) for w in matched),
                    refined=any(w.refined for w in matched),
                    raw_spans=[span for w in matched for span in w.raw_spans],
                )
            )
        return merged_windows

    def _set_run_state(self, failed: bool, reason: str = ''):
        self._last_run_failed = failed
        self._last_run_failure_reason = reason

    @staticmethod
    def _pairs_coverage_ratio(pairs: Optional[List[Tuple[float, float]]], duration_s: float) -> float:
        if not pairs:
            return 0.0
        total_speech = sum(max(0.0, float(end) - float(start)) for start, end in pairs)
        safe_duration = max(float(duration_s or 0.0), 0.01)
        return total_speech / safe_duration

    def _windows_coverage_ratio(
        self,
        windows: Optional[List[DetectedSpeechWindow]],
        total_duration_s: float,
    ) -> float:
        if not windows:
            return 0.0
        total_speech = sum(max(0.0, w.end_s - w.start_s) for w in windows)
        return total_speech / max(float(total_duration_s or 0.0), 0.01)

    def _build_relaxed_retry_config(self) -> VadConfig:
        return replace(
            self.config,
            threshold=max(0.35, float(self.config.threshold or 0.55) - 0.12),
            min_speech_ms=max(120, int(self.config.min_speech_ms * 0.6)),
            min_silence_ms=max(160, int(self.config.min_silence_ms * 0.6)),
            speech_pad_ms=min(320, int(self.config.speech_pad_ms + 60)),
        )

    def _build_refinement_config(self, config: VadConfig) -> VadConfig:
        return replace(
            config,
            threshold=min(0.80, float(config.threshold or 0.55) + 0.08),
            min_speech_ms=max(80, int(config.min_speech_ms * 0.6)),
            min_silence_ms=max(120, int(config.min_silence_ms * 0.75)),
            speech_pad_ms=max(40, int(config.speech_pad_ms * 0.5)),
            refinement_enabled=False,
        )

    def _effective_vad_cap_s(self, config: Optional[VadConfig] = None) -> float:
        active = config or self.config
        try:
            cap = float(active.max_segment_s or 0.0)
        except Exception:
            cap = 0.0
        return max(0.1, cap)

    def _effective_chunk_window_s(self, config: Optional[VadConfig] = None) -> float:
        active = config or self.config
        try:
            window = float(active.chunk_window_s or 0.0)
        except Exception:
            window = 0.0
        if window <= 0.0:
            window = self._effective_vad_cap_s(active)
        return min(window, self._effective_vad_cap_s(active))

    def _effective_vad_max_speech_s(self, config: Optional[VadConfig] = None) -> float:
        active = config or self.config
        try:
            max_speech = float(active.max_speech_s or 0.0)
        except Exception:
            max_speech = 0.0
        if max_speech <= 0.0:
            max_speech = self._effective_vad_cap_s(active)
        return min(max_speech, self._effective_vad_cap_s(active))

    def _effective_split_limit_s(self, config: Optional[VadConfig] = None) -> float:
        active = config or self.config
        try:
            split_limit = float(active.max_segment_s_for_split or 0.0)
        except Exception:
            split_limit = 0.0
        if split_limit <= 0.0:
            split_limit = self._effective_vad_cap_s(active)
        return min(max(0.1, split_limit), self._effective_vad_cap_s(active))

    def _extract_audio_clip(self, wav_path: str, start_s: float, end_s: float) -> Optional[str]:
        try:
            ffmpeg_bin = get_ffmpeg_path(logger=self.logger) or 'ffmpeg'
            out_dir = tempfile.mkdtemp(prefix='y2a_vad_clip_')
            self._temp_dirs.append(out_dir)
            out_wav = os.path.join(out_dir, 'clip.wav')
            duration = max(0.01, float(end_s) - float(start_s))
            cmd = [
                ffmpeg_bin, '-y',
                '-ss', f"{start_s:.3f}",
                '-t', f"{duration:.3f}",
                '-i', wav_path,
                '-ac', '1',
                '-ar', '16000',
                '-acodec', 'pcm_s16le',
                '-f', 'wav',
                out_wav,
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=120,
            )
            if result.returncode != 0 or not os.path.exists(out_wav):
                self.logger.warning("Audio clip extraction failed: %s", (result.stderr or '')[:200])
                return None
            with wave.open(out_wav, 'rb') as wf:
                actual_dur = wf.getnframes() / wf.getframerate() if wf.getframerate() > 0 else 0.0
                if actual_dur < 0.1:
                    return None
            return out_wav
        except Exception as exc:
            self.logger.warning("Audio clip extraction exception (%.3f-%.3f): %s", start_s, end_s, exc)
            return None
