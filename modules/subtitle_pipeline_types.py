#!/usr/bin/env python
# -*- coding: utf-8 -*-

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class DetectedSpeechWindow:
    start_s: float
    end_s: float
    ownership_start_s: float
    ownership_end_s: float
    chunk_index: int = 0
    total_chunks: int = 1
    source_pass: str = 'scan'
    threshold: float = 0.0
    coverage_ratio: float = 0.0
    speech_duration_s: float = 0.0
    refined: bool = False
    raw_spans: List[Tuple[float, float]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return max(0.0, float(self.end_s) - float(self.start_s))


@dataclass
class AsrWordTiming:
    start_s: float
    end_s: float
    text: str

    @property
    def duration_s(self) -> float:
        return max(0.0, float(self.end_s) - float(self.start_s))


@dataclass
class AsrSegmentTiming:
    start_s: float
    end_s: float
    text: str
    words: List[AsrWordTiming] = field(default_factory=list)
    confidence: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return max(0.0, float(self.end_s) - float(self.start_s))


@dataclass
class AsrTranscriptionResult:
    provider: str
    response_format: str
    timestamp_mode: str
    text: str = ''
    language: str = ''
    segments: List[AsrSegmentTiming] = field(default_factory=list)
    window: Optional[DetectedSpeechWindow] = None
    failure_token: str = ''
    fallback_token: str = ''
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.segments or (self.text or '').strip())


@dataclass
class AlignedSubtitleCue:
    start_s: float
    end_s: float
    text: str
    provider: str = ''
    timing_source: str = 'segment'
    alignment_confidence: float = 0.0
    source_window_index: int = -1
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'start': float(self.start_s),
            'end': float(self.end_s),
            'text': str(self.text or ''),
            'provider': self.provider,
            'timing_source': self.timing_source,
            'alignment_confidence': float(self.alignment_confidence),
            'source_window_index': int(self.source_window_index),
            'metadata': dict(self.metadata or {}),
        }
