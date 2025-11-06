#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import tempfile
import subprocess
import logging
import wave
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict, Any
import re
import json
import requests


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
    # Deprecated: language detection uses the same Whisper settings now
    detect_api_key: str = ''  # deprecated, ignored
    detect_base_url: str = ''  # deprecated, ignored
    detect_model_name: str = ''  # deprecated, ignored
    # Gating: treat as no subtitles if cues less than threshold
    min_lines_enabled: bool = True
    min_lines_threshold: int = 5

    # VAD settings
    vad_enabled: bool = False
    vad_provider: str = 'silero'
    vad_api_url: str = ''
    vad_api_token: str = ''
    vad_threshold: float = 0.5
    vad_min_speech_ms: int = 250
    vad_min_silence_ms: int = 100
    vad_max_speech_s: int = 120
    vad_speech_pad_ms: int = 30
    vad_max_segment_s: int = 90

    # Audio chunking settings (for long audio processing)
    chunk_window_s: float = 25.0  # Fixed window size for chunking (20-30s recommended)
    chunk_overlap_s: float = 0.2  # Overlap between chunks to ensure continuity

    # VAD post-processing constraints (control subtitle granularity)
    vad_merge_gap_s: float = 0.25  # Merge segments if gap < this value (0.20-0.25s)
    vad_min_segment_s: float = 1.0  # Minimum segment duration (0.8-1.2s)
    vad_max_segment_s_for_split: float = 8.0  # Max segment before secondary splitting (8-10s)
    vad_silence_threshold_s: float = 0.3  # Silence duration for secondary splitting

    # Transcription settings
    language: str = ''  # Force language (e.g., 'en', 'zh', 'ja'), empty = auto-detect
    prompt: str = ''  # Optional prompt to guide transcription
    translate: bool = False  # Translate to English
    max_workers: int = 3  # Parallel transcription workers (2-4 recommended)

    # Text post-processing settings
    max_subtitle_line_length: int = 42  # Max characters per line
    max_subtitle_lines: int = 2  # Max lines per subtitle cue
    normalize_punctuation: bool = True  # Normalize punctuation
    filter_filler_words: bool = False  # Remove filler words like "um", "uh"

    # Retry and fallback settings
    max_retries: int = 3  # Max retries for failed API calls
    retry_delay_s: float = 2.0  # Initial retry delay (with exponential backoff)
    fallback_to_fixed_chunks: bool = True  # Fallback to fixed chunks if VAD fails


