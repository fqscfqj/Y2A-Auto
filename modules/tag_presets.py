#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""预设标签（Issue #139）的解析、归一化与按平台合并。

产品语义（与设置页「标签预设」帮助文案保持一致）：

- **预设在前**：人工预设的标签排在任务已有标签之前，AI 生成的标签只补齐剩余名额；
- **一份预设服务两个平台**：落平台时按各平台上限截断
  （AcFun 6 个 / 每标签 10 字，bilibili 12 个 / 每标签 20 字）；
- **关闭即零变化**：``PRESET_TAGS_ENABLED`` 关闭或预设为空时，
  :func:`resolve_upload_tags` 原样返回任务标签，不改变任何既有行为。

本模块只依赖标准库。``task_manager`` 与 ``app`` 都会 import 它，反向 import
（例如复用 ``task_manager._normalize_tags_list``）会形成循环导入，故此处自带
JSON 列表解析（语义与 ``task_manager._normalize_tags_list`` 对齐：JSON 解析失败
即视为空，不做分隔符兜底，避免「特性关闭路径」的行为漂移）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: AcFun 标签数量上限（见 modules/acfun_uploader.py 上传前的截断）
ACFUN_TAG_LIMIT = 6
#: AcFun 单标签字数上限（与 modules/ai_enhancer.py 生成标签时的截断口径一致）
ACFUN_TAG_MAX_LEN = 10
#: bilibili 标签数量上限（见 modules/bilibili_uploader.py:271-272）
BILIBILI_TAG_LIMIT = 12
#: bilibili 单标签字数上限
BILIBILI_TAG_MAX_LEN = 20

#: 配置侧保存的预设标签数量上限；超出部分不落盘（AcFun 只会用前 6 个）
PRESET_TAG_MAX_COUNT = BILIBILI_TAG_LIMIT

PLATFORM_ACFUN = 'acfun'
PLATFORM_BILIBILI = 'bilibili'

#: 预设标签的分隔符：换行 / 半角与全角逗号 / 顿号 / 半角与全角分号。
#: 与监控关键词、WHISPER_TIMESTAMP_GRANULARITIES 等既有配置同属「分隔字符串」惯例。
TAG_SPLIT_RE = re.compile(r'[\n\r,，、;；]+')

_PLATFORM_LIMITS = {
    PLATFORM_ACFUN: (ACFUN_TAG_LIMIT, ACFUN_TAG_MAX_LEN),
    PLATFORM_BILIBILI: (BILIBILI_TAG_LIMIT, BILIBILI_TAG_MAX_LEN),
}

_PLATFORM_LABELS = {
    PLATFORM_ACFUN: 'AcFun',
    PLATFORM_BILIBILI: 'bilibili',
}


def coerce_bool(value: Any) -> bool:
    """将配置值稳健转换为布尔值（兼容 bool/int/float/str）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ('true', '1', 'on', 'yes')


def _maybe_json_list(text: str) -> Optional[List[Any]]:
    """仅当字符串确实是 JSON 数组时返回其内容，否则返回 None。"""
    stripped = text.strip()
    if not stripped.startswith('['):
        return None
    try:
        parsed = json.loads(stripped)
    except Exception:
        return None
    if isinstance(parsed, list):
        return parsed
    return None


def _coerce_task_tags(raw: Any) -> List[Any]:
    """解析任务 ``tags_generated`` 字段：list 直接用，字符串按 JSON 数组解析。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return list(raw)
    parsed = _maybe_json_list(str(raw))
    return parsed if parsed is not None else []


def _normalize_task_tags(raw: Any) -> List[str]:
    """与 ``task_manager._normalize_tags_list`` 逐字对齐的规范化。

    刻意**不做去重**：预设功能关闭时上传路径必须与改动前拿到完全一样的列表
    （含重复项），去重只发生在预设生效的合并路径里。
    """
    tags: List[str] = []
    for item in _coerce_task_tags(raw):
        tag = str(item or '').strip()
        if tag:
            tags.append(tag)
    return tags


