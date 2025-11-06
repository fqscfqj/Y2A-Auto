# YouTube 视频字幕生成优化策略

## 概述

本实现基于问题陈述中的完整策略，提供全自动、高稳健性的 YouTube 视频字幕生成功能，使用 LocalAI（Whisper 转写 + Silero-VAD 切分）实现"分片 + VAD + 约束 + ASR + 字幕后处理"的完整流水线。

## 核心特性

### 1. 音频分片策略（Audio Chunking）

**目的**：处理长视频时避免超时和内存问题

**配置参数**：
- `AUDIO_CHUNK_WINDOW_S`: 固定窗口大小（默认 25.0 秒，推荐 20-30 秒）
- `AUDIO_CHUNK_OVERLAP_S`: 重叠时间（默认 0.2 秒，确保跨段连续性）

**工作原理**：
```
原音频: |-------------------------------------------|
分片:    |---w1---|---w2---|---w3---|---w4---|
重叠:          ^^       ^^       ^^
```

### 2. VAD 后处理约束（VAD Post-processing Constraints）

**目的**：控制字幕粒度，提高可读性

**配置参数**：
- `VAD_MERGE_GAP_S`: 合并间隙阈值（默认 0.25 秒）
  - 小于此值的相邻片段将被合并，防止碎段
- `VAD_MIN_SEGMENT_S`: 最短片段时长（默认 1.0 秒）
  - 过短的片段会合并到前后片段，防止"一闪而过"的字幕
- `VAD_MAX_SEGMENT_S_FOR_SPLIT`: 最长片段时长（默认 8.0 秒）
  - 超过此值的片段会被二次切分，提高可读性
- `VAD_SILENCE_THRESHOLD_S`: 静音阈值（默认 0.3 秒）
  - 用于在长片段内寻找合适的切分点

**工作流程**：
```
1. VAD 原始输出: [0.1-0.5s] [0.6-1.2s] [3.0-15.0s]
2. 合并近邻:     [0.1-1.2s] [3.0-15.0s]
3. 过滤短段:     [0.1-1.2s] [3.0-15.0s] (1.1s > 1.0s ✓)
4. 切分长段:     [0.1-1.2s] [3.0-11.0s] [11.0-15.0s]
```

### 3. 转写参数优化（Transcription Optimization）

**目的**：减少幻觉，提高准确性

**配置参数**：
- `WHISPER_LANGUAGE`: 强制语言（如 'en', 'zh', 'ja'）
  - 已知语言时显式指定，减少错误语言推断
  - 留空则自动检测
- `WHISPER_PROMPT`: 转写提示文本
  - 示例: "只转写实际说话内容，不补充未说出的词语"
  - 帮助引导模型，减少幻觉
- `WHISPER_TRANSLATE`: 是否翻译为英文（默认 False）
- `WHISPER_MAX_WORKERS`: 并行转写线程数（默认 3，推荐 2-4）

### 4. 文本后处理（Text Post-processing）

**目的**：确保字幕可读性和格式规范

**配置参数**：
- `SUBTITLE_MAX_LINE_LENGTH`: 每行最大字符数（默认 42）
  - CJK 字符建议 15-20 字/行
  - 英文建议 35-42 字符/行
- `SUBTITLE_MAX_LINES`: 每个字幕最多行数（默认 2）
- `SUBTITLE_NORMALIZE_PUNCTUATION`: 标准化标点（默认 True）
  - 统一标点后的空格
  - 去除多余空白
- `SUBTITLE_FILTER_FILLER_WORDS`: 过滤填充词（默认 False）
  - 移除 "um", "uh", "嗯", "啊" 等

**文本处理流程**：
```
1. 原始文本: "Um,  well  hello,world!How are you?"
2. 标点规范: "Um, well hello, world! How are you?"
3. 过滤填充: "well hello, world! How are you?"
4. 切分长句: "well hello, world!" | "How are you?"
```

### 5. 重试与容错机制（Retry & Fallback）

**目的**：提高稳健性，应对网络/服务不稳定

**配置参数**：
- `WHISPER_MAX_RETRIES`: 最大重试次数（默认 3）
- `WHISPER_RETRY_DELAY_S`: 初始重试延迟（默认 2.0 秒）
  - 使用指数退避: 2s, 4s, 8s...
- `WHISPER_FALLBACK_TO_FIXED_CHUNKS`: VAD 失败时回退（默认 True）
  - 若 VAD 失败，自动切换到固定窗口分片

**容错策略**：
```
1. 尝试 VAD 分段识别
   ↓ 失败
2. 回退到固定窗口分片 (25s 窗口)
   ↓ 单次失败
3. 重试 (指数退避: 2s, 4s, 8s)
   ↓ 全部失败
4. 记录错误，继续处理其他片段
```

## 完整工作流程

