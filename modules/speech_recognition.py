#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import tempfile
import subprocess
import logging
import wave
import time
import shutil
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict, Any
import re
import json
import requests
from .ffmpeg_manager import get_ffmpeg_path, get_ffprobe_path

# Pre-compiled regex patterns for performance optimization
_WHITESPACE_RE = re.compile(r'\s+')
_BLOCK_SPLIT_RE = re.compile(r'\n\s*\n')
_VTT_HEADER_RE = re.compile(r'^WEBVTT.*?\n+', re.IGNORECASE | re.DOTALL)
_PUNCTUATION_SPACE_RE = re.compile(r'([.!?,:;])(?=\S)')
_FILLER_PATTERNS = [
    re.compile(r'\b(um|uh|er|ah|hmm|like|you know)\b', re.IGNORECASE),
    re.compile(r'[嗯啊呃哦唔]+'),
    re.compile(r'\b(doo|da|dee|ch|sh|tickle|scratch|tap|click|pop|mouth|sound|noise|chew|eat|drink|slurp|gulp|swallow|breath|whisper|lip|smack|tongue)\b', re.IGNORECASE),
    re.compile(r'\*[^*]*\*', re.IGNORECASE),  # Non-greedy: content in asterisks
    re.compile(r'\[[^\]]*\]', re.IGNORECASE),  # Non-greedy: content in brackets
    re.compile(r'\([^)]*\)', re.IGNORECASE),  # Non-greedy: content in parentheses
]
_REPEATED_WORD_RE = re.compile(r'\b(\w+)(?:[,\s]+\1\b)+', re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r'([.!?。！？;；,，]+\s*)')
_SENTENCE_PUNCT_RE = re.compile(r'[.!?。！？;；,，]+\s*')


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
    # Deprecated: language detection uses the same Whisper settings now
    detect_api_key: str = ''  # deprecated, ignored
    detect_base_url: str = ''  # deprecated, ignored
    detect_model_name: str = ''  # deprecated, ignored
    # Gating: treat as no subtitles if cues less than threshold
    min_lines_enabled: bool = True
    min_lines_threshold: int = 5

    # VAD settings
    vad_enabled: bool = False
    vad_provider: str = 'silero-vad'
    vad_api_url: str = ''
    vad_api_token: str = ''
    vad_threshold: float = 0.6      # 提高阈值，减少误判
    vad_min_speech_ms: int = 300    # 忽略过短的声音
    vad_min_silence_ms: int = 500   # 增加静音判定时长，避免句子被切断
    vad_max_speech_s: int = 120
    vad_speech_pad_ms: int = 100    # 增加前后缓冲
    vad_max_segment_s: int = 90

    # Audio chunking settings (for long audio processing)
    chunk_window_s: float = 25.0  # Fixed window size for chunking (20-30s recommended)
    chunk_overlap_s: float = 0.2  # Overlap between chunks to ensure continuity

    # VAD post-processing constraints (control subtitle granularity)
    vad_merge_gap_s: float = 1.0  # Merge segments if gap < this value (0.20-0.25s) -> Increased to 1.0s to keep context
    vad_min_segment_s: float = 1.0  # Minimum segment duration (0.8-1.2s)
    vad_max_segment_s_for_split: float = 29.0  # Max segment before secondary splitting (8-10s) -> Increased to 29s for Whisper window

    # Transcription settings
    language: str = ''  # Force language (e.g., 'en', 'zh', 'ja'), empty = auto-detect
    prompt: str = ''  # Optional prompt to guide transcription
    translate: bool = False  # Translate to English
    max_workers: int = 3  # Reserved for future parallel processing (currently sequential)

    # Text post-processing settings
    max_subtitle_line_length: int = 42  # Max characters per line
    max_subtitle_lines: int = 2  # Max lines per subtitle cue
    normalize_punctuation: bool = True  # Normalize punctuation
    filter_filler_words: bool = True  # Remove filler words like "um", "uh"

    # Final subtitle post-processing (timing/text granularity)
    subtitle_time_offset_s: float = 0.0  # Shift all cues by this offset in seconds (can be negative)
    subtitle_min_cue_duration_s: float = 0.6  # Ensure each cue lasts at least this long
    subtitle_merge_gap_s: float = 0.3  # Merge adjacent cues if gap <= this value
    subtitle_min_text_length: int = 2  # Drop/merge cues with text shorter than this

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
        self._temp_dirs: List[str] = []  # Track temp directories for cleanup
        self._language_hint: str = ''  # Set after VAD-based language detection
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
            ffmpeg_bin = get_ffmpeg_path(logger=self.logger)
            if ffmpeg_bin and os.path.exists(ffmpeg_bin):
                self.logger.info(f"语音识别提取音频将使用 ffmpeg: {ffmpeg_bin}")
            else:
                ffmpeg_bin = 'ffmpeg'
                self.logger.info("未找到配置的ffmpeg，尝试使用系统环境中的 ffmpeg")

            tmp_dir = tempfile.mkdtemp(prefix='y2a_audio_')
            self._temp_dirs.append(tmp_dir)  # Track for cleanup
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
            # 强制使用 srt 格式
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

                        # 预先基于首末片段检测语言，仅在前后片段一致时采用
                        self._language_hint = self._detect_language_from_segments(
                            audio_wav, vad_segments, total_audio_duration
                        ) or ''
                        # Filter out invalid language codes like 'unknown'
                        if self._language_hint and self._language_hint.lower() != 'unknown':
                            self.logger.info(f"基于VAD片段检测到语言: {self._language_hint}，将作为Whisper语言参数")
                        else:
                            self._language_hint = ''  # Clear invalid language hint
                            self.logger.info("VAD片段语言检测未达成一致或无效，按自动识别继续")
                        
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
                            
                            # Add prompt to reduce hallucinations (优化版：更精简)
                            base_prompt = "Transcribe speech only. Ignore noise."
                            if self.config.prompt:
                                params['prompt'] = f"{base_prompt} {self.config.prompt}"
                            else:
                                params['prompt'] = base_prompt
                            
                            resp = self.client.audio.transcriptions.create(**params)
                        except Exception as e:
                            self.logger.error(f"语音转写请求失败: {e}")
                            return None

                    cues = self._convert_response_to_cues(resp, 0.0, total_audio_duration)
                    if not cues:
                        self.logger.error("转写响应未生成任何字幕片段")
                        return None
                    
                    # Apply text normalization and splitting for whole transcription
                    cues = self._apply_text_processing_to_cues(cues)

            # Step 5: Render subtitles
            # Validate and clamp timestamps to video duration
            invalid_cues = []
            for cue in cues:
                if not isinstance(cue, dict) or 'start' not in cue or 'end' not in cue:
                    continue
                try:
                    start = float(cue['start'])
                    end = float(cue['end'])
                    
                    # Detect timestamps that exceed video duration
                    if start > total_audio_duration or end > total_audio_duration:
                        invalid_cues.append(cue)
                        self.logger.warning(f"检测到超出视频时长的字幕: [{start:.2f}s - {end:.2f}s] > {total_audio_duration:.2f}s")
                        # Clamp to video duration
                        cue['start'] = min(start, total_audio_duration)
                        cue['end'] = min(end, total_audio_duration)
                except (ValueError, TypeError) as e:
                    self.logger.warning(f"字幕时间戳格式错误: {e}")
            
            if invalid_cues:
                self.logger.warning(f"共发现 {len(invalid_cues)} 条超出视频时长的字幕，已自动修正")
            
            # Ensure cues are ordered by time to avoid jumbled SRT indices
            try:
                cues = sorted(
                    [c for c in cues if isinstance(c, dict) and 'start' in c and 'end' in c],
                    key=lambda x: (float(x.get('start', 0.0)), float(x.get('end', 0.0)))
                )
            except Exception:
                # 如果排序失败，继续使用原顺序
                pass
            
            # Log timestamp range for verification
            if cues:
                first_start = float(cues[0].get('start', 0))
                last_end = float(cues[-1].get('end', 0))
                self.logger.info(f"字幕时间范围: {first_start:.2f}s - {last_end:.2f}s (视频时长: {total_audio_duration:.2f}s)")
            
            # Apply final short-cue and timing post-processing (merge tiny fragments, apply offset)
            cues = self._finalize_cues(cues, total_audio_duration)

            self.logger.info(f"步骤 5/6: 渲染字幕 (格式: {response_format}, 共 {len(cues)} 个片段)")
            text = self._render_cues(cues, response_format)
            if not text:
                self.logger.error("无法从转写结果渲染字幕")
                return None

            with open(output_path, 'w', encoding='utf-8') as out:
                out.write(text)

            # Step 6: Quality check
            self.logger.info("步骤 6/6: 质量检查")
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
        finally:
            # Clean up temporary files
            self._cleanup_temp_files()

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

        blocks = _BLOCK_SPLIT_RE.split(text)
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

    def _convert_response_to_cues(self, resp: Any, base_offset_s: float, clip_duration_s: float) -> List[Dict[str, Any]]:
        """Normalize Whisper/OpenAI/whisper.cpp style responses into cue list.
        
        Args:
            resp: API response object
            base_offset_s: Time offset of this clip in the original video
            clip_duration_s: Duration of THIS audio clip (for scale inference)
        """
        payload_dict = self._as_dict(resp)
        if isinstance(payload_dict, dict):
            cues = self._normalize_verbose_segments_to_cues(payload_dict, base_offset_s, clip_duration_s)
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

    def _normalize_verbose_segments_to_cues(self, data: dict, base_offset_s: float, clip_duration_s: float) -> List[Dict[str, Any]]:
        """Adapt OpenAI/whisper.cpp verbose_json payloads to cue list.

        官方 OpenAI Whisper 与 whisper.cpp 均以秒为单位返回 start/end，但仍保留
        针对兼容实现可能使用毫秒/厘秒的容错缩放，以避免再次出现长度异常的字幕文件。
        
        Args:
            data: Whisper API response as dict
            base_offset_s: Time offset in the original video (for timestamp adjustment)
            clip_duration_s: Duration of THIS audio clip (not the whole video)
        """
        segments = data.get('segments') or []
        text_fallback = (data.get('text') or '').strip()
        if not isinstance(segments, list) or not segments:
            if not text_fallback:
                return []
            return [{
                'start': base_offset_s,
                'end': base_offset_s + min(clip_duration_s or 5.0, 5.0),
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
        
        # CRITICAL: Use clip_duration_s for scale inference (not total video duration)
        scale = self._infer_time_scale(max_end_raw, clip_duration_s)
        if scale != 1.0:
            self.logger.info(f"检测到转写时间单位需要缩放: x{scale} (片段时长: {clip_duration_s:.1f}s, 最大原始时间戳: {max_end_raw:.1f})")

        cues: List[Dict[str, Any]] = []
        for seg in segments:
            try:
                # Convert segment timestamps and add base offset
                start_raw = float(seg.get('start', 0.0))
                end_raw = float(seg.get('end', start_raw))
                
                start = start_raw * scale + base_offset_s
                end = end_raw * scale + base_offset_s
                
                text = (seg.get('text') or '').strip()
                if not text:
                    continue
                
                cues.append({'start': max(0.0, start), 'end': max(end, start + 0.01), 'text': text})
            except Exception as e:
                self.logger.warning(f"处理片段时出错: {e}")
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
                blocks = _BLOCK_SPLIT_RE.split(content.strip())
                cnt = 0
                for b in blocks:
                    if '-->' in b:
                        cnt += 1
                return cnt
            else:
                # VTT: ensure header removed then count cue lines with -->
                # Remove header WEBVTT lines
                body = _VTT_HEADER_RE.sub('', content)
                # Split by blank lines; count blocks containing a --> line
                blocks = _BLOCK_SPLIT_RE.split(body.strip())
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
        i = 0
        while i < len(merged):
            seg = merged[i]
            duration = seg[1] - seg[0]
            if duration < min_dur:
                # Try to merge with adjacent segment
                if filtered:
                    # Merge with previous
                    filtered[-1][1] = seg[1]
                    self.logger.debug(f"短段合并到前段: {duration:.2f}s < {min_dur:.2f}s")
                elif i < len(merged) - 1:
                    # Merge with next by extending current segment
                    next_seg = merged[i + 1]
                    merged[i + 1] = [seg[0], next_seg[1]]
                    self.logger.debug(f"短段合并到后段: {duration:.2f}s < {min_dur:.2f}s")
                    # Skip current segment, it's merged into next
                    i += 1
                    continue
                else:
                    # Keep it anyway if it's the only segment
                    filtered.append(seg)
            else:
                filtered.append(seg)
            i += 1
        
        # Step 3: Split long segments (Relaxed)
        # Whisper handles segments up to 30s well, and even longer with context.
        # We rely on VAD's vad_max_segment_s to keep segments reasonable.
        # Only split if extremely long to avoid memory issues or timeouts.
        max_dur = max(self.config.vad_max_segment_s_for_split, 60.0) # Ensure at least 60s
        final: List[Tuple[float, float]] = []
        for seg in filtered:
            duration = seg[1] - seg[0]
            if duration > max_dur:
                self.logger.info(f"长段强制切分: {duration:.1f}s > {max_dur:.1f}s")
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
        - Filter filler words and ASMR sounds
        - Remove repeated words/phrases
        """
        if not text:
            return ""
        
        original_text = text
        # Remove excessive whitespace (using pre-compiled pattern)
        text = _WHITESPACE_RE.sub(' ', text).strip()
        
        # Normalize punctuation if enabled
        if self.config.normalize_punctuation:
            # Ensure single space after punctuation (only when followed by non-whitespace)
            text = _PUNCTUATION_SPACE_RE.sub(r'\1 ', text)
            text = _WHITESPACE_RE.sub(' ', text).strip()
        
        # Filter filler words if enabled
        if self.config.filter_filler_words:
            # Common filler words in English and Chinese (using pre-compiled patterns)
            for pattern in _FILLER_PATTERNS:
                text = pattern.sub('', text)
            
            # Remove repeated words (e.g., "scan, scan, scan" -> "scan")
            text = _REPEATED_WORD_RE.sub(r'\1', text)
            
            text = _WHITESPACE_RE.sub(' ', text).strip()
        
        if text != original_text and len(original_text) < 100:
             self.logger.debug(f"文本清洗: '{original_text}' -> '{text}'")
        
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
        
        # Split text into sentences/phrases (using pre-compiled pattern)
        # 增强切分逻辑：支持更多标点，保留分隔符，加入逗号
        # Split by . ! ? ; , and their CJK equivalents
        sentences = _SENTENCE_SPLIT_RE.split(text)
        # Remove empty strings from split result
        sentences = [s for s in sentences if s.strip()]
        
        if len(text) > max_total_chars:
             self.logger.debug(f"尝试切分长字幕 ({len(text)} chars): {text[:50]}...")
             self.logger.debug(f"切分出的句子数: {len(sentences)}")

        # Reconstruct sentences with their punctuation
        joined_sentences = []
        i = 0
        while i < len(sentences):
            if i + 1 < len(sentences) and _SENTENCE_PUNCT_RE.match(sentences[i+1]):
                joined_sentences.append(sentences[i] + sentences[i+1])
                i += 2
            else:
                joined_sentences.append(sentences[i])
                i += 1
        sentences = joined_sentences
        
        # Group sentences into cues
        result_cues: List[Dict[str, Any]] = []
        current_text = ""
        start_time = cue['start']
        duration = cue['end'] - cue['start']
        total_chars = len(text)
        
        # 如果总时长很长，但字符数很少（语速极慢），也应该切分
        chars_per_sec = total_chars / duration if duration > 0 else 10
        
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            
            # 如果单句本身就超过最大长度，强制硬切分
            if len(sentence) > max_total_chars:
                self.logger.debug(f"单句过长 ({len(sentence)} > {max_total_chars})，强制硬切分: {sentence[:30]}...")
                # 先处理掉 current_text
                if current_text:
                    chars_in_cue = len(current_text)
                    time_fraction = chars_in_cue / total_chars if total_chars > 0 else 0
                    cue_duration = duration * time_fraction
                    cue_duration = max(cue_duration, 1.0)
                    cue_duration = min(cue_duration, cue['end'] - start_time)
                    
                    result_cues.append({
                        'start': start_time,
                        'end': start_time + cue_duration,
                        'text': current_text
                    })
                    start_time += cue_duration
                    total_chars -= chars_in_cue
                    duration -= cue_duration
                    current_text = ""
                
                # 硬切分长句
                sub_lines = self._split_long_subtitle(sentence, max_line_len)
                # 将硬切分后的行两两组合（因为 max_lines=2）
                chunked_lines = []
                for k in range(0, len(sub_lines), max_lines):
                    chunk = " ".join(sub_lines[k:k+max_lines])
                    chunked_lines.append(chunk)
                
                for chunk in chunked_lines:
                    chars_in_cue = len(chunk)
                    time_fraction = chars_in_cue / total_chars if total_chars > 0 else 0
                    cue_duration = duration * time_fraction
                    # 这里的 duration 可能会因为 total_chars 估算不准而偏小，需要保底
                    if cue_duration < 0.5 and duration > 1.0: 
                         cue_duration = min(2.0, duration)

                    cue_duration = min(cue_duration, cue['end'] - start_time)
                    
                    result_cues.append({
                        'start': start_time,
                        'end': start_time + cue_duration,
                        'text': chunk
                    })
                    start_time += cue_duration
                    total_chars -= chars_in_cue
                    duration -= cue_duration
                continue

            # Check if adding this sentence would exceed the limit
            test_text = (current_text + ' ' + sentence).strip() if current_text else sentence
            
            should_split = False
            if len(test_text) > max_total_chars:
                should_split = True
            elif current_text and len(current_text) > max_line_len: # 超过一行长度就倾向于切分
                should_split = True
            
            if should_split and current_text:
                # Save current cue
                chars_in_cue = len(current_text)
                # 估算时间：按字符比例分配时间
                time_fraction = chars_in_cue / total_chars if total_chars > 0 else 0
                cue_duration = duration * time_fraction
                
                # 保证最小持续时间
                cue_duration = max(cue_duration, 1.0)
                # 保证不超出剩余总时间
                remaining_duration = cue['end'] - start_time
                cue_duration = min(cue_duration, remaining_duration)
                
                end_time = start_time + cue_duration
                
                result_cues.append({
                    'start': start_time,
                    'end': end_time,
                    'text': current_text
                })
                
                start_time = end_time
                current_text = sentence
                # 更新剩余字符数，用于后续估算
                total_chars -= chars_in_cue
                duration -= cue_duration
            else:
                current_text = test_text
        
        # Add the last cue
        if current_text:
            result_cues.append({
                'start': start_time,
                'end': cue['end'],
                'text': current_text
            })
        
        if len(result_cues) > 1:
             self.logger.debug(f"切分结果: 1 -> {len(result_cues)} 条")

        return result_cues if result_cues else [cue]

    def _apply_text_processing_to_cues(self, cues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Apply text normalization and splitting to a list of cues.
        
        This helper method reduces code duplication between whole-audio transcription
        and segmented transcription paths.
        
        Args:
            cues: List of cue dictionaries with 'start', 'end', 'text' keys

        Returns:
            Processed list of cues with normalized and split text
        """
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

    def _finalize_cues(self, cues: List[Dict[str, Any]], total_duration_s: float) -> List[Dict[str, Any]]:
        """Final post-processing to reduce extremely short standalone subtitles.

        Steps:
        1. Sort cues by start time.
        2. Apply global time offset (can be negative) and clamp into [0, total_duration].
        3. Merge adjacent cues if gap <= subtitle_merge_gap_s OR either text length < subtitle_min_text_length.
        4. Enforce minimum cue duration; extend or merge with neighbor.
        5. Drop remaining cues whose text length < subtitle_min_text_length and duration < subtitle_min_cue_duration_s.
        """
        if not cues:
            return []

        try:
            offset = float(self.config.subtitle_time_offset_s)
        except Exception:
            offset = 0.0
        merge_gap = max(0.0, float(getattr(self.config, 'subtitle_merge_gap_s', 0.3)))
        min_text_len = max(0, int(getattr(self.config, 'subtitle_min_text_length', 2)))
        min_dur = max(0.05, float(getattr(self.config, 'subtitle_min_cue_duration_s', 0.6)))

        # 1. sort
        cues = sorted(cues, key=lambda c: float(c.get('start', 0.0)))

        # 2. apply offset & clamp
        for c in cues:
            try:
                c['start'] = max(0.0, min(total_duration_s, float(c['start']) + offset))
                c['end'] = max(0.0, min(total_duration_s, float(c['end']) + offset))
                if c['end'] <= c['start']:
                    c['end'] = min(total_duration_s, c['start'] + 0.05)
            except Exception:
                continue

        # 3. merge adjacent cues
        merged: List[Dict[str, Any]] = []
        for c in cues:
            if not merged:
                merged.append(c)
                continue
            prev = merged[-1]
            gap = float(c['start']) - float(prev['end'])
            prev_text = (prev.get('text') or '').strip()
            cur_text = (c.get('text') or '').strip()
            
            prev_dur = float(prev['end']) - float(prev['start'])
            cur_dur = float(c['end']) - float(c['start'])
            combined_dur = float(c['end']) - float(prev['start'])

            need_merge = False
            
            # Condition 1: Gap is small
            if gap <= merge_gap:
                # Only merge if the resulting subtitle isn't too long (e.g. < 7s)
                # OR if one of the segments is very short (fragment) that needs to be attached
                if combined_dur < 7.0:
                    need_merge = True
                elif prev_dur < 1.0 or cur_dur < 1.0:
                    # Always try to rescue tiny fragments
                    need_merge = True
                else:
                    # Both are substantial and combined is long -> keep separate
                    need_merge = False
            
            # Condition 2: Text is too short (fragment repair), even if gap is slightly larger
            elif len(prev_text) < min_text_len or len(cur_text) < min_text_len:
                if gap <= merge_gap * 2:
                    need_merge = True
            
            if need_merge:
                # Merge texts with space (avoid duplicate punctuation spacing)
                new_text = (prev_text + ' ' + cur_text).strip()
                prev['text'] = _WHITESPACE_RE.sub(' ', new_text)
                prev['end'] = max(prev['end'], c['end'])
            else:
                merged.append(c)

        # 4. enforce minimum duration by extending forward (without overlapping next) or merging
        finalized: List[Dict[str, Any]] = []
        for i, c in enumerate(merged):
            start = float(c['start'])
            end = float(c['end'])
            dur = end - start
            text_len = len((c.get('text') or '').strip())
            if dur < min_dur:
                # Try to extend to min_dur without passing next cue start
                next_start = float(merged[i + 1]['start']) if i + 1 < len(merged) else total_duration_s
                target_end = min(start + min_dur, next_start - 0.01 if next_start - start > 0.05 else next_start)
                if target_end > end:
                    c['end'] = target_end
                else:
                    # Merge with next if still too short and text also short
                    if i + 1 < len(merged) and (text_len < min_text_len or len((merged[i+1].get('text') or '').strip()) < min_text_len):
                        merged[i+1]['start'] = start
                        merged[i+1]['text'] = (c['text'].strip() + ' ' + merged[i+1]['text'].strip()).strip()
                        continue  # Skip adding current; it's merged forward
            finalized.append(c)

        # 5. drop remaining ultra-short single-character cues
        cleaned: List[Dict[str, Any]] = []
        for c in finalized:
            text = (c.get('text') or '').strip()
            dur = float(c['end']) - float(c['start'])
            if len(text) < min_text_len and dur < min_dur:
                # Skip dropping pronoun "I" if appears inside a longer word—here it's standalone so drop
                self.logger.debug(f"移除极短字幕: '{text}' ({dur:.2f}s)")
                continue
            cleaned.append(c)

        return cleaned

    def _probe_media_duration(self, media_path: str) -> Optional[float]:
        """使用ffprobe获取音/视频时长（秒）。"""
        try:
            ffmpeg_bin = get_ffmpeg_path(logger=self.logger)
            if not ffmpeg_bin:
                return None
            ffprobe_bin = get_ffprobe_path(ffmpeg_path=ffmpeg_bin, logger=self.logger)
            if not ffprobe_bin:
                return None
            cmd = [ffprobe_bin, '-v', 'quiet', '-print_format', 'json', '-show_format', media_path]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout or '{}')
            return float(data.get('format', {}).get('duration', 0.0))
        except Exception:
            return None

    def _run_vad_on_audio(self, wav_path: str, total_duration_s: float) -> Optional[List[Tuple[float, float]]]:
        """调用外部VAD服务（LocalAI silero-vad）；返回片段列表（单位：秒）。
        
        LocalAI VAD 端点需要 JSON 格式的 float32 数组，而不是文件上传。
        """
        if not self.config.vad_api_url:
            self.logger.warning("VAD已启用但未配置API地址，跳过")
            return None
        
        try:
            # 读取 WAV 文件并转换为 float32 数组
            import numpy as np
            with wave.open(wav_path, 'rb') as wf:
                sample_rate = wf.getframerate()
                channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                total_frames = wf.getnframes()
                
                if sample_rate != 16000 or channels != 1:
                    self.logger.warning(f"VAD要求16kHz单声道，当前: {sample_rate}Hz {channels}声道，跳过VAD")
                    return None
                
                # 读取音频数据
                audio_bytes = wf.readframes(total_frames)
            
            duration_from_wav = total_frames / float(sample_rate)
            
            # VAD服务需要至少一定长度的音频
            min_duration = 0.5  # 至少0.5秒
            if duration_from_wav < min_duration:
                self.logger.warning(f"音频长度不足 ({duration_from_wav:.3f}s < {min_duration}s)，跳过VAD")
                return None
            
            # 将 PCM 数据转换为 float32 数组（归一化到 [-1, 1]）
            if sample_width == 2:  # 16-bit PCM
                audio_array = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
                audio_array /= 32768.0  # 归一化到 [-1, 1]
            elif sample_width == 4:  # 32-bit PCM
                audio_array = np.frombuffer(audio_bytes, dtype=np.int32).astype(np.float32)
                audio_array /= 2147483648.0
            else:
                self.logger.warning(f"不支持的样本宽度: {sample_width} bytes")
                return None
            
            self.logger.info(
                f"准备调用VAD: {duration_from_wav:.2f}s, {total_frames}帧, 样本范围[{audio_array.min():.3f}, {audio_array.max():.3f}]"
            )
            
            # 构造 JSON 请求
            url = self.config.vad_api_url
            headers = {
                'Content-Type': 'application/json'
            }
            if self.config.vad_api_token:
                headers['Authorization'] = f"Bearer {self.config.vad_api_token}"
            
            # LocalAI VAD 需要 model 和 audio 字段
            vad_model = self.config.vad_provider or 'silero-vad'
            payload = {
                'model': vad_model,
                'audio': audio_array.tolist(),
                'threshold': self.config.vad_threshold,
                'min_speech_duration_ms': self.config.vad_min_speech_ms,
                'min_silence_duration_ms': self.config.vad_min_silence_ms,
                'speech_pad_ms': self.config.vad_speech_pad_ms
            }
            
            self.logger.info(f"调用VAD接口: {url} (model={vad_model}, samples={len(audio_array)})")
            
            resp = requests.post(url, headers=headers, json=payload, timeout=120)
            
            if resp.status_code != 200:
                self.logger.warning(f"VAD接口响应非200: {resp.status_code} {resp.text[:200]}")
                return None
            
            data = resp.json()
            segs = data.get('segments', [])
            
            if not isinstance(segs, list) or not segs:
                self.logger.debug("VAD返回空片段列表")
                return None
            
            # LocalAI 返回的时间已经是秒为单位
            raw_pairs: List[Tuple[float, float]] = []
            for s in segs:
                try:
                    start_s = float(s.get('start', 0))
                    end_s = float(s.get('end', 0))
                    if end_s > start_s:
                        raw_pairs.append((start_s, end_s))
                except Exception:
                    continue
            
            if not raw_pairs:
                self.logger.debug("VAD未返回有效的语音片段")
                return None
            
            self.logger.info(f"VAD检测到 {len(raw_pairs)} 个原始片段")
            
            # LocalAI 返回的时间单位应该已经是秒，但为了兼容性还是检查一下
            max_end_raw = max(end for _, end in raw_pairs)
            scale = self._infer_time_scale(max_end_raw, total_duration_s)
            if scale != 1.0:
                self.logger.info(f"检测到VAD时间单位需要缩放: x{scale}")
                pairs_sec = [(a * scale, b * scale) for (a, b) in raw_pairs]
            else:
                pairs_sec = raw_pairs
            
            # Apply VAD constraints (merge gaps, filter short segments, split long segments)
            constrained_segments = self._apply_vad_constraints(pairs_sec)
            return constrained_segments
        except ImportError:
            self.logger.error("VAD功能需要numpy库，请安装: pip install numpy")
            return None
        except Exception as e:
            self.logger.warning(f"VAD请求异常: {e}")
            import traceback
            self.logger.debug(traceback.format_exc())
            return None

    def _extract_audio_clip(self, wav_path: str, start_s: float, end_s: float) -> Optional[str]:
        """用ffmpeg从wav中裁剪一段到临时wav，返回路径。
        
        Note: Caller is responsible for cleanup via _cleanup_temp_files() or use TemporaryDirectory context manager.
        """
        try:
            ffmpeg_bin = get_ffmpeg_path(logger=self.logger) or 'ffmpeg'
            out_dir = tempfile.mkdtemp(prefix='y2a_clip_')
            self._temp_dirs.append(out_dir)  # Track for cleanup
            out_wav = os.path.join(out_dir, 'clip.wav')
            dur = max(0.01, end_s - start_s)
            # 将 -ss 和 -t 放在 -i 之前，这样可以更快速准确地定位和裁剪音频
            cmd = [
                ffmpeg_bin, '-y', '-ss', f"{start_s:.3f}", '-t', f"{dur:.3f}", '-i', wav_path,
                '-ac', '1', '-ar', '16000', '-acodec', 'pcm_s16le', '-f', 'wav', out_wav
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
            if result.returncode != 0 or not os.path.exists(out_wav):
                self.logger.warning(f"裁剪音频失败 (returncode={result.returncode}): {result.stderr[:200]}")
                return None
            
            # 验证裁剪后的音频文件是否有足够的样本
            try:
                with wave.open(out_wav, 'rb') as wf:
                    n_frames = wf.getnframes()
                    sample_rate = wf.getframerate()
                    actual_duration = n_frames / sample_rate if sample_rate > 0 else 0.0
                    
                    # 检查是否有足够的样本（至少0.1秒）
                    if actual_duration < 0.1:
                        self.logger.warning(f"裁剪后的音频时长不足 ({actual_duration:.3f}s < 0.1s)，跳过此片段")
                        return None
                    
                    self.logger.debug(f"成功裁剪音频: {start_s:.2f}s-{end_s:.2f}s, 实际时长: {actual_duration:.2f}s, 样本数: {n_frames}")
            except Exception as e:
                self.logger.warning(f"验证裁剪音频失败: {e}")
                return None
            
            return out_wav
        except Exception as e:
            self.logger.warning(f"裁剪音频异常: {e}")
            return None

    def _cleanup_temp_files(self):
        """Clean up all temporary directories created during processing."""
        for temp_dir in self._temp_dirs:
            try:
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
                    self.logger.debug(f"清理临时目录: {temp_dir}")
            except Exception as e:
                self.logger.warning(f"清理临时目录失败 {temp_dir}: {e}")
        self._temp_dirs.clear()

    def _detect_language_from_segments(self, audio_wav: str, segments: List[Tuple[float, float]], total_duration_s: float) -> str:
        """Use first & last VAD segments to probe language via Whisper; require agreement."""
        try:
            if not segments:
                return ''

            # Choose first and last segments after sorting
            segs_sorted = sorted(segments, key=lambda x: x[0])
            picks = [segs_sorted[0]]
            if len(segs_sorted) > 1:
                picks.append(segs_sorted[-1])

            detected = []
            for idx, (s, e) in enumerate(picks):
                clip = self._extract_audio_clip(audio_wav, s, e)
                if not clip:
                    continue
                lang = self._whisper_language_detection(clip)
                if lang:
                    detected.append(lang)
                    self.logger.info(f"语言探测({ '首段' if idx == 0 else '末段' }): {lang} [{s:.2f}s - {e:.2f}s]")

            if len(detected) >= 2 and detected[0] == detected[1]:
                return detected[0]
            return ''
        except Exception as e:
            self.logger.warning(f"VAD片段语言检测失败: {e}")
            return ''

    def _whisper_language_detection(self, wav_path: str) -> str:
        """Call Whisper for language-only detection on a small clip."""
        try:
            model_name = self.config.model_name or 'whisper-1'
            with open(wav_path, 'rb') as f:
                params = {
                    'model': model_name,
                    'file': f,
                    'response_format': 'verbose_json',
                    'temperature': 0
                }
                resp = self.client.audio.transcriptions.create(**params)

            data = self._as_dict(resp) or {}
            lang = data.get('language') or ''
            if lang:
                return str(lang).strip()
            # Fallback: try segments[0].language if present
            segments = data.get('segments') or []
            if segments and isinstance(segments, list):
                seg_lang = segments[0].get('language') or segments[0].get('lang') or ''
                return str(seg_lang).strip()
            return ''
        except Exception as e:
            self.logger.warning(f"Whisper语言探测失败: {e}")
            return ''

    def _get_wav_duration(self, wav_path: str) -> float:
        """Get duration of a WAV file efficiently."""
        try:
            with wave.open(wav_path, 'rb') as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                if rate > 0:
                    return frames / float(rate)
        except Exception:
            pass
        try:
            size = os.path.getsize(wav_path)
            return max(0.0, (size - 44) / 32000.0)
        except Exception:
            return 0.0

    def _transcribe_one_clip(self, wav_path: str, base_offset_s: float, total_duration_s: float) -> List[Dict[str, Any]]:
        """Transcribe a single audio clip with retry logic.
        
        Strategy: Use language parameter, prompt, and retry on failure.
        
        Args:
            wav_path: Path to audio file to transcribe
            base_offset_s: Time offset of this clip in the original video (for timestamp adjustment)
            total_duration_s: Total duration of the ENTIRE video (not the clip)
        """
        model_name = self.config.model_name or 'whisper-1'
        
        # Get the duration of THIS clip (not the whole video)
        clip_duration_s = self._get_wav_duration(wav_path)
        if clip_duration_s <= 0.1:
            # Fallback if wav read fails
            clip_duration_s = self._probe_media_duration(wav_path) or 30.0
        
        for attempt in range(self.config.max_retries):
            try:
                with open(wav_path, 'rb') as f:
                    # Build request parameters
                    params = {
                        'model': model_name,
                        'file': f,
                        'response_format': 'verbose_json'
                    }
                    
                    # Add language if specified (skip invalid values like 'unknown')
                    language_hint = self._language_hint or self.config.language
                    if language_hint and language_hint.lower() != 'unknown':
                        params['language'] = language_hint
                    
                    # Add prompt to reduce hallucinations (优化版：更精简的提示)
                    base_prompt = "Transcribe speech only. Ignore noise."
                    if self.config.prompt:
                        params['prompt'] = f"{base_prompt} {self.config.prompt}"
                    else:
                        params['prompt'] = base_prompt
                    
                    # Use translation endpoint if translate is enabled
                    if self.config.translate:
                        resp = self.client.audio.translations.create(**params)
                    else:
                        resp = self.client.audio.transcriptions.create(**params)
                
                # CRITICAL: Pass clip_duration_s for time scale inference, not total_duration_s
                cues = self._convert_response_to_cues(resp, base_offset_s, clip_duration_s)
                if not cues:
                    self.logger.warning(f"分段转写响应为空 ({base_offset_s:.2f}s 起)")
                    return []
                
                # Clamp timestamps to video bounds (prevent overflow beyond total duration)
                for cue in cues:
                    cue['start'] = min(cue['start'], total_duration_s)
                    cue['end'] = min(cue['end'], total_duration_s)
                
                # Apply text normalization and splitting
                return self._apply_text_processing_to_cues(cues)
            except Exception as e:
                self.logger.warning(f"分段转写失败 (尝试 {attempt + 1}/{self.config.max_retries}, {base_offset_s:.2f}s): {e}")
                if attempt < self.config.max_retries - 1:
                    # Exponential backoff
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
            # output_format removed, hardcoded to srt internally
            min_lines_enabled=app_config.get('SPEECH_RECOGNITION_MIN_SUBTITLE_LINES_ENABLED', True),
            min_lines_threshold=int(app_config.get('SPEECH_RECOGNITION_MIN_SUBTITLE_LINES', 5) or 0),
            vad_enabled=bool(app_config.get('VAD_ENABLED', False)),
            vad_provider=(app_config.get('VAD_PROVIDER') or 'silero-vad'),
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
            # Transcription settings
            language=app_config.get('WHISPER_LANGUAGE') or '',
            prompt=app_config.get('WHISPER_PROMPT') or '',
            translate=bool(app_config.get('WHISPER_TRANSLATE', False)),
            max_workers=int(app_config.get('WHISPER_MAX_WORKERS', 3) or 3),
            # Text post-processing settings
            max_subtitle_line_length=int(app_config.get('SUBTITLE_MAX_LINE_LENGTH', 42) or 42),
            max_subtitle_lines=int(app_config.get('SUBTITLE_MAX_LINES', 2) or 2),
            normalize_punctuation=bool(app_config.get('SUBTITLE_NORMALIZE_PUNCTUATION', True)),
            filter_filler_words=bool(app_config.get('SUBTITLE_FILTER_FILLER_WORDS', True)),
            # Final subtitle post-processing (optional params)
            subtitle_time_offset_s=float(app_config.get('SUBTITLE_TIME_OFFSET_S', 0.0) or 0.0),
            subtitle_min_cue_duration_s=float(app_config.get('SUBTITLE_MIN_CUE_DURATION_S', 0.6) or 0.6),
            subtitle_merge_gap_s=float(app_config.get('SUBTITLE_MERGE_GAP_S', 0.3) or 0.3),
            subtitle_min_text_length=int(app_config.get('SUBTITLE_MIN_TEXT_LENGTH', 2) or 2),
            # Retry and fallback settings
            max_retries=int(app_config.get('WHISPER_MAX_RETRIES', 3) or 3),
            retry_delay_s=float(app_config.get('WHISPER_RETRY_DELAY_S', 2.0) or 2.0),
            fallback_to_fixed_chunks=bool(app_config.get('WHISPER_FALLBACK_TO_FIXED_CHUNKS', True)),
        )
        return SpeechRecognizer(config, task_id)
    except Exception:
        return None


