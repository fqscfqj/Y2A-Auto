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
    chunk_window_s: float = 15.0
    chunk_overlap_s: float = 0.4
    merge_gap_s: float = 0.35
    min_segment_s: float = 0.8
    max_segment_s: float = 15.0
    max_segment_s_for_split: float = 15.0
    refinement_enabled: bool = True
    min_speech_coverage_ratio: float = 0.01
    # 孤立极短段（既合并不进前一窗、也合并不进后一窗）是否丢弃。
    # 需放在数据类末尾并带默认值，保持 replace() 构造的下游配置自动继承。
    drop_isolated_short: bool = True


class VadProcessor:
    # 分片窗口硬下限：VAD_MAX_SEGMENT_S 被误配得过小时，不得让 chunk 窗口
    # 跟着塌缩（3s 窗口会把 1 小时音频切成约 1385 片，且硬切点容易落在词中间）。
    _MIN_CHUNK_WINDOW_S = 3.0

    _silero_vad_model: Any = None
    _silero_vad_utils: Any = None
    _silero_vad_lock = threading.Lock()
    _silero_vad_inference_lock = threading.Lock()

    def __init__(self, config: VadConfig, logger: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self._temp_dirs: List[str] = []
        self.last_result_state: str = 'unknown'
        self.last_failure_reason: str = ''
        self.last_speech_coverage_ratio: float = 0.0
        self._last_run_failed: bool = False
        self._last_run_failure_reason: str = ''
        self._warned_narrow_cap: bool = False
        # 本轮检测的切片目录：所有 _extract_audio_clip 输出集中到此目录，
        # 并在 _run_vad_on_audio 读入内存后尽力删除，避免临时盘累积。
        self._clip_dir: Optional[str] = None
        self._clip_seq: int = 0
        self.last_dropped_short_count: int = 0
        self.last_refine_dropped_count: int = 0

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
        # 本轮运行态一律在公开入口重置，保证任何异常路径下 finally 汇总与
        # 后续读取到的都是本轮数据（不依赖内部实现是否被替换/包装）。
        self.last_dropped_short_count = 0
        self.last_refine_dropped_count = 0
        # 新一轮检测：切片目录与序号重置，旧目录仍留在 _temp_dirs 由 cleanup() 统一删除。
        self._clip_dir = None
        self._clip_seq = 0
        try:
            return self._detect_speech_windows_impl(wav_path, total_duration_s)
        except Exception as exc:
            self.last_result_state = 'failure'
            self.last_failure_reason = str(exc)
            self.logger.warning("VAD processing failed: %s", exc)
            return None
        finally:
            # 本轮丢弃汇总统一在此输出一次：_refine_windows 会被 primary 与
            # relaxed 两轮 pass 各调用一次，若在其内部汇总会把同一批丢弃事件
            # 重复打印，且第二次携带的是未重置的累积计数（中间值失真）。
            if self.last_dropped_short_count > 0:
                self.logger.info(
                    "VAD dropped %d isolated short segments (< drop threshold)",
                    self.last_dropped_short_count,
                )
            if self.last_refine_dropped_count > 0:
                self.logger.info(
                    "Refine pass dropped %d silent/pseudo windows in total",
                    self.last_refine_dropped_count,
                )

    def _detect_speech_windows_impl(
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
            primary_pass_partial = self.last_result_state == 'partial'
            primary_coverage = self._windows_coverage_ratio(primary_windows, total_duration_s)
            self.last_speech_coverage_ratio = primary_coverage

            if primary_windows and primary_coverage >= max(0.0, float(self.config.min_speech_coverage_ratio or 0.0)):
                # 部分分片失败时结果仍然可用，但来源退化，状态必须保留为 partial，
                # 交由上层（speech_recognition._transcribe_with_vad）升级为 degraded 严格质检。
                if primary_pass_partial:
                    self.last_result_state = 'partial'
                    self.last_failure_reason = self.last_failure_reason or 'vad_partial_chunks'
                else:
                    self.last_result_state = 'success'
                return primary_windows

            relaxed_config = self._build_relaxed_retry_config()
            should_retry_relaxed = (
                primary_windows is None
                or not primary_windows
                or primary_coverage < max(0.0, float(self.config.min_speech_coverage_ratio or 0.0))
            )
            if should_retry_relaxed:
                self.last_result_state = 'unknown'
                relaxed_windows = self._detect_windows_with_config(
                    wav_path,
                    total_duration_s,
                    relaxed_config,
                    source_pass='relaxed_retry',
                )
                relaxed_pass_partial = self.last_result_state == 'partial'
                relaxed_coverage = self._windows_coverage_ratio(relaxed_windows, total_duration_s)
                if relaxed_windows and relaxed_coverage > primary_coverage:
                    self.last_speech_coverage_ratio = relaxed_coverage
                    if primary_pass_partial or relaxed_pass_partial:
                        self.last_result_state = 'partial'
                        self.last_failure_reason = self.last_failure_reason or 'vad_partial_chunks'
                    else:
                        self.last_result_state = 'success'
                    return relaxed_windows
                if primary_windows and primary_coverage > 0.0:
                    if primary_pass_partial:
                        self.last_result_state = 'partial'
                        self.last_failure_reason = self.last_failure_reason or 'vad_partial_chunks'
                    else:
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

            if primary_pass_partial:
                self.last_result_state = 'partial'
                self.last_failure_reason = self.last_failure_reason or 'vad_partial_chunks'
                return primary_windows

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

            # 音频已载入内存，切片文件不再需要：若它属于本轮检测的 _clip_dir，
            # 立即尽力删除，把临时盘占用从 O(chunk 数) 降到实际峰值附近的 O(1)。
            # 失败忽略——清理是 best-effort，绝不影响检测主流程。
            self._release_clip_file(wav_path)

            duration_from_wav = total_frames / float(sample_rate)
            if duration_from_wav < 0.5:
                reason = f"audio too short: {duration_from_wav:.3f}s"
                self._set_run_state(True, reason)
                self.logger.info("Audio too short for VAD, treating as no speech: %s", reason)
                return []  # 返回空列表表示无语音，而非None表示检测失败

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
            # Silero JIT VAD mutates internal recurrent state during inference.
            # The shared singleton model is therefore guarded across tasks.
            with VadProcessor._silero_vad_inference_lock:
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
                    time_resolution=2,
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
        except ImportError as exc:
            self._set_run_state(True, "missing silero-vad or torch dependency")
            self.logger.error("VAD dependency import failed: %s", exc)
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
        # failed_chunks 记录切片/推理失败的 chunk 数；分母用 total_chunks（即 ownership
        # 划分所用的同一 chunk 总数），保证部分失败占比与窗口归属口径一致。
        failed_chunks = 0

        for chunk_index, (chunk_start, chunk_end) in enumerate(chunks):
            chunk_wav = self._extract_audio_clip(wav_path, chunk_start, chunk_end)
            if not chunk_wav:
                consecutive_failures += 1
                failed_chunks += 1
                self._set_run_state(True, "audio clip extraction failed")
                self.logger.warning(
                    "VAD chunk %d/%d failed: audio clip extraction failed",
                    chunk_index + 1,
                    total_chunks,
                )
                if consecutive_failures >= 3 and not chunk_windows:
                    return None
                continue

            chunk_duration = chunk_end - chunk_start
            raw_pairs = self._run_vad_on_audio(chunk_wav, chunk_duration, config)
            if raw_pairs is None:
                consecutive_failures += 1
                failed_chunks += 1
                self.logger.warning(
                    "VAD chunk %d/%d failed: %s",
                    chunk_index + 1,
                    total_chunks,
                    self._last_run_failure_reason or 'vad_chunk_failed',
                )
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
            # chunk 内部合并使用用户配置的 VAD_MERGE_GAP_S（allow_gap_merge=True），
            # 跨 chunk 缝合才退化为 chunk_overlap_s/2 + 0.02 的小间隙。
            constrained = self._apply_constraints(adjusted, config=config, allow_gap_merge=True)
            keep_start, keep_end = self._ownership_range(
                config,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                chunk_start=chunk_start,
                chunk_end=chunk_end,
            )
            # 覆盖率分母必须是本 chunk 的 ownership 区间长度（实际采纳的音频范围），
            # 用含重叠的 chunk 全长会系统性低估覆盖比例。
            coverage_ratio = self._pairs_coverage_ratio(constrained, max(keep_end - keep_start, 0.01))
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

        failure_ratio = failed_chunks / max(1, total_chunks)
        if failed_chunks > 0 and chunk_windows:
            if failure_ratio >= 0.5:
                # 过半分片失败：不交付只覆盖前半段的部分字幕，交由上层走全量 fallback。
                self._set_run_state(True, 'vad_partial_chunks')
                self.logger.warning(
                    "VAD chunked pass aborted: %d/%d chunks failed (ratio %.2f >= 0.50), discarding partial result",
                    failed_chunks,
                    total_chunks,
                    failure_ratio,
                )
                return None
            # 少量分片失败：结果仍可用但覆盖不完整，标记为 partial 让上层升级严格质检。
            self.last_result_state = 'partial'
            self.last_failure_reason = 'vad_partial_chunks'
            self.logger.warning(
                "VAD chunked pass partial: %d/%d chunks failed (ratio %.2f); returning degraded windows",
                failed_chunks,
                total_chunks,
                failure_ratio,
            )

        if not chunk_windows:
            return []
        merged_windows = self._merge_windows(
            chunk_windows,
            config=config,
            allow_gap_merge=False,
            stitch_gap_s=max(0.0, float(config.chunk_overlap_s or 0.0) / 2.0 + 0.02),
        )
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
            # 过短窗口不值得再切一次音频并跑一轮推理：精修收益低于成本。
            if window.duration_s < 1.5:
                refined_windows.append(window)
                continue
            clip = self._extract_audio_clip(wav_path, window.start_s, window.end_s)
            if not clip:
                refined_windows.append(window)
                continue
            local_pairs = self._run_vad_on_audio(clip, window.duration_s, refined_config)
            if local_pairs is None:
                # 精修推理失败：无法判定，保守回退原窗口。
                refined_windows.append(window)
                continue
            if not local_pairs:
                # 精修判定「无语音」：该窗口是静音或伪窗口（幻觉来源），必须丢弃，
                # 不能像失败那样回退原窗口。
                self.last_refine_dropped_count += 1
                self.logger.info(
                    "Refine pass dropped window [%.2fs-%.2fs] duration=%.2fs: no local speech",
                    window.start_s,
                    window.end_s,
                    window.duration_s,
                )
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
                    # 保留原始 source_pass：精修不是新的检测 pass，覆盖它会污染诊断口径。
                    threshold=refined_config.threshold,
                    coverage_ratio=self._pairs_coverage_ratio(local_pairs, max(window.duration_s, 0.01)),
                    speech_duration_s=sum(max(0.0, e - s) for s, e in local_pairs),
                    refined=True,
                    raw_spans=refined_pairs,
                    # 传新 dict，避免 replace 共享原窗口的 metadata 引用。
                    metadata={
                        **(window.metadata or {}),
                        'refined': True,
                        'refine_threshold': refined_config.threshold,
                    },
                )
            )
        # 精修丢弃汇总不在此输出：detect_speech_windows 的 finally 统一汇报一次，
        # 避免两轮 pass 重复打印同一批事件与累积计数的中间值。
        return self._merge_windows(
            refined_windows,
            config=config,
            allow_gap_merge=False,
            stitch_gap_s=max(0.0, float(config.chunk_overlap_s or 0.0) / 2.0 + 0.02),
        )

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
        stitch_gap_s: Optional[float] = None,
    ) -> List[Tuple[float, float]]:
        if not segments:
            return []
        segments = sorted((float(start), float(end)) for start, end in segments if end > start)
        if not segments:
            return []
        max_dur = self._effective_split_limit_s(config)
        merge_gap = max(0.0, float(config.merge_gap_s or 0.0))
        if stitch_gap_s is None:
            stitch_gap_s = max(0.0, float(config.chunk_overlap_s or 0.0) / 2.0 + 0.02)
        else:
            stitch_gap_s = max(0.0, float(stitch_gap_s))

        merged: List[List[float]] = []
        for start, end in segments:
            if not merged:
                merged.append([start, end])
                continue
            last = merged[-1]
            boundary_gap = start - last[1]
            combined_duration = max(last[1], end) - last[0]
            merge_limit = merge_gap if allow_gap_merge else stitch_gap_s
            can_merge = boundary_gap <= 0.0 or (
                boundary_gap <= merge_limit and combined_duration <= max_dur
            )
            if can_merge:
                last[1] = max(last[1], end)
            else:
                merged.append([start, end])

        min_dur = max(0.0, float(config.min_segment_s or 0.0))
        # 孤立极短段的丢弃阈值：既低于 min_segment_s 的一半、又低于 0.4s 的绝对下限，
        # 才视为真正没有 ASR 价值的碎片（默认丢弃，可用 drop_isolated_short=False 保留）。
        drop_threshold = max(0.4, min_dur * 0.5)
        drop_isolated_short = bool(getattr(config, 'drop_isolated_short', True))
        filtered: List[List[float]] = []
        idx = 0
        while idx < len(merged):
            seg = merged[idx]
            duration = seg[1] - seg[0]
            if duration < min_dur:
                merge_limit = merge_gap if allow_gap_merge else stitch_gap_s
                previous_gap = seg[0] - filtered[-1][1] if filtered else float('inf')
                next_seg = merged[idx + 1] if idx < len(merged) - 1 else None
                next_gap = next_seg[0] - seg[1] if next_seg else float('inf')

                can_merge_previous = (
                    bool(filtered)
                    and previous_gap <= merge_limit
                    and seg[1] - filtered[-1][0] <= max_dur
                )
                can_merge_next = (
                    next_seg is not None
                    and next_gap <= merge_limit
                    and next_seg[1] - seg[0] <= max_dur
                )

                if can_merge_previous and (not can_merge_next or previous_gap <= next_gap):
                    filtered[-1][1] = seg[1]
                elif can_merge_next:
                    next_seg[0] = seg[0]
                elif drop_isolated_short and duration < drop_threshold:
                    # 孤立极短段：Silero 已过滤 sub-min_speech 噪声，此类碎片送 ASR
                    # 只会产出幻觉或空白，按显式策略丢弃而不是无条件保留。
                    self.last_dropped_short_count += 1
                    self.logger.info(
                        "Dropping isolated short segment [%.2fs-%.2fs] duration=%.2fs (< %.2fs)",
                        seg[0],
                        seg[1],
                        duration,
                        drop_threshold,
                    )
                else:
                    # Silero has already filtered sub-min_speech noise. Keep an
                    # isolated short utterance instead of spanning arbitrary
                    # silence to force it into a neighbouring ASR window.
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
        stitch_gap_s: Optional[float] = None,
    ) -> List[DetectedSpeechWindow]:
        if not windows:
            return []
        merged_segments = self._apply_constraints(
            [(w.start_s, w.end_s) for w in windows],
            config=config,
            allow_gap_merge=allow_gap_merge,
            stitch_gap_s=stitch_gap_s,
        )
        merged_windows: List[DetectedSpeechWindow] = []
        for start, end in merged_segments:
            matched = [w for w in windows if not (w.end_s <= start or w.start_s >= end)]
            if not matched:
                continue
            # metadata 逐窗口合并（后者覆盖前者），否则精修标记会在 merge 重建时丢失。
            merged_metadata: dict = {}
            for window in matched:
                merged_metadata.update(window.metadata or {})
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
                    metadata=merged_metadata,
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
        # 口径变更：覆盖率语义是「语音时长占比」，此前累加窗口全长
        # （含 speech_pad 与窗内静音）会系统性高估 2-4 倍，导致
        # VAD_MIN_SPEECH_COVERAGE_RATIO 与 vad_low_coverage 几乎不触发。
        # speech_duration_s 为 0（如精修前未填充）时退回窗口全长，避免误算成 0。
        total_speech = sum(
            float(w.speech_duration_s)
            if float(w.speech_duration_s) > 0.0
            else max(0.0, w.end_s - w.start_s)
            for w in windows
        )
        total_window_span = sum(
            max(0.0, float(w.end_s) - float(w.start_s))
            for w in windows
        )
        ratio = total_speech / max(float(total_duration_s or 0.0), 0.01)
        # 仅 debug：同时给出旧口径（窗口全长占比）与新旧比值，便于用真实任务
        # 标定阈值。生产默认 INFO 级，不产生额外日志量。
        if self.logger.isEnabledFor(logging.DEBUG):
            legacy_ratio = total_window_span / max(float(total_duration_s or 0.0), 0.01)
            self.logger.debug(
                "VAD coverage ratio: new=%.5f (speech %.2fs / %.2fs), legacy=%.5f (window span %.2fs), boost=%.2fx, threshold=%.5f",
                ratio,
                total_speech,
                max(float(total_duration_s or 0.0), 0.01),
                legacy_ratio,
                total_window_span,
                (legacy_ratio / ratio) if ratio > 0.0 else 0.0,
                max(0.0, float(self.config.min_speech_coverage_ratio or 0.0)),
            )
        return ratio

    def _build_relaxed_retry_config(self) -> VadConfig:
        return replace(
            self.config,
            threshold=max(0.35, float(self.config.threshold) - 0.12),
            min_speech_ms=max(120, int(self.config.min_speech_ms * 0.6)),
            min_silence_ms=max(160, int(self.config.min_silence_ms * 0.6)),
            speech_pad_ms=min(320, int(self.config.speech_pad_ms + 60)),
        )

    def _build_refinement_config(self, config: VadConfig) -> VadConfig:
        return replace(
            config,
            threshold=min(0.80, float(config.threshold) + 0.08),
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
        cap = self._effective_vad_cap_s(active)
        raw_cap = getattr(active, 'max_segment_s', None)
        try:
            raw_cap = float(raw_cap) if raw_cap is not None else None
        except (TypeError, ValueError):
            raw_cap = None
        # 只对「确有窄 cap 配置」的情况告警一次，避免对缺失字段误报。
        if raw_cap is not None and raw_cap < 5.0 and not self._warned_narrow_cap:
            self._warned_narrow_cap = True
            self.logger.warning(
                "VAD_MAX_SEGMENT_S=%.2fs 小于 5 秒会让分片窗口过窄，建议 >= 5",
                raw_cap,
            )
        if window <= 0.0:
            window = cap
        # 下限保护：即使 VAD_MAX_SEGMENT_S（cap）被配得过小，chunk 窗口也不得塌缩，
        # 否则长音频会被切成上千片，且硬切点容易落在词中间。
        return max(self._MIN_CHUNK_WINDOW_S, min(window, cap))

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

    def _release_clip_file(self, wav_path: str) -> None:
        """尽力删除本轮 run 的切片文件（音频已读入内存后调用）。

        只删除位于 ``self._clip_dir`` 内的文件，绝不触碰调用方传入的原始音频。
        失败静默忽略：临时文件清理是 best-effort，不能影响检测结果。
        """
        clip_dir = self._clip_dir
        if not clip_dir or not wav_path:
            return
        try:
            if os.path.dirname(os.path.abspath(wav_path)) != os.path.abspath(clip_dir):
                return
            if os.path.exists(wav_path):
                os.remove(wav_path)
        except Exception:
            pass

    def _extract_audio_clip(self, wav_path: str, start_s: float, end_s: float) -> Optional[str]:
        try:
            ffmpeg_bin = get_ffmpeg_path(logger=self.logger) or 'ffmpeg'
            # 所有切片输出到同一个 run-scoped 目录，避免每次调用都 mkdtemp 造成
            # 目录数随 chunk 数线性增长；文件名用递增序号避免冲突。
            # 目录本身仍登记在 _temp_dirs 中，由 cleanup() 兜底删除。
            if not self._clip_dir:
                self._clip_dir = tempfile.mkdtemp(prefix='y2a_vad_clip_')
                self._temp_dirs.append(self._clip_dir)
            self._clip_seq += 1
            out_wav = os.path.join(self._clip_dir, f'clip_{self._clip_seq:06d}.wav')
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