def _coerce_preset_items(raw: Any) -> List[Any]:
    """解析预设配置：list 直接用，字符串先试 JSON 数组，再按分隔符切分。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return list(raw)
    text = str(raw)
    parsed = _maybe_json_list(text)
    if parsed is not None:
        return parsed
    return TAG_SPLIT_RE.split(text)


def _iter_tags(value: Any) -> List[str]:
    """把「标签集合」入参统一成字符串列表；单个字符串按一个标签处理（不按字符拆）。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    try:
        return [str(item) for item in value]
    except TypeError:
        return [str(value)]


def _dedupe_tags(items: Iterable[Any]) -> List[str]:
    """trim → 去空 → 大小写不敏感去重，保持首次出现的顺序。"""
    tags: List[str] = []
    seen = set()
    for item in items:
        tag = str(item or '').strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
    return tags


def parse_preset_tags(raw: Any) -> List[str]:
    """把配置值解析成预设标签列表（保序、去空、大小写不敏感去重）。

    接受 ``list`` / ``tuple``、JSON 数组字符串、以及普通分隔字符串
    （换行 / 逗号 / 顿号 / 分号可混用）。此处不截断长度、不限制数量：
    数量与长度的裁决分别由 :func:`normalize_preset_tags_config`（配置侧）
    与 :func:`resolve_upload_tags`（上传侧）负责。
    """
    return _dedupe_tags(_coerce_preset_items(raw))


def normalize_preset_tags_config(raw: Any) -> Tuple[str, List[str]]:
    """归一化设置页提交的预设标签，返回 ``(落盘文本, 告警文案列表)``。

    落盘文本统一为「每行一个」，使重复保存幂等。对用户可见的后果一律显式告警，
    不做静默丢弃：

    - 超过 ``PRESET_TAG_MAX_COUNT``（bilibili 上限）的标签不落盘；
    - 超过 6 个时提示 AcFun 只上传前 6 个；
    - 单标签超过 20 字时提示两个平台各自的截断长度。
    """
    tags = parse_preset_tags(raw)
    warnings: List[str] = []

    if len(tags) > PRESET_TAG_MAX_COUNT:
        dropped = len(tags) - PRESET_TAG_MAX_COUNT
        tags = tags[:PRESET_TAG_MAX_COUNT]
        warnings.append(
            f'标签预设最多 {PRESET_TAG_MAX_COUNT} 个（bilibili 上限），'
            f'已保留前 {PRESET_TAG_MAX_COUNT} 个，忽略后面的 {dropped} 个。'
        )

    if len(tags) > ACFUN_TAG_LIMIT:
        warnings.append(
            f'标签预设共 {len(tags)} 个，AcFun 只会上传前 {ACFUN_TAG_LIMIT} 个，'
            '其余仅用于 bilibili。'
        )

    overlong = [tag for tag in tags if len(tag) > BILIBILI_TAG_MAX_LEN]
    if overlong:
        warnings.append(
            f'标签预设中有 {len(overlong)} 个标签超过 {BILIBILI_TAG_MAX_LEN} 字'
            f'（如「{overlong[0]}」），bilibili 会截断到 {BILIBILI_TAG_MAX_LEN} 字、'
            f'AcFun 会截断到 {ACFUN_TAG_MAX_LEN} 字。'
        )

    return '\n'.join(tags), warnings


def is_preset_tags_enabled(config: Any) -> bool:
    """``PRESET_TAGS_ENABLED`` 是否勾选（不判断预设内容是否为空）。"""
    if not isinstance(config, dict):
        return False
    return coerce_bool(config.get('PRESET_TAGS_ENABLED', False))


def is_preset_tags_effective(config: Any) -> bool:
    """启用且确实填了标签才算生效；「启用了但留空」等同未启用。"""
    if not is_preset_tags_enabled(config):
        return False
    return bool(parse_preset_tags(config.get('PRESET_TAGS', '')))


def _platform_limits(platform: Any) -> Tuple[str, int, Optional[int]]:
    """返回 ``(规范化平台名, 标签数量上限, 预设标签字数上限)``。

    字数上限只用于预设标签（任务自身标签由上传器/历史行为决定，不因预设开关而变）。
    未知/缺省平台按 AcFun 处理，与 ``task_manager._get_effective_metadata_limits``
    的保守口径一致（无法确定平台时按更严格的限制执行）。
    """
    key = str(platform or '').strip().lower()
    if key not in _PLATFORM_LIMITS:
        key = PLATFORM_ACFUN
    limit, max_len = _PLATFORM_LIMITS[key]
    return key, limit, max_len