```
步骤 1: 音频提取与预处理
  ├─ 从视频提取音频
  ├─ 转换为 16kHz 单声道 WAV
  └─ 探测音频时长

步骤 2: 音频分片（若需要）
  ├─ 判断时长 > 25s?
  ├─ Yes: 创建重叠分片 (25s 窗口, 0.2s 重叠)
  └─ No: 使用整段音频

步骤 3: VAD 处理（若启用）
  ├─ 对每个分片调用 VAD API
  ├─ 合并所有分片的 VAD 结果
  ├─ 应用后处理约束:
  │   ├─ 合并近邻间隙 (< 0.25s)
  │   ├─ 过滤短段 (< 1.0s)
  │   └─ 切分长段 (> 8.0s)
  └─ 失败时回退到固定分片

步骤 4: 语音转写
  ├─ 对每个语音段:
  │   ├─ 裁剪音频片段
  │   ├─ 调用 Whisper API
  │   │   ├─ 设置语言参数
  │   │   ├─ 添加提示文本
  │   │   └─ 重试机制 (最多 3 次)
  │   └─ 文本后处理:
  │       ├─ 标准化标点
  │       ├─ 过滤填充词
  │       └─ 切分长句
  └─ 合并所有结果

步骤 5: 字幕渲染
  ├─ 生成 SRT 或 VTT 格式
  ├─ 应用时间戳
  └─ 写入文件

步骤 6: 质量检查
  ├─ 统计字幕条目数
  ├─ 少于阈值? 丢弃
  └─ 通过: 保存字幕文件
```

## 配置示例

### 基础配置（适用于大多数场景）

```json
{
  "SPEECH_RECOGNITION_ENABLED": true,
  "WHISPER_API_KEY": "your-api-key",
  "WHISPER_BASE_URL": "http://localhost:8080/v1",
  "WHISPER_MODEL_NAME": "whisper-1",
  "WHISPER_LANGUAGE": "",
  
  "VAD_ENABLED": true,
  "VAD_API_URL": "http://localhost:8080/vad",
  
  "AUDIO_CHUNK_WINDOW_S": 25.0,
  "AUDIO_CHUNK_OVERLAP_S": 0.2,
  
  "VAD_MERGE_GAP_S": 0.25,
  "VAD_MIN_SEGMENT_S": 1.0,
  "VAD_MAX_SEGMENT_S_FOR_SPLIT": 8.0
}
```

### 高精度配置（准确优先）

```json
{
  "WHISPER_MODEL_NAME": "whisper-large-v3",
  "WHISPER_LANGUAGE": "zh",
  "WHISPER_PROMPT": "只转写实际说话内容，不补充未说出的词语",
  
  "VAD_SILERO_THRESHOLD": 0.45,
  "VAD_SILERO_MIN_SILENCE_MS": 250,
  "VAD_MERGE_GAP_S": 0.20,
  "VAD_MIN_SEGMENT_S": 1.2,
  
  "SUBTITLE_NORMALIZE_PUNCTUATION": true,
  "SUBTITLE_FILTER_FILLER_WORDS": true,
  "WHISPER_MAX_RETRIES": 5
}
```

### 快速配置（性能优先）

```json
{
  "WHISPER_MODEL_NAME": "whisper-small",
  "WHISPER_MAX_WORKERS": 4,
  
  "AUDIO_CHUNK_WINDOW_S": 30.0,
  "VAD_SILERO_THRESHOLD": 0.55,
  "VAD_MIN_SEGMENT_S": 0.8,
  "VAD_MAX_SEGMENT_S_FOR_SPLIT": 10.0,
  
  "WHISPER_MAX_RETRIES": 2,
  "WHISPER_RETRY_DELAY_S": 1.0
}
```

## 常见问题与解决方案

### 1. 字幕切分过碎
**症状**：字幕条目过多，每条只有几个字  
**解决**：增大 `VAD_MERGE_GAP_S` 和 `VAD_MIN_SEGMENT_S`

### 2. 字幕太长难以阅读
**症状**：单条字幕持续 10+ 秒  
**解决**：减小 `VAD_MAX_SEGMENT_S_FOR_SPLIT`

### 3. VAD 在嘈杂环境误判
**症状**：背景噪音被识别为语音  
**解决**：提高 `VAD_SILERO_THRESHOLD`

### 4. 转写出现幻觉（无中生有）
**症状**：生成了视频中没有的内容  
**解决**：设置明确的语言和提示

```json
{
  "WHISPER_LANGUAGE": "zh",
  "WHISPER_PROMPT": "只转写实际说话内容，不补充未说出的词语"
}
```

### 5. 长视频超时失败
**症状**：处理长视频时 API 超时  
**解决**：启用分片处理

```json
{
  "AUDIO_CHUNK_WINDOW_S": 25.0,
  "AUDIO_CHUNK_OVERLAP_S": 0.2,
  "WHISPER_FALLBACK_TO_FIXED_CHUNKS": true
}
```

## 参考资料

- [Whisper API 文档](https://platform.openai.com/docs/guides/speech-to-text)
- [LocalAI 文档](https://localai.io/)
- [Silero VAD](https://github.com/snakers4/silero-vad)
