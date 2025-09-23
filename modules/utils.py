#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import json
import shutil
import logging
from datetime import datetime
from PIL import Image, ImageOps

def get_app_root_dir():
    """
    获取应用根目录，兼容开发环境和打包环境
    
    Returns:
        str: 应用根目录路径
    """
    if getattr(sys, 'frozen', False):
        # 在PyInstaller打包环境中
        # sys.executable 指向的是实际的可执行文件
        app_root = os.path.dirname(sys.executable)
    else:
        # 在开发环境中
        # __file__ 是当前文件的路径，需要向上两级找到项目根目录
        app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    return app_root

def get_app_subdir(subdir_name):
    """
    获取应用子目录路径
    
    Args:
        subdir_name (str): 子目录名称，如 'config', 'logs', 'db' 等
        
    Returns:
        str: 子目录的完整路径
    """
    return os.path.join(get_app_root_dir(), subdir_name)

"""
工具函数模块
"""

import re
from PIL import Image

def init_app():
    """
    初始化应用
    """
    pass

def parse_id_md_to_json(md_content_str):
    """
    解析id.md文件内容，将其转换为结构化的JSON
    
    Args:
        md_content_str (str): id.md文件的内容字符串
        
    Returns:
        list: 分类和分区的JSON结构
    """
    result = []
    current_category = None
    current_partition = None
    
    # 按行处理
    lines = md_content_str.strip().split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # 检查是否为分类行 (例如: "## 番剧")
        category_match = re.match(r'^##\s+(.+)$', line)
        if category_match:
            category_name = category_match.group(1).strip()
            current_category = {
                "category": category_name,
                "partitions": []
            }
            result.append(current_category)
            continue
            
        # 检查是否为一级分区行 (例如: "- **TV动画** `67` - 连载日本动画（季番/半年番）")
        partition_match = re.match(r'^-\s+\*\*(.+?)\*\*\s+`(\d+)`\s*-\s*(.*)$', line)
        if partition_match:
            partition_name = partition_match.group(1).strip()
            partition_id = partition_match.group(2).strip()
            partition_description = partition_match.group(3).strip() if partition_match.group(3) else ""
            
            current_partition = {
                "id": partition_id,
                "name": partition_name,
                "description": partition_description,
                "sub_partitions": []
            }
            
            if current_category:
                current_category["partitions"].append(current_partition)
            continue
            
        # 检查是否为二级分区行 (例如: "- **动画综合** `106` - 无法归类到其他子分区的动画相关内容")
        # 结构与一级分区相似，但缩进不同，在当前实现中我们通过行的开头来区分
        sub_partition_match = re.match(r'^\s+-\s+\*\*(.+?)\*\*\s+`(\d+)`\s*-\s*(.*)$', line)
        if not sub_partition_match:
            # 尝试另一种可能的格式 (例如: "- **王者荣耀** `214`")
            sub_partition_match = re.match(r'^\s+-\s+\*\*(.+?)\*\*\s+`(\d+)`\s*$', line)
            
        if sub_partition_match:
            sub_name = sub_partition_match.group(1).strip()
            sub_id = sub_partition_match.group(2).strip()
            sub_description = sub_partition_match.group(3).strip() if len(sub_partition_match.groups()) > 2 and sub_partition_match.group(3) else ""
            
            sub_partition = {
                "id": sub_id,
                "name": sub_name,
                "description": sub_description
            }
            
            if current_partition:
                current_partition["sub_partitions"].append(sub_partition)
            continue
    
    return result

def process_cover(image_path, output_path=None, mode='crop'):
    """
    处理视频封面图片，使其适合AcFun上传要求（16:10比例）
    
    Args:
        image_path (str): 输入图片路径
        output_path (str, optional): 输出图片路径，如果不提供则覆盖原图片
        mode (str): 处理模式，'crop'表示裁剪，'pad'表示添加黑边
        
    Returns:
        str: 处理后的图片路径
    """
    if not output_path:
        output_path = image_path
        
    try:
        # 打开图片
        img = Image.open(image_path)
        width, height = img.size
        
        # 目标比例 16:10
        target_ratio = 16 / 10
        current_ratio = width / height
        
        if mode == 'crop':
            # 裁剪模式
            if current_ratio > target_ratio:
                # 图片太宽，需要裁剪宽度
                new_width = int(height * target_ratio)
                left = (width - new_width) // 2
                img = img.crop((left, 0, left + new_width, height))
            elif current_ratio < target_ratio:
                # 图片太高，需要裁剪高度
                new_height = int(width / target_ratio)
                top = (height - new_height) // 2
                img = img.crop((0, top, width, top + new_height))
        elif mode == 'pad':
            # 填充模式
            if current_ratio > target_ratio:
                # 图片太宽，需要增加高度
                new_height = int(width / target_ratio)
                new_img = Image.new('RGB', (width, new_height), (0, 0, 0))
                paste_y = (new_height - height) // 2
                new_img.paste(img, (0, paste_y))
                img = new_img
            elif current_ratio < target_ratio:
                # 图片太高，需要增加宽度
                new_width = int(height * target_ratio)
                new_img = Image.new('RGB', (new_width, height), (0, 0, 0))
                paste_x = (new_width - width) // 2
                new_img.paste(img, (paste_x, 0))
                img = new_img
        
        # 保存处理后的图片
        img.save(output_path, quality=95)
        return output_path
    except Exception as e:
        print(f"处理封面图片时出错: {str(e)}")
        return image_path 

# -----------------------------
# LLM 输出清洗与兼容辅助函数
# -----------------------------

def strip_reasoning_thoughts(text):
    """
    屏蔽/移除思考模型产出的思考内容，仅保留最终答案。
    - 兼容 DeepSeek 的 <think>...</think> 标签
    - 兼容 ```think ...``` 代码块形式

    Args:
        text (str): 原始模型输出

    Returns:
        str: 已移除思考内容的纯净文本
    """
    try:
        if not isinstance(text, str):
            return text

        cleaned = text

        # 移除 <think>...</think>（大小写不敏感，跨行匹配）
        cleaned = re.sub(r'(?is)<\s*think\s*>.*?<\s*/\s*think\s*>', '', cleaned)

        # 移除 ```think ...``` 样式的思考内容代码块（仅当语言标记包含 think 时）
        cleaned = re.sub(r'(?is)```\s*think[^\n]*\n.*?```', '', cleaned)

        # 去除多余空白
        cleaned = cleaned.strip()
        return cleaned
    except Exception:
        return text

def safe_str(value, default=''):
    """
    将任意值安全转换为字符串，如果为 None 则返回默认值（默认为空字符串）。

    Args:
        value: 可能为 None 或其他类型的值
        default: 当 value 为 None 或空时返回的默认字符串

    Returns:
        str: 安全的字符串表示
    """
    try:
        if value is None:
            return default
        # 如果已经是字符串，直接返回（保持原样）
        if isinstance(value, str):
            return value
        # 否则尝试转换为字符串
        return str(value)
    except Exception:
        return default