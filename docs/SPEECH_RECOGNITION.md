# 语音识别模块 (Speech Recognition Module)

## 概述

语音识别模块已重构为模块化架构，专注于官方 OpenAI Whisper API 兼容性，便于未来对接各种语音识别模型。

## 模块化架构

### 核心组件

1. **SpeechRecognitionConfig** - 配置数据类
   - 统一管理所有语音识别相关配置
   - 支持 API 密钥、基础 URL、模型名称等设置
   - 支持独立的语言检测配置

2. **SpeechRecognizer** - 主要识别器类
   - 抽象化的语音识别接口
   - 支持视频转字幕功能
   - 集成语言检测和响应解析

3. **create_speech_recognizer_from_config()** - 工厂函数
   - 从应用配置创建识别器实例
   - 简化集成和实例化过程

### 配置选项

```json
{
  "SPEECH_RECOGNITION_ENABLED": true,
  "SPEECH_RECOGNITION_PROVIDER": "whisper",
  "SPEECH_RECOGNITION_OUTPUT_FORMAT": "srt",
  "WHISPER_API_KEY": "your-openai-api-key",
  "WHISPER_BASE_URL": "https://api.openai.com/v1",
  "WHISPER_MODEL_NAME": "whisper-1",
  "WHISPER_DETECT_API_KEY": "",
  "WHISPER_DETECT_BASE_URL": "",
  "WHISPER_DETECT_MODEL_NAME": ""
}
```

## 使用示例

### 基本使用

```python
from modules.speech_recognition import create_speech_recognizer_from_config

# 从应用配置创建识别器
config = {
    'SPEECH_RECOGNITION_ENABLED': True,
    'WHISPER_API_KEY': 'your-api-key',
    'WHISPER_BASE_URL': 'https://api.openai.com/v1'
}

recognizer = create_speech_recognizer_from_config(config, task_id='my-task')
if recognizer:
    # 转写视频为字幕
    result = recognizer.transcribe_video_to_subtitles(
        video_path='/path/to/video.mp4',
        output_path='/path/to/output.srt'
    )
```

### 直接配置使用

```python
from modules.speech_recognition import SpeechRecognitionConfig, SpeechRecognizer

# 直接创建配置
config = SpeechRecognitionConfig(
    provider='whisper',
    api_key='your-api-key',
    base_url='https://api.openai.com/v1',
    model_name='whisper-1',
    output_format='srt'
)

# 创建识别器
recognizer = SpeechRecognizer(config, task_id='my-task')
```

## OpenAI Whisper API 兼容性

模块完全兼容官方 OpenAI Whisper API：

- ✅ 支持官方 `whisper-1` 模型
- ✅ 支持 SRT 和 VTT 字幕格式
- ✅ 支持语言自动检测
- ✅ 支持手动指定语言
- ✅ 完整的错误处理和日志记录

## 未来扩展性

模块化设计支持未来添加新的语音识别提供商：

1. **配置扩展** - 在 `SpeechRecognitionConfig` 中添加新字段
2. **提供商检测** - 在 `SpeechRecognizer._init_client()` 中添加新的 provider 判断
3. **API 适配** - 实现新提供商的 API 调用逻辑
4. **响应解析** - 在响应解析方法中添加新格式支持

### 扩展示例

```python
# 未来支持其他提供商的配置示例
config = SpeechRecognitionConfig(
    provider='new_provider',  # 新的提供商
    api_key='new-provider-key',
    base_url='https://new-provider.com/v1',
    model_name='new-model',
    output_format='srt'
)
```

## 特性

- 🔧 **模块化设计** - 清晰的组件分离和接口抽象
- 🎯 **专注官方 API** - 仅支持官方 OpenAI Whisper API
- 🚀 **易于扩展** - 为未来集成其他模型做好准备
- 📝 **完整日志** - 详细的操作日志和错误报告
- ⚙️ **灵活配置** - 支持多种配置方式和选项
- 🔍 **语言检测** - 自动语言检测和手动指定
- 📊 **质量控制** - 字幕条目数量阈值检查

## 迁移说明

从旧版本迁移时：

1. **配置更新** - 移除 `WHISPER_PARAKEET_COMPATIBILITY_MODE` 配置项
2. **API 调用** - 现有的工厂函数调用方式保持不变
3. **功能保持** - 所有核心功能（转写、语言检测等）保持一致

## 性能优化

- 音频预处理：自动转换为 16kHz 单声道 WAV 格式
- 语言检测优化：仅使用前 60 秒音频进行检测
- 内存管理：临时文件自动清理
- 错误恢复：智能重试和降级处理