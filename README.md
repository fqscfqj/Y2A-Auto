<div align="center">

# Y2A-Auto

**YouTube to AcFun 自动化工具**

*从 YouTube 搬运视频到 AcFun，支持 AI 翻译、字幕处理、内容审核、智能标签生成、YouTube 监控*

[![License](https://img.shields.io/badge/license-GPL%20v3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-green.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-supported-blue.svg)](https://www.docker.com/)

[快速开始](#快速开始) • [功能特性](#功能特性) • [部署方式](#部署方式) • [使用指南](#使用指南) • [常见问题](#常见问题)

---

</div>

## 项目简介

Y2A-Auto 是基于 Flask 的 YouTube 到 AcFun 视频搬运工具，提供完整的自动化处理流程。

**主要功能**
- 自动化视频下载和上传
- YouTube 频道和趋势监控
- AI 驱动的内容翻译和标签生成
- 字幕下载、翻译和嵌入
- Web 界面管理和浏览器插件支持

## 功能特性

### YouTube 监控
- 趋势视频、搜索关键词、特定频道监控
- 自定义筛选条件（观看数、点赞数、时长等）
- 定时调度和手动执行
- 关键词过滤和频道黑白名单
- 自动添加符合条件的视频到处理队列

### 视频处理
- 基于 yt-dlp 的视频下载
- 字幕自动下载、翻译和嵌入
- 封面自动处理和格式适配
- FFmpeg 视频处理和转码

### AI 增强
- OpenAI API 标题和描述翻译
- 批量字幕翻译
- 自动标签生成
- 阿里云内容安全审核
- AcFun 分区智能推荐

### 任务管理
- 完整的任务生命周期管理
- 实时状态显示和进度跟踪
- 人工审核和内容调整
- 批量操作和任务统计

### 系统特性
- Cookie 文件登录支持
- Docker 容器化部署
- 详细日志记录和管理
- Web 界面配置管理
- 浏览器插件集成

## 快速开始

### 环境要求

- Docker & Docker Compose (推荐)
- Python 3.10+ (本地部署)
- FFmpeg (Docker 镜像已包含)
- YouTube Data API v3 密钥 (监控功能需要)

### 一键部署

**使用预构建镜像**
```bash
git clone https://github.com/fqscfqj/Y2A-Auto.git
cd Y2A-Auto
docker-compose up -d
```

**本地构建**
```bash
docker-compose -f docker-compose-build.yml up -d --build
```

### 首次配置

1. 访问 Web 界面: http://localhost:5000
2. 配置 API 密钥和账号信息
3. 上传 Cookie 文件或设置登录凭据
4. 配置字幕翻译选项
5. 设置 YouTube 监控规则

## 部署方式

<details>
<summary>Docker 部署 (推荐)</summary>

### 快速开始

**预构建镜像**
```bash
git clone https://github.com/fqscfqj/Y2A-Auto.git
cd Y2A-Auto
docker-compose up -d
```

**本地构建**
```bash
docker-compose -f docker-compose-build.yml up -d --build
```

### Makefile 命令

```bash
make help        # 查看所有命令
make up          # 启动应用
make down        # 停止应用  
make logs        # 查看日志
make restart     # 重启应用
make build       # 构建镜像
make clean       # 清理资源
make health      # 健康检查
```

### 目录挂载

```yaml
volumes:
  - ./config:/app/config      # 配置文件
  - ./db:/app/db             # 数据库文件
  - ./downloads:/app/downloads # 下载文件
  - ./logs:/app/logs         # 日志文件  
  - ./cookies:/app/cookies   # Cookie文件
  - ./temp:/app/temp         # 临时文件
```

### 更新维护

```bash
# 更新到最新版本
docker-compose down
docker-compose pull
docker-compose up -d

# 备份数据
tar -czf y2a-auto-backup-$(date +%Y%m%d).tar.gz config db cookies
```

</details>

<details>
<summary>本地部署</summary>

```bash
# 克隆项目
git clone https://github.com/fqscfqj/Y2A-Auto.git
cd Y2A-Auto

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# 安装系统依赖
# Ubuntu/Debian: sudo apt-get install ffmpeg
# macOS: brew install ffmpeg
# Windows: 下载 FFmpeg 并配置环境变量

# 安装Python依赖
pip install -r requirements.txt

# 启动应用
python app.py
```
</details>

## 使用指南

### Web 界面操作

#### YouTube 监控
1. 创建监控配置，设置监控类型和条件
2. 配置筛选规则（观看数、点赞数、时长等）
3. 设置调度方式（手动或自动定时）
4. 启动监控，查看发现的视频
5. 符合条件的视频自动加入处理队列

#### 任务管理
1. 添加 YouTube 视频 URL 或通过监控自动添加
2. 启动任务处理（下载、翻译、审核）
3. 人工审核和调整 AI 生成的内容
4. 上传到 AcFun

#### 字幕处理
1. 自动下载字幕文件 (.vtt 格式)
2. 语言检测和批量翻译
3. 字幕嵌入视频文件
4. 保留原始字幕文件（可选）

### 浏览器插件

#### 安装 Tampermonkey 脚本
1. 安装 [Tampermonkey](https://www.tampermonkey.net/) 扩展
2. 导入 `PushToY2AAuto.user.js` 脚本
3. 配置服务器地址
   ```javascript
   const Y2A_AUTO_SERVER = 'http://localhost:5000';
   ```

#### 使用方法
1. 在 YouTube 视频页面点击"推送到Y2A-Auto"按钮
2. 等待推送成功通知

## 配置说明

### 主要配置项

#### YouTube 监控
```json
{
  "YOUTUBE_API_KEY": "your-youtube-data-api-v3-key",
  "YOUTUBE_MONITOR_ENABLED": true,
  "YOUTUBE_MONITOR_DEFAULT_REGION": "US",
  "YOUTUBE_MONITOR_DEFAULT_MAX_RESULTS": 10
}
```

#### AI 配置
```json
{
  "OPENAI_API_KEY": "your-openai-api-key",
  "OPENAI_BASE_URL": "https://api.openai.com/v1",
  "OPENAI_MODEL_NAME": "gpt-3.5-turbo",
  "TRANSLATE_TITLE": true,
  "TRANSLATE_DESCRIPTION": true,
  "GENERATE_TAGS": true
}
```

#### 字幕翻译
```json
{
  "SUBTITLE_TRANSLATION_ENABLED": true,
  "SUBTITLE_SOURCE_LANGUAGE": "auto",
  "SUBTITLE_TARGET_LANGUAGE": "zh",
  "SUBTITLE_BATCH_SIZE": 5,
  "SUBTITLE_MAX_WORKERS": 3,
  "SUBTITLE_EMBED_IN_VIDEO": true
}
```

#### 内容审核
```json
{
  "CONTENT_MODERATION_ENABLED": true,
  "ALIYUN_ACCESS_KEY_ID": "your-access-key-id",
  "ALIYUN_ACCESS_KEY_SECRET": "your-access-key-secret"
}
```

### API 密钥获取

#### YouTube Data API v3
1. 访问 [Google Cloud Console](https://console.cloud.google.com/)
2. 创建项目并启用 "YouTube Data API v3"
3. 创建 API 密钥

#### OpenAI API
1. 访问 [OpenAI Platform](https://platform.openai.com/)
2. 注册账号并生成 API Key
3. 确保账户有足够余额

### Cookie 配置

#### 获取 Cookie 文件

**方法一：浏览器插件**
1. 安装 "Get cookies.txt" 插件
2. 登录 AcFun/YouTube
3. 导出 Cookie 文件

**方法二：手动提取**
1. 登录目标网站
2. 打开开发者工具 (F12)
3. 复制 Application → Cookies 中的数据

#### 文件格式

**Netscape 格式**
```
# Netscape HTTP Cookie File
.acfun.cn	TRUE	/	FALSE	1234567890	cookie_name	cookie_value
```

#### 配置方式
- Web 界面上传（推荐）
- 手动放置到 `cookies/` 目录

## 系统架构

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   YouTube       │    │   Y2A-Auto      │    │   AcFun         │
│                 │    │                 │    │                 │
│   视频源         │─▶─│   下载处理       │─▶─│   自动发布       │
│   视频链接       │    │   字幕翻译       │    │   数据统计       │
│   字幕文件       │    │   AI 增强        │    └─────────────────┘
│   API 监控       │    │   人工审核       │
└─────────────────┘    │   任务管理       │
                       │   智能监控       │
                       └─────────────────┘
                               │
                       ┌─────────────────┐
                       │   Flask Web     │
                       │   浏览器插件     │
                       │   定时任务       │
                       └─────────────────┘
```

## 技术栈

### 后端技术
- **Flask 2.3.3** - Web 框架
- **yt-dlp ≥2025.5.22** - YouTube 下载器
- **OpenAI ≥1.0.0** - AI 翻译和增强
- **APScheduler 3.10.1** - 任务调度
- **FFmpeg** - 视频处理
- **SQLite** - 数据存储

### 前端技术
- **Bootstrap 5** - UI 框架
- **JavaScript ES6+** - 交互逻辑

### 部署工具
- **Docker** - 容器化部署
- **Docker Compose** - 多容器编排
- **GitHub Actions** - 自动构建

## 项目结构

```
Y2A-Auto/
├── modules/                    # 核心功能模块
│   ├── youtube_handler.py      # YouTube 视频处理
│   ├── youtube_monitor.py      # YouTube 监控系统
│   ├── acfun_uploader.py       # AcFun 上传器
│   ├── subtitle_translator.py  # 字幕翻译器
│   ├── ai_enhancer.py          # AI 增强功能
│   ├── content_moderator.py    # 内容审核
│   ├── task_manager.py         # 任务管理器
│   └── config_manager.py       # 配置管理
├── templates/                  # HTML 模板
├── static/                     # 静态资源
├── config/                     # 配置文件
├── cookies/                    # Cookie 文件
├── db/                         # 数据库文件
├── downloads/                  # 下载文件
├── logs/                       # 日志文件
├── docker-compose.yml          # Docker 配置
├── Dockerfile                  # Docker 镜像
├── app.py                      # Flask 主应用
└── requirements.txt            # Python 依赖
```

## API 接口

### YouTube 监控
- `GET /youtube_monitor` - 监控主页面
- `POST /youtube_monitor/config` - 创建监控配置
- `POST /youtube_monitor/run/{id}` - 手动执行监控

### 任务管理
- `POST /tasks/add` - 添加新任务
- `POST /tasks/{id}/start` - 启动任务
- `POST /tasks/{id}/force_upload` - 强制上传
- `POST /tasks/add_via_extension` - 浏览器插件推送

### 系统管理
- `GET/POST /settings` - 系统配置管理
- `POST /maintenance/cleanup_logs` - 清理日志

## 更新日志

### 最新修复
- **任务状态显示优化**: 修复了在字幕翻译过程中任务状态显示不准确的问题
  - 字幕翻译完成后正确显示"上传中"状态
  - 改善了用户体验，提供更准确的进度反馈

## 常见问题

<details>
<summary>YouTube 监控相关</summary>

**Q: YouTube API 配额不足怎么办？**
- 合理设置监控频率
- 使用精确的搜索条件
- 申请更高的 API 配额限制

**Q: 监控到的视频质量不符合要求？**
- 调整筛选条件（观看数、点赞数等）
- 完善关键词过滤规则
- 使用频道黑白名单功能
</details>

<details>
<summary>字幕翻译相关</summary>

**Q: 字幕翻译失败怎么办？**
- 检查 OpenAI API 密钥
- 确认网络连接正常
- 检查源语言设置

**Q: 如何提高翻译质量？**
- 使用更高级的模型 (如 gpt-4)
- 调整批次大小
- 确保源语言设置准确
</details>

<details>
<summary>Cookie 过期问题</summary>

**解决方案**:
- 重新获取并上传 Cookie 文件
- 使用备用的用户名密码登录
- 定期更新 Cookie 文件
</details>

<details>
<summary>上传失败问题</summary>

**检查清单**:
1. Cookie 文件格式是否正确
2. 网络连接是否正常  
3. 视频文件是否完整
4. AcFun 服务器是否正常
</details>

<details>
<summary>Docker 部署问题</summary>

**排查步骤**:
1. 检查 Docker 版本
2. 确认端口 5000 未被占用
3. 查看容器日志: `docker-compose logs -f`
4. 重新构建: `docker-compose up -d --build`
</details>

## 安全提示

- 保护 API 密钥和 Cookie 文件
- 定期更新登录凭据
- 在受信任的网络环境中使用
- 定期清理日志文件
- 合理使用 API 配额

## 许可证

本项目基于 [GNU General Public License v3.0](LICENSE) 开源协议。

---

<div align="center">

[查看文档](README.md) • [报告问题](../../issues) • [功能建议](../../issues)

</div>