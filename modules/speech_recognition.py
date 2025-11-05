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
        Transcribe a video file into SRT/VTT subtitles using OpenAI-compatible Whisper API.

        Returns the output_path on success, otherwise None.
        """
        try:
            if not self.client:
                self.logger.error("语音识别客户端未初始化")
                return None
            if not os.path.exists(video_path):
                self.logger.error(f"视频文件不存在: {video_path}")
                return None
            # 提取整段音频（16kHz单声道wav）
            audio_wav = self._extract_audio_wav(video_path)
            if not audio_wav:
                return None

            total_audio_duration = self._probe_media_duration(audio_wav)
            if total_audio_duration is None:
                # 兜底：使用视频时长
                total_audio_duration = self._probe_media_duration(video_path) or 0.0

            model_name = self.config.model_name or 'whisper-1'
            response_format = (self.config.output_format or 'srt').lower()
            if response_format not in ('srt', 'vtt'):
                response_format = 'srt'

            # 如果启用了VAD，则先按语音段切分进行分段识别并拼接
            cues: List[Dict[str, Any]] = []
            used_vad = False
            if self.config.vad_enabled:
                try:
                    vad_segments = self._run_vad_on_audio(audio_wav, total_audio_duration)
                    if vad_segments:
                        used_vad = True
                        self.logger.info(f"VAD启用：共检测到 {len(vad_segments)} 个语音片段，开始分段识别")
                        for (seg_start_s, seg_end_s) in vad_segments:
                            seg_start_s = max(0.0, float(seg_start_s))
                            seg_end_s = max(seg_start_s, float(seg_end_s))
                            # 限制分段最大长度
                            max_len = max(5, int(self.config.vad_max_segment_s or 90))
                            if seg_end_s - seg_start_s > max_len:
                                # 二次切分
                                t = seg_start_s
                                while t < seg_end_s:
                                    t2 = min(seg_end_s, t + max_len)
                                    part_wav = self._extract_audio_clip(audio_wav, t, t2)
                                    if part_wav:
                                        cues.extend(self._transcribe_one_clip(part_wav, t, total_audio_duration))
                                    t = t2
                            else:
                                part_wav = self._extract_audio_clip(audio_wav, seg_start_s, seg_end_s)
                                if part_wav:
                                    cues.extend(self._transcribe_one_clip(part_wav, seg_start_s, total_audio_duration))
                    else:
                        self.logger.warning("VAD已启用但未返回有效语音段，回退为整段识别")
                except Exception as e:
                    self.logger.warning(f"VAD处理失败，回退整段识别: {e}")

            if not used_vad:
                # 整段识别
                self.logger.info(f"开始语音转写（一次请求），模型: {model_name}，输出格式: {response_format}")
                with open(audio_wav, 'rb') as f:
                    try:
                        resp = self.client.audio.transcriptions.create(
                            model=model_name,
                            file=f,
                            response_format='verbose_json'
                        )
                    except Exception as e:
                        self.logger.error(f"语音转写请求失败: {e}")
                        return None

                cues = self._convert_response_to_cues(resp, 0.0, total_audio_duration)
                if not cues:
                    self.logger.error("转写响应未生成任何字幕片段")
                    return None

            # 渲染字幕
            text = self._render_cues(cues, response_format)
            if not text:
                self.logger.error("无法从转写结果渲染字幕")
                return None

            with open(output_path, 'w', encoding='utf-8') as out:
                out.write(text)

            # Post-check: count subtitle cues; if below threshold and gating enabled, treat as no subtitles
            try:
                if self.config.min_lines_enabled:
                    cue_count = self._count_subtitle_cues(output_path, response_format)
                    self.logger.info(f"ASR字幕条目数: {cue_count}")
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

            self.logger.info(f"语音转写完成，字幕已保存: {output_path}")
            return output_path
        except Exception as e:
            self.logger.error(f"语音转写失败: {e}")
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
            # 排序并去除交叠
            pairs_sec.sort(key=lambda x: x[0])
            merged: List[List[float]] = []
            for st, ed in pairs_sec:
                if not merged:
                    merged.append([st, ed])
                else:
                    last = merged[-1]
                    if st <= last[1] + 0.05:
                        last[1] = max(last[1], ed)
                    else:
                        merged.append([st, ed])
            return [(float(a), float(b)) for a, b in merged]
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
        model_name = self.config.model_name or 'whisper-1'
        try:
            with open(wav_path, 'rb') as f:
                resp = self.client.audio.transcriptions.create(
                    model=model_name,
                    file=f,
                    response_format='verbose_json'
                )
            cues = self._convert_response_to_cues(resp, base_offset_s, total_duration_s)
            if not cues:
                self.logger.warning(f"分段转写响应为空 ({base_offset_s:.2f}s 起)" )
            return cues
        except Exception as e:
            self.logger.warning(f"分段转写失败({base_offset_s:.2f}s): {e}")
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
        )
        return SpeechRecognizer(config, task_id)
    except Exception:
        return None


