# Parakeet API Docker 兼容性指南

## 概述

Y2A-Auto 现已支持 [@fqscfqj/parakeet-api-docker](https://github.com/fqscfqj/parakeet-api-docker) 兼容模式，在保持与 OpenAI Whisper API 完全兼容的同时，提供对 parakeet-api-docker 特定响应格式的支持。

## 配置方式

在 `config/config.json` 中添加以下配置：

```json
{
  "WHISPER_PARAKEET_COMPATIBILITY_MODE": true,
  "WHISPER_BASE_URL": "http://localhost:8000/v1",
  "WHISPER_API_KEY": "your-api-key"
}
```

## 支持的响应格式

### OpenAI 标准格式（始终支持）

```json
// 字符串响应
"This is the transcribed text."

// 对象响应（带 .text 属性）
{
  "text": "This is the transcribed text."
}
```

### Parakeet 兼容格式（兼容模式下支持）

#### 1. 直接 text 字段
```json
{
  "text": "This is parakeet format response."
}
```

#### 2. result.text 格式
```json
{
  "result": {
    "text": "This is parakeet format with nested result."
  }
}
```

#### 3. result 字符串格式
```json
{
  "result": "This is parakeet format with result string."
}
```

#### 4. transcription 字段格式
```json
{
  "transcription": "This is parakeet format with transcription field."
}
```

### 语言检测响应格式

#### OpenAI 标准格式
```json
{
  "language": "zh",
  "segments": [
    {"avg_logprob": -0.1}
  ]
}
```

#### Parakeet 兼容格式

##### 直接格式
```json
{
  "language": "zh",
  "confidence": 0.95
}
```

##### result 格式
```json
{
  "result": {
    "lang": "zh",
    "score": 0.88
  }
}
```

##### detection 格式
```json
{
  "detection": {
    "language_code": "zh",
    "probability": 0.92
  }
}
```

## 错误处理

兼容模式下，系统能够正确处理两种API的错误格式：

### OpenAI 错误格式
```json
{
  "error": {
    "code": "unsupported_language",
    "message": "Unsupported language: xyz",
    "param": "language"
  }
}
```

### Parakeet 错误格式
系统会自动识别并处理 parakeet-api-docker 特定的错误响应格式。

## 配置选项说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `WHISPER_PARAKEET_COMPATIBILITY_MODE` | boolean | `false` | 启用 parakeet-api-docker 兼容模式 |
| `WHISPER_BASE_URL` | string | `""` | Whisper API 基础URL（parakeet服务地址） |
| `WHISPER_API_KEY` | string | `""` | API 密钥 |
| `WHISPER_MODEL_NAME` | string | `"whisper-1"` | 模型名称 |

## 向后兼容性

- ✅ **完全向后兼容**：现有使用 OpenAI Whisper API 的配置无需任何更改
- ✅ **可选启用**：兼容模式需要显式启用，不会影响现有功能
- ✅ **优雅降级**：当 parakeet 格式解析失败时，自动回退到标准 OpenAI 格式处理
- ✅ **零破坏性**：所有现有 API 调用和响应处理保持不变

## 使用示例

### 启用 Parakeet 兼容模式

```python
# 在 config.json 中
{
  "SPEECH_RECOGNITION_ENABLED": true,
  "WHISPER_PARAKEET_COMPATIBILITY_MODE": true,
  "WHISPER_BASE_URL": "http://your-parakeet-server:8000/v1",
  "WHISPER_API_KEY": "your-key"
}
```

### 保持 OpenAI 标准模式

```python
# 在 config.json 中
{
  "SPEECH_RECOGNITION_ENABLED": true,
  "WHISPER_PARAKEET_COMPATIBILITY_MODE": false,  // 或直接省略该配置
  "WHISPER_BASE_URL": "https://api.openai.com/v1",
  "WHISPER_API_KEY": "sk-your-openai-key"
}
```

## 技术实现

### 响应解析流程

1. **错误检查**：首先检查响应是否包含错误信息
2. **Parakeet 格式检查**（兼容模式下）：尝试解析 parakeet 特定的响应字段
3. **标准格式回退**：如果 parakeet 格式解析失败，回退到标准 OpenAI 格式处理
4. **结果返回**：返回解析后的文本或语言检测结果

### 日志记录

兼容模式下会输出详细的调试日志，帮助了解响应解析过程：

```
DEBUG: 使用parakeet兼容模式解析响应：找到text字段
DEBUG: 使用parakeet兼容模式解析语言检测响应：直接格式
INFO: 语音识别客户端初始化成功(含语言检测) (parakeet兼容模式)
```

## 测试验证

项目包含完整的测试套件验证兼容性：

- 运行 `python test_parakeet_compatibility_simple.py` 测试各种响应格式
- 验证标准模式和兼容模式都能正常工作
- 确保错误处理机制正确

## 故障排除

### 常见问题

1. **兼容模式未生效**
   - 检查 `WHISPER_PARAKEET_COMPATIBILITY_MODE` 是否设置为 `true`
   - 查看日志中是否有 "parakeet兼容模式" 提示

2. **响应解析失败**
   - 查看调试日志了解解析过程
   - 确认 parakeet-api-docker 返回的响应格式

3. **标准 OpenAI API 不工作**
   - 确认 `WHISPER_PARAKEET_COMPATIBILITY_MODE` 设置为 `false` 或未设置
   - 检查 `WHISPER_BASE_URL` 是否指向正确的 OpenAI API 端点

### 日志分析

启用详细日志记录来诊断问题：

```python
# 查看语音识别相关日志
tail -f logs/task_*.log | grep -i "speech\|whisper\|parakeet"
```

## 总结

Parakeet API Docker 兼容模式为 Y2A-Auto 提供了灵活的语音识别后端选择，用户可以：

- 继续使用 OpenAI Whisper API（默认）
- 切换到 parakeet-api-docker 服务
- 在两者之间无缝切换
- 享受零停机时间的配置更新

这种设计确保了最大的兼容性和灵活性，同时保持代码的简洁性和可维护性。