#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""TwelveLabs Pegasus 视频理解集成（可选）。

在视频下载完成后，使用 TwelveLabs Pegasus 1.5 模型对本地视频做两件事：
  1. 视频级内容安全检测（补充阿里云的文本审核，覆盖纯画面/无字幕风险）；
  2. 生成更贴合画面的中文描述，丰富搬运稿件的简介。

该模块完全可选：未安装 SDK 或未配置 API Key 时静默跳过，不影响既有流程。
免费 API Key 可在 https://twelvelabs.io 获取（有较慷慨的免费额度）。
"""

import json
import logging
import os
import time

# 尝试导入 TwelveLabs SDK，如果失败则设置标记（与阿里云审核模块保持一致的处理方式）
TWELVELABS_AVAILABLE = True
try:
    from twelvelabs import TwelveLabs
    from twelvelabs.types.video_context import VideoContext_AssetId
except ImportError as e:
    TWELVELABS_AVAILABLE = False
    _import_error = str(e)
    # 为静态分析器（Pylance）提供占位，保证名称始终存在
    from typing import Any
    TwelveLabs: Any = None
    VideoContext_AssetId: Any = None

# Pegasus 1.5 约束（已对官方 SDK 实测确认）：
#   - 不接受裸 video_id，需使用 URL 或上传后的 asset_id（本模块走 asset 路径）
#   - direct 方式上传本地文件上限 200MB
#   - max_tokens 必须 >= 512
#   - 分析窗口需 >= 4 秒
DEFAULT_MODEL_NAME = 'pegasus1.5'
ASSET_DIRECT_UPLOAD_MAX_BYTES = 200 * 1024 * 1024  # 200MB
MIN_MAX_TOKENS = 512
ASSET_READY_TIMEOUT_SECONDS = 600
ASSET_POLL_INTERVAL_SECONDS = 5

DEFAULT_MODERATION_PROMPT = (
    "你是视频内容安全审核员。请判断这段视频是否包含暴力、血腥、色情、政治敏感、"
    "违法犯罪或其他不适宜公开发布的内容。只返回严格的 JSON："
    '{"safe": true/false, "labels": ["命中的风险类别"], "reason": "简要中文说明"}。'
    "若内容正常，safe 为 true、labels 为空数组。"
)
DEFAULT_DESCRIPTION_PROMPT = (
    "用一段简洁、客观的中文（150 字以内）描述这段视频的主要画面内容，"
    "便于作为搬运视频的简介。不要编造未出现的信息，不要使用第一人称。"
)


def _get_logger(task_id=None):
    """复用任务日志器；无 task_id 时退回模块级日志器。"""
    if task_id:
        from modules.task_manager import setup_task_logger as task_setup_logger
        return task_setup_logger(task_id)
    return logging.getLogger('twelvelabs_analyzer')


def _skip(reason):
    """统一的跳过返回结构。"""
    return {
        'available': False,
        'moderation': {
            'pass': True,
            'details': [{'label': 'skipped', 'suggestion': 'pass', 'reason': reason}],
        },
        'description': '',
        'reason': reason,
    }


class TwelveLabsAnalyzer:
    """TwelveLabs Pegasus 视频理解封装。"""

    def __init__(self, tl_config, task_id=None):
        """
        Args:
            tl_config (dict): TwelveLabs 配置（API Key、模型名、开关等）
            task_id (str, optional): 任务 ID，用于日志
        """
        self.config = tl_config or {}
        self.task_id = task_id
        self.logger = _get_logger(task_id)
        self.client = None

        if not TWELVELABS_AVAILABLE:
            self.logger.warning(f"TwelveLabs SDK 未安装: {_import_error}")
            self.logger.warning("TwelveLabs 视频理解功能将被跳过")
            return

        api_key = (self.config.get('TWELVELABS_API_KEY')
                   or os.environ.get('TWELVELABS_API_KEY')
                   or '')
        if not api_key:
            self.logger.info("未配置 TwelveLabs API Key，跳过视频理解")
            return

        self.model_name = self.config.get('TWELVELABS_MODEL_NAME') or DEFAULT_MODEL_NAME
        try:
            self.client = TwelveLabs(api_key=api_key)
            self.logger.info("TwelveLabs 客户端初始化成功")
        except Exception as e:  # noqa: BLE001 - 初始化失败应静默降级
            self.logger.error(f"创建 TwelveLabs 客户端失败: {str(e)}")
            self.client = None

    # ---- 公共接口 -------------------------------------------------------

    def analyze_video(self, video_path, want_moderation=True, want_description=True):
        """对本地视频做安全检测与描述生成。

        Args:
            video_path (str): 本地视频文件路径
            want_moderation (bool): 是否执行内容安全检测
            want_description (bool): 是否生成描述

        Returns:
            dict: {available, moderation:{pass,details}, description, reason}
        """
        if not TWELVELABS_AVAILABLE:
            return _skip("TwelveLabs SDK 未安装")
        if not self.client:
            return _skip("TwelveLabs 客户端未初始化")
        if not video_path or not os.path.exists(video_path):
            return _skip(f"视频文件不存在: {video_path}")

        size = os.path.getsize(video_path)
        if size > ASSET_DIRECT_UPLOAD_MAX_BYTES:
            # direct 上传上限 200MB；超限提示改用公开 URL（最高 4GB），此处直接跳过避免报错
            msg = (f"视频体积 {size / 1024 / 1024:.0f}MB 超过直传上限 200MB，"
                   "已跳过 TwelveLabs 分析（可改用公开 URL 输入）")
            self.logger.warning(msg)
            return _skip(msg)

        asset_id = None
        try:
            asset_id = self._upload_asset(video_path)
            if not asset_id:
                return _skip("视频资产上传或处理失败")

            result = {
                'available': True,
                'moderation': {'pass': True, 'details': []},
                'description': '',
                'reason': '',
            }
            if want_moderation:
                result['moderation'] = self._moderate(asset_id)
            if want_description:
                result['description'] = self._describe(asset_id)
            return result
        except Exception as e:  # noqa: BLE001 - 任何异常都降级为跳过，避免阻断搬运
            self.logger.error(f"TwelveLabs 视频分析出错: {str(e)}")
            import traceback
            self.logger.error(traceback.format_exc())
            return _skip(f"视频分析异常: {str(e)}")
        finally:
            if asset_id:
                self._delete_asset(asset_id)

    # ---- 内部实现 -------------------------------------------------------

    def _upload_asset(self, video_path):
        """上传本地文件为 asset 并等待 ready，返回 asset_id。"""
        self.logger.info(f"上传视频到 TwelveLabs: {os.path.basename(video_path)}")
        with open(video_path, 'rb') as f:
            asset = self.client.assets.create(
                method='direct', file=f, filename=os.path.basename(video_path)
            )
        asset_id = asset.id
        deadline = time.time() + ASSET_READY_TIMEOUT_SECONDS
        status = getattr(asset, 'status', None)
        while status not in ('ready', 'failed') and time.time() < deadline:
            time.sleep(ASSET_POLL_INTERVAL_SECONDS)
            status = getattr(self.client.assets.retrieve(asset_id=asset_id), 'status', None)
        if status != 'ready':
            self.logger.warning(f"视频资产未就绪（status={status}），跳过分析")
            self._delete_asset(asset_id)
            return None
        self.logger.info("视频资产已就绪")
        return asset_id

    def _analyze(self, asset_id, prompt, max_tokens=MIN_MAX_TOKENS):
        """调用 Pegasus 分析，返回文本（失败返回空串）。"""
        resp = self.client.analyze(
            model_name=self.model_name,
            video=VideoContext_AssetId(asset_id=asset_id),
            prompt=prompt,
            max_tokens=max(MIN_MAX_TOKENS, int(max_tokens)),
        )
        return (resp.data or '').strip()

    def _moderate(self, asset_id):
        """视频级安全检测，输出与阿里云审核一致的 {pass, details} 结构。"""
        prompt = self.config.get('TWELVELABS_MODERATION_PROMPT') or DEFAULT_MODERATION_PROMPT
        text = self._analyze(asset_id, prompt)
        verdict = _parse_moderation_json(text)
        if verdict is None:
            self.logger.warning(f"无法解析视频审核结果，按通过处理。原始输出: {text[:200]}")
            return {
                'pass': True,
                'details': [{'label': 'unparsed', 'suggestion': 'pass',
                             'reason': f'模型输出无法解析: {text[:120]}'}],
            }
        safe = bool(verdict.get('safe', True))
        labels = verdict.get('labels') or []
        reason = verdict.get('reason') or ''
        self.logger.info(f"视频审核结果: safe={safe}, labels={labels}")
        if safe:
            return {'pass': True,
                    'details': [{'label': 'normal', 'suggestion': 'pass', 'reason': reason or '画面内容正常'}]}
        return {
            'pass': False,
            'details': [{'label': ','.join(labels) if labels else 'unsafe',
                         'suggestion': 'review', 'reason': reason or '视频画面命中风险'}],
        }

    def _describe(self, asset_id):
        """生成视频中文描述。"""
        prompt = self.config.get('TWELVELABS_DESCRIPTION_PROMPT') or DEFAULT_DESCRIPTION_PROMPT
        text = self._analyze(asset_id, prompt)
        self.logger.info(f"生成视频描述: {text[:120]}")
        return text

    def _delete_asset(self, asset_id):
        try:
            self.client.assets.delete(asset_id=asset_id)
        except Exception as e:  # noqa: BLE001 - 清理失败不影响主流程
            self.logger.debug(f"删除 TwelveLabs 资产失败（可忽略）: {str(e)}")


def _parse_moderation_json(text):
    """从模型输出中尽量解析出审核 JSON；失败返回 None。"""
    if not text:
        return None
    candidate = text.strip()
    # 去掉 ```json ... ``` 代码块包裹
    if candidate.startswith('```'):
        candidate = candidate.strip('`')
        if candidate.lower().startswith('json'):
            candidate = candidate[4:]
        candidate = candidate.strip()
    try:
        return json.loads(candidate)
    except (ValueError, TypeError):
        pass
    # 退而求其次：截取第一个 { 到最后一个 }
    start, end = candidate.find('{'), candidate.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(candidate[start:end + 1])
        except (ValueError, TypeError):
            return None
    return None
