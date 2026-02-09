#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
VAD Processor Module – Broad, High-Recall Voice Activity Detection.

This module implements a lenient VAD strategy designed to produce "search windows"
for the downstream ASR engine, **not** final subtitle boundaries.

Key design decisions:
  - Dynamic padding (default 500 ms) to avoid cutting mid-word.
  - Aggressive gap merging (default 1 s) to keep semantic context intact.
  - Thread-safe, lazy-loaded Silero VAD model cached at class level.
"""

import os
import wave
import logging
import threading
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from .ffmpeg_manager import get_ffmpeg_path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class VadConfig:
    """Configuration for the broad VAD preprocessor."""
    provider: str = 'silero-vad'
    threshold: float = 0.5          # Speech probability threshold (lenient)
    min_speech_ms: int = 250        # Ignore sounds shorter than this
    min_silence_ms: int = 500       # Min silence to split (broad – avoid mid-word cuts)
    max_speech_s: int = 120         # Max continuous speech before forced split
    speech_pad_ms: int = 500        # Dynamic padding before/after speech (500 ms+)

    # Audio chunking (for processing long files in manageable pieces)
    chunk_window_s: float = 25.0
    chunk_overlap_s: float = 0.2

    # Post-processing constraints (lenient, broad)
    merge_gap_s: float = 1.0       # Merge segments if gap < 1 s
    min_segment_s: float = 1.0     # Drop segments shorter than this
    max_segment_s_for_split: float = 29.0  # Secondary split threshold


# ---------------------------------------------------------------------------
# VadProcessor
# ---------------------------------------------------------------------------

class VadProcessor:
    """High-recall VAD processor using Silero VAD.

    Produces broad audio segments ("search windows") suitable for
    downstream ASR transcription.  The segments intentionally over-estimate
    speech boundaries to guarantee that no word is cut in the middle.
    """

    # Class-level model cache (thread-safe)
    _silero_vad_model: Any = None
    _silero_vad_utils: Any = None
    _silero_vad_lock = threading.Lock()

    def __init__(self, config: VadConfig, logger: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self._temp_dirs: List[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect_speech_segments(
        self,
        wav_path: str,
        total_duration_s: float,
    ) -> Optional[List[Tuple[float, float]]]:
        """Run VAD on *wav_path* and return broad speech segments.

        For long audio (> chunk_window_s) the file is processed in
        overlapping chunks and the per-chunk results are merged.

        Returns:
            Sorted list of ``(start_s, end_s)`` tuples, or *None* on failure.
        """
        try:
            if total_duration_s > self.config.chunk_window_s:
                return self._detect_chunked(wav_path, total_duration_s)
            else:
                return self._run_vad_on_audio(wav_path, total_duration_s)
        except Exception as exc:
            self.logger.warning(f"VAD processing failed: {exc}")
            return None

    def cleanup(self):
        """Remove temporary files created during processing."""
        import shutil
        for d in self._temp_dirs:
            try:
                if os.path.exists(d):
                    shutil.rmtree(d)
            except Exception as exc:
                # Best-effort cleanup: log and continue without raising.
                self.logger.warning("Failed to remove temporary directory %s: %s", d, exc)
        self._temp_dirs.clear()

    # ------------------------------------------------------------------
    # Silero model loading (class-level singleton, thread-safe)
    # ------------------------------------------------------------------

    def _load_silero_vad(self):
        if VadProcessor._silero_vad_model is not None and VadProcessor._silero_vad_utils is not None:
            return VadProcessor._silero_vad_model, VadProcessor._silero_vad_utils
        with VadProcessor._silero_vad_lock:
            if VadProcessor._silero_vad_model is not None and VadProcessor._silero_vad_utils is not None:
                return VadProcessor._silero_vad_model, VadProcessor._silero_vad_utils
            try:
                from silero_vad import load_silero_vad, get_speech_timestamps
                model = load_silero_vad()
                utils = {'get_speech_timestamps': get_speech_timestamps}
                VadProcessor._silero_vad_model = model
                VadProcessor._silero_vad_utils = utils
                self.logger.info("Silero VAD model loaded successfully (local)")
                return model, utils
            except ImportError:
                self.logger.error("Missing silero-vad dependency – pip install silero-vad torch")
                raise
            except Exception as exc:
                self.logger.error(f"Failed to load Silero VAD model: {exc}")
                raise

    # ------------------------------------------------------------------
    # Core VAD execution
    # ------------------------------------------------------------------

    def _run_vad_on_audio(
        self, wav_path: str, total_duration_s: float
    ) -> Optional[List[Tuple[float, float]]]:
        """Run Silero VAD on a single WAV file and return constrained segments."""
        try:
            import torch
            import numpy as np

            model, utils = self._load_silero_vad()
            get_speech_timestamps = utils['get_speech_timestamps']

            with wave.open(wav_path, 'rb') as wf:
                sample_rate = wf.getframerate()
                channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                total_frames = wf.getnframes()

                if sample_rate != 16000 or channels != 1:
                    self.logger.warning(
                        f"VAD requires 16 kHz mono; got {sample_rate} Hz {channels}-ch – skipping"
                    )
                    return None

                audio_bytes = wf.readframes(total_frames)

            duration_from_wav = total_frames / float(sample_rate)
            if duration_from_wav < 0.5:
                self.logger.warning(f"Audio too short ({duration_from_wav:.3f}s) – skipping VAD")
                return None

            if sample_width == 2:
                audio_array = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            elif sample_width == 4:
                audio_array = np.frombuffer(audio_bytes, dtype=np.int32).astype(np.float32) / 2147483648.0
            else:
                self.logger.warning(f"Unsupported sample width: {sample_width} bytes")
                return None

            audio_tensor = torch.from_numpy(audio_array)

            self.logger.info(
                f"VAD processing: {duration_from_wav:.2f}s, {total_frames} frames, "
                f"range [{audio_array.min():.3f}, {audio_array.max():.3f}]"
            )

            speech_timestamps = get_speech_timestamps(
                audio_tensor,
                model,
                threshold=self.config.threshold,
                min_speech_duration_ms=self.config.min_speech_ms,
                min_silence_duration_ms=self.config.min_silence_ms,
                speech_pad_ms=self.config.speech_pad_ms,
                max_speech_duration_s=self.config.max_speech_s,
                sampling_rate=sample_rate,
                return_seconds=True,
            )

            if not speech_timestamps:
                self.logger.debug("VAD detected no speech segments")
                return None

            raw_pairs: List[Tuple[float, float]] = [
                (float(seg['start']), float(seg['end']))
                for seg in speech_timestamps
                if float(seg['end']) > float(seg['start'])
            ]

            if not raw_pairs:
                return None

            self.logger.info(f"VAD detected {len(raw_pairs)} raw segments")
            return self._apply_constraints(raw_pairs)
        except ImportError:
            self.logger.error("VAD requires silero-vad + torch – pip install silero-vad torch")
            return None
        except Exception as exc:
            self.logger.warning(f"VAD exception: {exc}")
            return None

    # ------------------------------------------------------------------
    # Long-audio chunked processing
    # ------------------------------------------------------------------

    def _detect_chunked(
        self, wav_path: str, total_duration_s: float
    ) -> Optional[List[Tuple[float, float]]]:
        """Process long audio by splitting into overlapping chunks."""
        chunks = self._create_chunks(total_duration_s)
        self.logger.info(
            f"VAD chunked processing: {total_duration_s:.1f}s, "
            f"window {self.config.chunk_window_s}s, "
            f"overlap {self.config.chunk_overlap_s}s, {len(chunks)} chunks"
        )

        all_segments: List[Tuple[float, float]] = []
        consecutive_failures = 0

        for chunk_start, chunk_end in chunks:
            chunk_wav = self._extract_audio_clip(wav_path, chunk_start, chunk_end)
            if not chunk_wav:
                continue

            chunk_duration = chunk_end - chunk_start
            chunk_segments = self._run_vad_on_audio(chunk_wav, chunk_duration)

            if chunk_segments:
                adjusted = [(s + chunk_start, e + chunk_start) for s, e in chunk_segments]
                all_segments.extend(adjusted)
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                self.logger.warning(f"VAD chunk failed ({consecutive_failures}/3)")
                if consecutive_failures >= 3:
                    self.logger.error("VAD failed on 3 consecutive chunks – aborting")
                    return None

        if not all_segments:
            return None

        return self._apply_constraints(all_segments)

    def _create_chunks(self, total_duration_s: float) -> List[Tuple[float, float]]:
        """Create overlapping time windows for chunked processing."""
        window = self.config.chunk_window_s
        overlap = self.config.chunk_overlap_s
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

    # ------------------------------------------------------------------
    # Post-processing constraints (broad / lenient)
    # ------------------------------------------------------------------

    def _apply_constraints(
        self, segments: List[Tuple[float, float]]
    ) -> List[Tuple[float, float]]:
        """Merge nearby segments, drop tiny ones, split overly-long ones."""
        if not segments:
            return []

        segments = sorted(segments, key=lambda x: x[0])

        # 1. Merge gaps < merge_gap_s
        merge_gap = self.config.merge_gap_s
        merged: List[List[float]] = []
        for start, end in segments:
            if not merged:
                merged.append([start, end])
            else:
                last = merged[-1]
                if start - last[1] < merge_gap:
                    last[1] = max(last[1], end)
                else:
                    merged.append([start, end])

        # 2. Filter segments shorter than min_segment_s
        min_dur = self.config.min_segment_s
        filtered: List[List[float]] = []
        i = 0
        while i < len(merged):
            seg = merged[i]
            duration = seg[1] - seg[0]
            if duration < min_dur:
                if filtered:
                    filtered[-1][1] = seg[1]
                elif i < len(merged) - 1:
                    merged[i + 1] = [seg[0], merged[i + 1][1]]
                    i += 1
                    continue
                else:
                    filtered.append(seg)
            else:
                filtered.append(seg)
            i += 1

        # 3. Split extremely long segments
        max_dur = max(self.config.max_segment_s_for_split, 60.0)
        final: List[Tuple[float, float]] = []
        for seg in filtered:
            duration = seg[1] - seg[0]
            if duration > max_dur:
                self.logger.info(f"Force-splitting long segment: {duration:.1f}s > {max_dur:.1f}s")
                t = seg[0]
                while t < seg[1]:
                    t_end = min(t + max_dur, seg[1])
                    final.append((t, t_end))
                    t = t_end
            else:
                final.append((seg[0], seg[1]))

        self.logger.info(
            f"VAD constraints: {len(segments)} raw → {len(merged)} merged → "
            f"{len(filtered)} filtered → {len(final)} final"
        )
        return final

    # ------------------------------------------------------------------
    # Audio clip extraction helper
    # ------------------------------------------------------------------

    def _extract_audio_clip(
        self, wav_path: str, start_s: float, end_s: float
    ) -> Optional[str]:
        """Extract a WAV clip using ffmpeg. Returns path to temp file."""
        try:
            ffmpeg_bin = get_ffmpeg_path(logger=self.logger) or 'ffmpeg'
            out_dir = tempfile.mkdtemp(prefix='y2a_vad_clip_')
            self._temp_dirs.append(out_dir)
            out_wav = os.path.join(out_dir, 'clip.wav')
            dur = max(0.01, end_s - start_s)
            cmd = [
                ffmpeg_bin, '-y',
                '-ss', f"{start_s:.3f}", '-t', f"{dur:.3f}",
                '-i', wav_path,
                '-ac', '1', '-ar', '16000', '-acodec', 'pcm_s16le', '-f', 'wav',
                out_wav,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=120,
            )
            if result.returncode != 0 or not os.path.exists(out_wav):
                self.logger.warning(
                    "Audio clip extraction failed (rc=%d, %s–%ss): %s",
                    result.returncode, f"{start_s:.3f}", f"{end_s:.3f}",
                    (result.stderr or '')[:200],
                )
                return None

            with wave.open(out_wav, 'rb') as wf:
                actual_dur = wf.getnframes() / wf.getframerate() if wf.getframerate() > 0 else 0.0
                if actual_dur < 0.1:
                    self.logger.warning(
                        "Extracted clip too short (%.3fs) for %.3f–%.3fs – skipping",
                        actual_dur, start_s, end_s,
                    )
                    return None

            return out_wav
        except Exception as exc:
            self.logger.warning("Audio clip extraction exception (%s–%ss): %s", f"{start_s:.3f}", f"{end_s:.3f}", exc)
            return None