class SpeechRecognizer:
    """Abstracted speech recognizer with a Whisper(OpenAI compatible) implementation."""

    def __init__(self, config: SpeechRecognitionConfig, task_id: Optional[str] = None):
        self.config = config
        self.task_id = task_id or 'unknown'
        self.logger = _setup_task_logger(self.task_id)
        self.client: Any = None  # OpenAI/Whisper client for transcription
        self._init_client()

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
            self.logger.info("语音识别客户端初始化成功")
        except Exception as e:
            self.logger.error(f"初始化语音识别客户端失败: {e}")

    def _extract_audio_wav(self, video_path: str) -> Optional[str]:
        """Extract 16kHz mono WAV from video using ffmpeg. Returns temp file path."""
        try:
            # 优先使用项目内置/配置中的 ffmpeg
            ffmpeg_bin = 'ffmpeg'
            try:
                from .youtube_handler import find_ffmpeg_location  # 复用已有定位逻辑
                ffmpeg_path = find_ffmpeg_location(logger=self.logger)
                if ffmpeg_path and os.path.exists(ffmpeg_path):
                    ffmpeg_bin = ffmpeg_path
                    self.logger.info(f"语音识别提取音频将使用 ffmpeg: {ffmpeg_bin}")
                else:
                    self.logger.info("未找到配置的ffmpeg，尝试使用系统环境中的 ffmpeg")
            except Exception as _e:
                self.logger.debug(f"定位 ffmpeg 失败，退回系统命令: {_e}")

            tmp_dir = tempfile.mkdtemp(prefix='y2a_audio_')
            audio_path = os.path.join(tmp_dir, 'audio.wav')
            cmd = [
                ffmpeg_bin, '-y',
                '-i', video_path,
                '-vn',
                '-ac', '1',
                '-ar', '16000',
                '-acodec', 'pcm_s16le',
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

    def transcribe_video_to_subtitles(self, video_path: str, output_path: str) -> Optional[str]:
        """
        Transcribe a video file into SRT/VTT subtitles using optimized strategy.
        
        Strategy (based on problem statement):
        1. Audio extraction & preprocessing (16kHz mono WAV)
        2. Chunking (20-30s window, 0.2s overlap) for long audio
        3. VAD (Silero) with post-processing constraints
        4. Parallel transcription with retry
        5. Text post-processing & subtitle assembly
        
        Returns the output_path on success, otherwise None.
        """
        try:
            if not self.client:
                self.logger.error("语音识别客户端未初始化")
                return None
            if not os.path.exists(video_path):
                self.logger.error(f"视频文件不存在: {video_path}")
                return None
            
            # Step 1: Extract audio (16kHz mono WAV)
            self.logger.info("步骤 1/5: 提取音频并预处理 (16kHz mono WAV)")
            audio_wav = self._extract_audio_wav(video_path)
            if not audio_wav:
                return None

            total_audio_duration = self._probe_media_duration(audio_wav)
            if total_audio_duration is None:
                # Fallback: use video duration
                total_audio_duration = self._probe_media_duration(video_path) or 0.0
            
            self.logger.info(f"音频时长: {total_audio_duration:.1f}s")

            model_name = self.config.model_name or 'whisper-1'
            response_format = (self.config.output_format or 'srt').lower()
            if response_format not in ('srt', 'vtt'):
                response_format = 'srt'

            cues: List[Dict[str, Any]] = []
            
            # Step 2 & 3: Chunking + VAD
            if self.config.vad_enabled and self.config.vad_api_url:
                self.logger.info("步骤 2/5: 应用 VAD (Silero) 进行语音活动检测")
                try:
                    # For long audio, process in chunks to avoid timeout
                    if total_audio_duration > self.config.chunk_window_s:
                        self.logger.info(f"音频较长，将分片处理 (窗口: {self.config.chunk_window_s}s, 重叠: {self.config.chunk_overlap_s}s)")
                        chunks = self._create_audio_chunks(total_audio_duration)
                        
                        all_vad_segments: List[Tuple[float, float]] = []
                        for chunk_start, chunk_end in chunks:
                            # Extract chunk
                            chunk_wav = self._extract_audio_clip(audio_wav, chunk_start, chunk_end)
                            if not chunk_wav:
                                continue
                            
                            # Run VAD on chunk
                            chunk_duration = chunk_end - chunk_start
                            chunk_segments = self._run_vad_on_audio(chunk_wav, chunk_duration)
                            if chunk_segments:
                                # Adjust timestamps to global time
                                adjusted_segments = [(s + chunk_start, e + chunk_start) for s, e in chunk_segments]
                                all_vad_segments.extend(adjusted_segments)
                        
                        # Merge overlapping segments from different chunks
                        if all_vad_segments:
                            all_vad_segments = self._apply_vad_constraints(all_vad_segments)
                            vad_segments = all_vad_segments
                        else:
                            vad_segments = None
                    else:
                        # Process entire audio as single chunk
                        vad_segments = self._run_vad_on_audio(audio_wav, total_audio_duration)
                    
                    if vad_segments:
                        self.logger.info(f"步骤 3/5: VAD 检测到 {len(vad_segments)} 个语音片段，开始转写")
                        
                        # Step 4: Parallel transcription
                        # For now, process sequentially (parallel can be added with ThreadPoolExecutor)
                        for seg_start_s, seg_end_s in vad_segments:
                            seg_start_s = max(0.0, float(seg_start_s))
                            seg_end_s = max(seg_start_s, float(seg_end_s))
                            
                            # Extract segment audio
                            part_wav = self._extract_audio_clip(audio_wav, seg_start_s, seg_end_s)
                            if part_wav:
                                segment_cues = self._transcribe_one_clip(part_wav, seg_start_s, total_audio_duration)
                                cues.extend(segment_cues)
                        
                        if not cues:
                            self.logger.warning("VAD 模式下未生成任何字幕，回退到整段识别")
                    else:
                        if self.config.fallback_to_fixed_chunks:
                            self.logger.warning("VAD 未返回有效片段，回退到固定窗口切分")
                            # Fallback to fixed chunks
                            chunks = self._create_audio_chunks(total_audio_duration)
                            for chunk_start, chunk_end in chunks:
                                chunk_wav = self._extract_audio_clip(audio_wav, chunk_start, chunk_end)
                                if chunk_wav:
                                    chunk_cues = self._transcribe_one_clip(chunk_wav, chunk_start, total_audio_duration)
                                    cues.extend(chunk_cues)
                        else:
                            self.logger.warning("VAD 未返回有效片段，回退到整段识别")
                except Exception as e:
                    self.logger.warning(f"VAD 处理失败: {e}")
                    if self.config.fallback_to_fixed_chunks:
                        self.logger.info("回退到整段识别")

            if not cues:
                # Fallback: Whole audio transcription
                self.logger.info(f"步骤 3/5: 整段音频转写 (模型: {model_name})")
                
                # For very long audio, use chunking even without VAD
                if total_audio_duration > self.config.chunk_window_s * 2:
                    self.logger.info("音频过长，使用固定窗口分片")
                    chunks = self._create_audio_chunks(total_audio_duration)
                    for chunk_start, chunk_end in chunks:
                        chunk_wav = self._extract_audio_clip(audio_wav, chunk_start, chunk_end)
                        if chunk_wav:
                            chunk_cues = self._transcribe_one_clip(chunk_wav, chunk_start, total_audio_duration)
                            cues.extend(chunk_cues)
                else:
                    # Single transcription for short audio
                    with open(audio_wav, 'rb') as f:
                        try:
                            params = {
                                'model': model_name,
                                'file': f,
                                'response_format': 'verbose_json'
                            }
                            if self.config.language:
                                params['language'] = self.config.language
                            if self.config.prompt:
                                params['prompt'] = self.config.prompt
                            
                            resp = self.client.audio.transcriptions.create(**params)
                        except Exception as e:
                            self.logger.error(f"语音转写请求失败: {e}")
                            return None

                    cues = self._convert_response_to_cues(resp, 0.0, total_audio_duration)
                    if not cues:
                        self.logger.error("转写响应未生成任何字幕片段")
                        return None
                    
                    # Apply text normalization and splitting for whole transcription
                    normalized_cues = []
                    for cue in cues:
                        cue['text'] = self._normalize_subtitle_text(cue['text'])
                        if not cue['text']:
                            continue
                        split_cues = self._split_cue_by_text_length(cue)
                        normalized_cues.extend(split_cues)
                    cues = normalized_cues

            # Step 5: Render subtitles
            self.logger.info(f"步骤 4/5: 渲染字幕 (格式: {response_format}, 共 {len(cues)} 个片段)")
            text = self._render_cues(cues, response_format)
            if not text:
                self.logger.error("无法从转写结果渲染字幕")
                return None

            with open(output_path, 'w', encoding='utf-8') as out:
                out.write(text)

            # Step 6: Quality check
            self.logger.info("步骤 5/5: 质量检查")
            try:
                if self.config.min_lines_enabled:
                    cue_count = self._count_subtitle_cues(output_path, response_format)
                    self.logger.info(f"生成字幕条目数: {cue_count}")
                    if isinstance(cue_count, int) and cue_count < max(0, int(self.config.min_lines_threshold)):
                        try:
                            os.remove(output_path)
                        except Exception:
                            pass
                        self.logger.info(
                            f"ASR字幕少于阈值({self.config.min_lines_threshold})，视为无字幕，丢弃文件")
                        return None
            except Exception as _e:
                self.logger.warning(f"ASR字幕条目数统计失败，忽略门槛检查: {_e}")

            self.logger.info(f"✓ 语音转写完成，字幕已保存: {output_path}")
            return output_path
        except Exception as e:
            self.logger.error(f"语音转写失败: {e}")
            import traceback
            self.logger.error(f"详细错误: {traceback.format_exc()}")
            return None

    # ---- Helpers for OpenAI response normalization and rendering ----
    def _as_dict(self, resp) -> Optional[dict]:
        try:
            if isinstance(resp, dict):
                return resp
            if hasattr(resp, 'model_dump'):
                return resp.model_dump()
            if hasattr(resp, 'to_dict'):
                return resp.to_dict()
            d = getattr(resp, '__dict__', None)
            if isinstance(d, dict):
                return d
            # last resort: try JSON loads on str
            import json as _json
            return _json.loads(str(resp))
        except Exception:
            return None

    @staticmethod
    def _format_timestamp(seconds: float) -> str:
        if seconds is None:
            seconds = 0.0
        try:
            ms = int(round(seconds * 1000))
            h = ms // 3600000
            ms %= 3600000
            m = ms // 60000
            ms %= 60000
            s = ms // 1000
            ms %= 1000
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
        except Exception:
            return "00:00:00,000"

    @staticmethod
    def _srt_time_to_seconds(time_str: str) -> float:
        """Convert HH:MM:SS,mmm (or HH:MM:SS.mmm) to seconds."""
        if not time_str:
            return 0.0
        try:
            normalized = time_str.strip().replace('.', ',')
            hh, mm, rest = normalized.split(':')
            sec, ms = rest.split(',')
            return int(hh) * 3600 + int(mm) * 60 + int(sec) + int(ms) / 1000.0
        except Exception:
            return 0.0

    def _parse_subtitle_text_to_cues(self, payload: str, base_offset_s: float) -> List[Dict[str, Any]]:
        """Parse SRT/VTT payload and return cues in seconds."""
        if not payload:
            return []
        text = payload.strip()
        if not text:
            return []

        # Remove potential VTT header lines
        if text.upper().startswith('WEBVTT'):
            lines = text.splitlines()
            # Skip the first header line(s) until an empty line or cue
            idx = 1
            while idx < len(lines) and lines[idx].strip():
                idx += 1
            text = '\n'.join(lines[idx:]).strip()

        blocks = re.split(r'\n\s*\n', text)
        cues: List[Dict[str, Any]] = []
        for block in blocks:
            block_text = block.strip()
            if not block_text:
                continue
            lines = block_text.splitlines()
            if len(lines) < 2:
                continue
            # Handle optional index line
            if '-->' not in lines[0] and len(lines) >= 2 and '-->' in lines[1]:
                time_line = lines[1]
                content_lines = lines[2:]
            else:
                time_line = lines[0]
                content_lines = lines[1:]
            if '-->' not in time_line:
                continue
            try:
                start_str, end_str = [part.strip() for part in time_line.split('-->')]
            except ValueError:
                continue
            start_s = self._srt_time_to_seconds(start_str) + base_offset_s
            end_s = self._srt_time_to_seconds(end_str) + base_offset_s
            if end_s <= start_s:
                end_s = start_s + 0.01
            text_content = '\n'.join(line.strip() for line in content_lines if line.strip())
            if not text_content:
                continue
            cues.append({
                'start': max(0.0, start_s),
                'end': max(end_s, start_s + 0.01),
                'text': text_content
            })
        return cues

    def _convert_response_to_cues(self, resp: Any, base_offset_s: float, total_duration_s: float) -> List[Dict[str, Any]]:
        """Normalize Whisper/OpenAI/whisper.cpp style responses into cue list."""
        payload_dict = self._as_dict(resp)
        if isinstance(payload_dict, dict):
            cues = self._normalize_verbose_segments_to_cues(payload_dict, base_offset_s, total_duration_s)
            if cues:
                return cues

        # Fallback: try textual payload (SRT/VTT)
        text_payload: Optional[str] = None
        if isinstance(resp, str):
            text_payload = resp
        elif hasattr(resp, 'text') and isinstance(getattr(resp, 'text'), str):
            text_payload = getattr(resp, 'text')
        elif isinstance(payload_dict, dict):
            candidate = payload_dict.get('text')
            if isinstance(candidate, str) and '-->' in candidate:
                text_payload = candidate
        if text_payload:
            cues = self._parse_subtitle_text_to_cues(text_payload, base_offset_s)
            if cues:
                return cues

        return []

    # ---- Rendering and normalization helpers ----
    def _render_cues(self, cues: List[Dict[str, Any]], fmt: str) -> Optional[str]:
        fmt = (fmt or 'srt').lower()
        cues = [c for c in cues if (c.get('text') or '').strip()]
        if not cues:
            return None
        if fmt == 'vtt':
            lines = ["WEBVTT", ""]
            for c in cues:
                start_ms = self._format_timestamp(float(c['start'])).replace(',', '.')
                end_ms = self._format_timestamp(float(c['end'])).replace(',', '.')
                lines.append(f"{start_ms} --> {end_ms}")
                lines.append((c['text'] or '').strip())
                lines.append("")
            return "\n".join(lines).strip() + "\n"
        # SRT
        lines = []
        for idx, c in enumerate(cues, start=1):
            start_srt = self._format_timestamp(float(c['start']))
            end_srt = self._format_timestamp(float(c['end']))
            lines.append(str(idx))
            lines.append(f"{start_srt} --> {end_srt}")
            lines.append((c['text'] or '').strip())
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def _normalize_verbose_segments_to_cues(self, data: dict, base_offset_s: float, total_duration_s: float) -> List[Dict[str, Any]]:
        """Adapt OpenAI/whisper.cpp verbose_json payloads to cue list.

        官方 OpenAI Whisper 与 whisper.cpp 均以秒为单位返回 start/end，但仍保留
        针对兼容实现可能使用毫秒/厘秒的容错缩放，以避免再次出现长度异常的字幕文件。
        """
        segments = data.get('segments') or []
        text_fallback = (data.get('text') or '').strip()
        if not isinstance(segments, list) or not segments:
            if not text_fallback:
                return []
            return [{
                'start': base_offset_s,
                'end': base_offset_s + min( (total_duration_s or 0) - base_offset_s, 5.0) if total_duration_s else base_offset_s + 5.0,
                'text': text_fallback
            }]

        # 自动判别时间单位（s / ms / centisecond)
        max_end_raw = 0.0
        for seg in segments:
            try:
                v = float(seg.get('end', 0.0))
            except Exception:
                v = 0.0
            if v > max_end_raw:
                max_end_raw = v
        scale = self._infer_time_scale(max_end_raw, total_duration_s)
        if scale != 1.0:
            self.logger.info(f"检测到转写时间单位需要缩放: x{scale}")

        cues: List[Dict[str, Any]] = []
        for seg in segments:
            try:
                start = float(seg.get('start', 0.0)) * scale + base_offset_s
                end = float(seg.get('end', start)) * scale + base_offset_s
                text = (seg.get('text') or '').strip()
                if not text:
                    continue
                cues.append({'start': max(0.0, start), 'end': max(end, start + 0.01), 'text': text})
            except Exception:
                continue
        return cues

    def _infer_time_scale(self, max_end_raw: float, total_duration_s: float) -> float:
        """推断第三方API返回的时间单位。返回应乘以的缩放，使值变为秒。

        规则：
        - 如果 max_end_raw 在 [0.5x, 1.5x] * duration 之间，认为单位是秒 -> scale=1
        - 如果 max_end_raw/1000 在该范围 -> 单位毫秒 -> scale=0.001
        - 如果 max_end_raw/100 在该范围 -> 单位厘秒 -> scale=0.01
        - 优先匹配更接近的比例；若 duration 不可用，默认按秒
        """
        try:
            dur = float(total_duration_s or 0)
        except Exception:
            dur = 0.0
        if dur <= 0:
            return 1.0
        def in_range(val: float) -> bool:
            return 0.5 * dur <= val <= 1.5 * dur
        candidates = [
            (1.0, max_end_raw),
            (0.001, max_end_raw * 0.001),
            (0.01, max_end_raw * 0.01),
        ]
        for scale, val in candidates:
            if in_range(val):
                return scale
        # 选择与dur比值最近的
        best = min(candidates, key=lambda x: abs((x[1] or 0) - dur))
        return best[0]

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

    # ---- VAD and chunking helpers ----
    def _create_audio_chunks(self, total_duration_s: float) -> List[Tuple[float, float]]:
        """Create overlapping audio chunks for processing long audio.
        
        Returns list of (start_time, end_time) tuples in seconds.
        Based on strategy: 20-30s window with 0.2s overlap.
        """
        chunks: List[Tuple[float, float]] = []
        window = self.config.chunk_window_s
        overlap = self.config.chunk_overlap_s
        
        if total_duration_s <= window:
            # Audio is short enough, process as single chunk
            return [(0.0, total_duration_s)]
        
        current = 0.0
        while current < total_duration_s:
            end = min(current + window, total_duration_s)
            chunks.append((current, end))
            if end >= total_duration_s:
                break
            current = end - overlap  # Move forward with overlap
        
        self.logger.info(f"音频分片策略: 总时长 {total_duration_s:.1f}s, 窗口 {window}s, 重叠 {overlap}s, 共 {len(chunks)} 个片段")
        return chunks

    def _apply_vad_constraints(self, segments: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        """Apply post-processing constraints to VAD segments.
        
        Strategy from problem statement:
        1. Merge nearby gaps (< 0.20-0.25s)
        2. Enforce minimum segment duration (0.8-1.2s)
        3. Split long segments (> 8-10s) at silence points
        
        Args:
            segments: List of (start, end) tuples in seconds
            
        Returns:
            Constrained list of segments
        """
        if not segments:
            return []
        
        # Sort segments by start time
        segments = sorted(segments, key=lambda x: x[0])
        
        # Step 1: Merge nearby gaps
        merge_gap = self.config.vad_merge_gap_s
        merged: List[List[float]] = []
        for start, end in segments:
            if not merged:
                merged.append([start, end])
            else:
                last = merged[-1]
                gap = start - last[1]
                if gap < merge_gap:
                    # Merge with previous segment
                    last[1] = max(last[1], end)
                    self.logger.debug(f"合并间隙 {gap:.3f}s: [{last[0]:.2f}, {start:.2f}] -> [{last[0]:.2f}, {last[1]:.2f}]")
                else:
                    merged.append([start, end])
        
        # Step 2: Filter out segments shorter than minimum duration
        min_dur = self.config.vad_min_segment_s
        filtered: List[List[float]] = []
        for i, seg in enumerate(merged):
            duration = seg[1] - seg[0]
            if duration < min_dur:
                # Try to merge with adjacent segment
                if i > 0 and filtered:
                    # Merge with previous
                    filtered[-1][1] = seg[1]
                    self.logger.debug(f"短段合并到前段: {duration:.2f}s < {min_dur:.2f}s")
                elif i < len(merged) - 1:
                    # Merge with next (will be handled in next iteration)
                    merged[i + 1][0] = seg[0]
                    self.logger.debug(f"短段合并到后段: {duration:.2f}s < {min_dur:.2f}s")
                else:
                    # Keep it anyway if it's the only segment
                    filtered.append(seg)
            else:
                filtered.append(seg)
        
        # Step 3: Split long segments
        # Note: Secondary splitting at silence points would require re-analyzing audio
        # For now, we'll just mark segments that are too long for potential secondary splitting
        max_dur = self.config.vad_max_segment_s_for_split
        final: List[Tuple[float, float]] = []
        for seg in filtered:
            duration = seg[1] - seg[0]
            if duration > max_dur:
                # Split into smaller chunks at regular intervals
                # In a real implementation, we'd look for silence points within the segment
                self.logger.info(f"长段标记为需要二次切分: {duration:.1f}s > {max_dur:.1f}s")
                # For now, split at regular intervals as fallback
                t = seg[0]
                while t < seg[1]:
                    t_end = min(t + max_dur, seg[1])
                    final.append((t, t_end))
                    t = t_end
            else:
                final.append((seg[0], seg[1]))
        
        self.logger.info(f"VAD约束处理: 原始 {len(segments)} 段 -> 合并后 {len(merged)} 段 -> 过滤后 {len(filtered)} 段 -> 最终 {len(final)} 段")
        return final

    def _normalize_subtitle_text(self, text: str) -> str:
        """Normalize subtitle text for better readability.
        
        Strategy:
        - Normalize punctuation
        - Remove excessive whitespace
        - Optionally filter filler words
        """
        if not text:
            return ""
        
        # Remove excessive whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        
        # Normalize punctuation if enabled
        if self.config.normalize_punctuation:
            # Ensure single space after punctuation
            text = re.sub(r'([.!?,:;])\s*', r'\1 ', text)
            text = re.sub(r'\s+', ' ', text).strip()
        
        # Filter filler words if enabled
        if self.config.filter_filler_words:
            # Common filler words in English and Chinese
            filler_patterns = [
                r'\b(um|uh|er|ah|hmm|like|you know)\b',
                r'[嗯啊呃哦唔]+'
            ]
            for pattern in filler_patterns:
                text = re.sub(pattern, '', text, flags=re.IGNORECASE)
            text = re.sub(r'\s+', ' ', text).strip()
        
        return text

    def _split_long_subtitle(self, text: str, max_chars: int = 42) -> List[str]:
        """Split long subtitle text into multiple lines.
        
        Strategy:
        - Max characters per line (default 42 for CJK, adjust as needed)
        - Split at punctuation or word boundaries
        - Don't break words/numbers
        """
        if not text or len(text) <= max_chars:
            return [text] if text else []
        
        lines: List[str] = []
        remaining = text
        
        while remaining:
            if len(remaining) <= max_chars:
                lines.append(remaining)
                break
            
            # Try to split at punctuation within the limit
            split_pos = -1
            for punct in ['. ', '! ', '? ', ', ', '; ', '。', '！', '？', '，', '；']:
                pos = remaining[:max_chars].rfind(punct)
                if pos > split_pos:
                    split_pos = pos + len(punct)
            
            # If no punctuation found, split at word boundary
            if split_pos <= 0:
                split_pos = remaining[:max_chars].rfind(' ')
            
            # If still no good split point, force split at max_chars
            if split_pos <= 0:
                split_pos = max_chars
            
            lines.append(remaining[:split_pos].strip())
            remaining = remaining[split_pos:].strip()
        
        return lines

    def _split_cue_by_text_length(self, cue: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Split a cue into multiple cues if text is too long.
        
        Strategy:
        - Max 2 lines per subtitle
        - Max 42 characters per line
        - Split at punctuation when possible
        """
        text = cue.get('text', '')
        if not text:
            return [cue]
        
        max_line_len = self.config.max_subtitle_line_length
        max_lines = self.config.max_subtitle_lines
        max_total_chars = max_line_len * max_lines
        
        if len(text) <= max_total_chars:
            return [cue]
        
        # Split text into sentences/phrases
        sentences = re.split(r'([.!?。！？]+\s*)', text)
        sentences = [''.join(sentences[i:i+2]) for i in range(0, len(sentences), 2)]
        
        # Group sentences into cues
        result_cues: List[Dict[str, Any]] = []
        current_text = ""
        start_time = cue['start']
        duration = cue['end'] - cue['start']
        total_chars = len(text)
        chars_processed = 0
        
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            
            # Check if adding this sentence would exceed the limit
            test_text = (current_text + ' ' + sentence).strip() if current_text else sentence
            if len(test_text) > max_total_chars and current_text:
                # Save current cue
                chars_in_cue = len(current_text)
                time_fraction = chars_in_cue / total_chars if total_chars > 0 else 0
                cue_duration = duration * time_fraction
                end_time = start_time + cue_duration
                
                result_cues.append({
                    'start': start_time,
                    'end': end_time,
                    'text': current_text
                })
                
                chars_processed += chars_in_cue
                start_time = end_time
                current_text = sentence
            else:
                current_text = test_text
        
        # Add the last cue
        if current_text:
            result_cues.append({
                'start': start_time,
                'end': cue['end'],
                'text': current_text
            })
        
        return result_cues if result_cues else [cue]

    def _probe_media_duration(self, media_path: str) -> Optional[float]:
        """使用ffprobe获取音/视频时长（秒）。"""
        try:
            from .youtube_handler import find_ffmpeg_location
            ffmpeg_bin = find_ffmpeg_location(logger=self.logger)
            if not ffmpeg_bin:
                return None
            ffprobe_bin = os.path.join(os.path.dirname(ffmpeg_bin), 'ffprobe.exe' if os.name == 'nt' else 'ffprobe')
            cmd = [ffprobe_bin, '-v', 'quiet', '-print_format', 'json', '-show_format', media_path]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout or '{}')
            return float(data.get('format', {}).get('duration', 0.0))
        except Exception:
            return None

    def _run_vad_on_audio(self, wav_path: str, total_duration_s: float) -> Optional[List[Tuple[float, float]]]:
        """调用外部VAD服务（或将来本地实现）；返回片段列表（单位：秒）。"""
        if not self.config.vad_api_url:
            self.logger.warning("VAD已启用但未配置API地址，跳过")
            return None
        sample_rate = 16000
        channels = 1
        total_frames = 0
        try:
            with wave.open(wav_path, 'rb') as wf:
                sample_rate = wf.getframerate() or sample_rate
                channels = wf.getnchannels() or channels
                total_frames = wf.getnframes()
        except Exception as exc:
            self.logger.debug(f"读取音频属性失败，将使用默认采样率: {exc}")

        if sample_rate <= 0 or total_frames <= 0:
            self.logger.warning("VAD前检测到音频帧数异常，跳过VAD")
            return None

        duration_from_wav = total_frames / float(sample_rate)
        if duration_from_wav < 0.2:
            self.logger.warning("音频长度不足0.2秒，跳过VAD")
            return None

        url = self.config.vad_api_url
        headers = {}
        if self.config.vad_api_token:
            headers['Authorization'] = f"Bearer {self.config.vad_api_token}"
        fields = {
            'threshold': str(self.config.vad_threshold),
            'min_speech_ms': str(self.config.vad_min_speech_ms),
            'min_silence_ms': str(self.config.vad_min_silence_ms),
            'max_speech_s': str(self.config.vad_max_speech_s),
            'speech_pad_ms': str(self.config.vad_speech_pad_ms),
            'sample_rate': str(int(sample_rate)),
            'channels': str(int(channels)),
            'format': 'wav',
            'duration_ms': str(int(duration_from_wav * 1000))
        }
        try:
            with open(wav_path, 'rb') as f:
                files = {'audio': (os.path.basename(wav_path), f, 'audio/wav')}
                self.logger.info(
                    f"调用VAD接口: {url} (sample_rate={sample_rate}, duration={duration_from_wav:.2f}s)"
                )
                resp = requests.post(url, headers=headers, data=fields, files=files, timeout=60)
            if resp.status_code != 200:
                self.logger.warning(f"VAD接口响应非200: {resp.status_code} {resp.text[:200]}")
                return None
            data = resp.json()
            segs = data.get('segments') or data.get('result') or []
            if not isinstance(segs, list) or not segs:
                return None
            # 支持多种字段命名
            raw_pairs: List[Tuple[float, float]] = []
            max_end_raw = 0.0
            for s in segs:
                st = s.get('start_ms', s.get('start', 0))
                ed = s.get('end_ms', s.get('end', 0))
                try:
                    stf = float(st)
                    edf = float(ed)
                except Exception:
                    continue
                max_end_raw = max(max_end_raw, edf)
                raw_pairs.append((stf, edf))
            if not raw_pairs:
                return None
            # 时间单位判别（假设ms/centisecond/second）
            scale = self._infer_time_scale(max_end_raw, total_duration_s)
            if scale != 1.0:
                self.logger.info(f"检测到VAD时间单位需要缩放: x{scale}")
            pairs_sec = [(a * scale, b * scale) for (a, b) in raw_pairs]
            # Apply VAD constraints (merge gaps, filter short segments, split long segments)
            constrained_segments = self._apply_vad_constraints(pairs_sec)
            return constrained_segments
        except Exception as e:
            self.logger.warning(f"VAD请求异常: {e}")
            return None

    def _extract_audio_clip(self, wav_path: str, start_s: float, end_s: float) -> Optional[str]:
        """用ffmpeg从wav中裁剪一段到临时wav，返回路径。"""
        try:
            from .youtube_handler import find_ffmpeg_location
            ffmpeg_bin = find_ffmpeg_location(logger=self.logger) or 'ffmpeg'
            out_dir = tempfile.mkdtemp(prefix='y2a_clip_')
            out_wav = os.path.join(out_dir, 'clip.wav')
            dur = max(0.01, end_s - start_s)
            cmd = [
                ffmpeg_bin, '-y', '-ss', f"{start_s:.3f}", '-t', f"{dur:.3f}", '-i', wav_path,
                '-ac', '1', '-ar', '16000', '-f', 'wav', out_wav
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
            if result.returncode != 0 or not os.path.exists(out_wav):
                self.logger.warning(f"裁剪音频失败: {result.stderr[:200]}")
                return None
            return out_wav
        except Exception as e:
            self.logger.warning(f"裁剪音频异常: {e}")
            return None

    def _transcribe_one_clip(self, wav_path: str, base_offset_s: float, total_duration_s: float) -> List[Dict[str, Any]]:
        """Transcribe a single audio clip with retry logic.
        
        Strategy: Use language parameter, prompt, and retry on failure.
        """
        model_name = self.config.model_name or 'whisper-1'
        
        for attempt in range(self.config.max_retries):
            try:
                with open(wav_path, 'rb') as f:
                    # Build request parameters
                    params = {
                        'model': model_name,
                        'file': f,
                        'response_format': 'verbose_json'
                    }
                    
                    # Add language if specified
                    if self.config.language:
                        params['language'] = self.config.language
                    
                    # Add prompt if specified (helps reduce hallucinations)
                    if self.config.prompt:
                        params['prompt'] = self.config.prompt
                    
                    # Add translate flag if enabled
                    if self.config.translate:
                        # Note: OpenAI API uses 'translate' endpoint separately
                        # For transcribe with translation, we'd use a different endpoint
                        pass
                    
                    resp = self.client.audio.transcriptions.create(**params)
                
                cues = self._convert_response_to_cues(resp, base_offset_s, total_duration_s)
                if not cues:
                    self.logger.warning(f"分段转写响应为空 ({base_offset_s:.2f}s 起)")
                else:
                    # Apply text normalization and splitting
                    normalized_cues = []
                    for cue in cues:
                        # Normalize text
                        cue['text'] = self._normalize_subtitle_text(cue['text'])
                        if not cue['text']:
                            continue
                        
                        # Split long cues
                        split_cues = self._split_cue_by_text_length(cue)
                        normalized_cues.extend(split_cues)
                    
                    return normalized_cues
                
                return cues
            except Exception as e:
                self.logger.warning(f"分段转写失败 (尝试 {attempt + 1}/{self.config.max_retries}, {base_offset_s:.2f}s): {e}")
                if attempt < self.config.max_retries - 1:
                    # Exponential backoff
                    import time
                    delay = self.config.retry_delay_s * (2 ** attempt)
                    self.logger.info(f"等待 {delay:.1f}s 后重试...")
                    time.sleep(delay)
                else:
                    self.logger.error(f"分段转写最终失败 ({base_offset_s:.2f}s)")
        
        return []


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
            min_lines_enabled=app_config.get('SPEECH_RECOGNITION_MIN_SUBTITLE_LINES_ENABLED', True),
            min_lines_threshold=int(app_config.get('SPEECH_RECOGNITION_MIN_SUBTITLE_LINES', 5) or 0),
            vad_enabled=bool(app_config.get('VAD_ENABLED', False)),
            vad_provider=(app_config.get('VAD_PROVIDER') or 'silero'),
            vad_api_url=app_config.get('VAD_API_URL') or '',
            vad_api_token=app_config.get('VAD_API_TOKEN') or '',
            vad_threshold=float(app_config.get('VAD_SILERO_THRESHOLD', 0.5) or 0.5),
            vad_min_speech_ms=int(app_config.get('VAD_SILERO_MIN_SPEECH_MS', 250) or 250),
            vad_min_silence_ms=int(app_config.get('VAD_SILERO_MIN_SILENCE_MS', 100) or 100),
            vad_max_speech_s=int(app_config.get('VAD_SILERO_MAX_SPEECH_S', 120) or 120),
            vad_speech_pad_ms=int(app_config.get('VAD_SILERO_SPEECH_PAD_MS', 30) or 30),
            vad_max_segment_s=int(app_config.get('VAD_MAX_SEGMENT_S', 90) or 90),
            # Audio chunking settings
            chunk_window_s=float(app_config.get('AUDIO_CHUNK_WINDOW_S', 25.0) or 25.0),
            chunk_overlap_s=float(app_config.get('AUDIO_CHUNK_OVERLAP_S', 0.2) or 0.2),
            # VAD post-processing constraints
            vad_merge_gap_s=float(app_config.get('VAD_MERGE_GAP_S', 0.25) or 0.25),
            vad_min_segment_s=float(app_config.get('VAD_MIN_SEGMENT_S', 1.0) or 1.0),
            vad_max_segment_s_for_split=float(app_config.get('VAD_MAX_SEGMENT_S_FOR_SPLIT', 8.0) or 8.0),
            vad_silence_threshold_s=float(app_config.get('VAD_SILENCE_THRESHOLD_S', 0.3) or 0.3),
            # Transcription settings
            language=app_config.get('WHISPER_LANGUAGE') or '',
            prompt=app_config.get('WHISPER_PROMPT') or '',
            translate=bool(app_config.get('WHISPER_TRANSLATE', False)),
            max_workers=int(app_config.get('WHISPER_MAX_WORKERS', 3) or 3),
            # Text post-processing settings
            max_subtitle_line_length=int(app_config.get('SUBTITLE_MAX_LINE_LENGTH', 42) or 42),
            max_subtitle_lines=int(app_config.get('SUBTITLE_MAX_LINES', 2) or 2),
            normalize_punctuation=bool(app_config.get('SUBTITLE_NORMALIZE_PUNCTUATION', True)),
            filter_filler_words=bool(app_config.get('SUBTITLE_FILTER_FILLER_WORDS', False)),
            # Retry and fallback settings
            max_retries=int(app_config.get('WHISPER_MAX_RETRIES', 3) or 3),
            retry_delay_s=float(app_config.get('WHISPER_RETRY_DELAY_S', 2.0) or 2.0),
            fallback_to_fixed_chunks=bool(app_config.get('WHISPER_FALLBACK_TO_FIXED_CHUNKS', True)),
        )
        return SpeechRecognizer(config, task_id)
    except Exception:
        return None