def _merge_tags_report(
    preset: Any,
    extra: Any,
    *,
    limit: int,
    preset_max_len: Optional[int] = None,
) -> Tuple[List[str], int, int]:
    """合并标签并返回 ``(结果, 因超量被丢弃数, 被截断数)``。

    顺序为 ``preset + extra``；trim → 去空 → （仅预设）单标签截断 → 小写去重 → 截到 ``limit``。
    重复项不计入「丢弃数」（预设与任务标签重叠是常态，也正是本函数幂等的来源）。

    ``preset_max_len`` **只作用于预设标签**：任务自身标签（AI 生成或人工手改）一律原样
    保留。否则同一个手工标签会因为「预设开关」这个与它无关的配置改变上传内容 ——
    例如 AcFun 在预设关闭时原样上传 12 字的手工标签，开启预设后却被截到 10 字。
    """
    merged: List[str] = []
    seen = set()
    dropped = 0
    truncated = 0

    for is_preset, items in ((True, _iter_tags(preset)), (False, _iter_tags(extra))):
        for item in items:
            tag = str(item or '').strip()
            if not tag:
                continue
            if is_preset and preset_max_len and len(tag) > preset_max_len:
                tag = tag[:preset_max_len].strip()
                if not tag:
                    dropped += 1
                    continue
                truncated += 1
            key = tag.lower()
            if key in seen:
                continue
            if limit and len(merged) >= limit:
                dropped += 1
                continue
            seen.add(key)
            merged.append(tag)

    return merged, dropped, truncated


def merge_tags(
    preset: Any,
    extra: Any,
    *,
    limit: int,
    preset_max_len: Optional[int] = None,
) -> List[str]:
    """预设在前、其余标签在后的去重合并。

    ``preset_max_len`` 只截断预设标签，``extra``（任务自身标签）原样保留。
    """
    merged, _dropped, _truncated = _merge_tags_report(
        preset, extra, limit=limit, preset_max_len=preset_max_len
    )
    return merged


def _log_resolution(
    logger_obj: Any,
    platform_key: str,
    limit: int,
    preset_max_len: Optional[int],
    dropped: int,
    truncated: int,
) -> None:
    """仅在真的发生截断/丢弃时打一条 warning，避免每轮上传刷日志。"""
    if logger_obj is None:
        return
    label = _PLATFORM_LABELS.get(platform_key, platform_key)
    try:
        if dropped:
            logger_obj.warning(
                f"预设标签：{label} 标签上限 {limit} 个，已丢弃 {dropped} 个（预设优先保留）"
            )
        if truncated:
            logger_obj.warning(
                f"预设标签：{truncated} 个预设标签超过 {label} 的单标签 {preset_max_len} 字上限，已截断"
            )
    except Exception:  # 日志失败绝不能影响上传
        pass


def resolve_upload_tags(
    config: Any,
    task_tags: Any,
    platform: Any,
    logger_obj: Any = None,
) -> List[str]:
    """返回某平台最终要上传的标签列表。

    - 预设未生效：仅把任务标签规范化后**原样返回**（顺序、长度都不动），
      保证 ``PRESET_TAGS_ENABLED`` 关闭时与改动前行为一致；
    - 预设生效：预设在前 + 任务标签在后，去重后按平台上限截断；
      单标签字数截断**只作用于预设标签**，任务自身标签原样保留。
    """
    platform_key, limit, preset_max_len = _platform_limits(platform)
    existing = _normalize_task_tags(task_tags)

    if not is_preset_tags_effective(config):
        return existing

    preset = parse_preset_tags(config.get('PRESET_TAGS', ''))
    merged, dropped, truncated = _merge_tags_report(
        preset, existing, limit=limit, preset_max_len=preset_max_len
    )
    _log_resolution(logger_obj, platform_key, limit, preset_max_len, dropped, truncated)
    return merged
