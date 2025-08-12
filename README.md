<div align="center">

# Y2A-Auto

**YouTube to AcFun 自动化工具**

*从 YouTube 搬运视频到 AcFun，支持 AI 翻译、字幕处理、内容审核、智能标签生成、YouTube 监控*

[![License](https://img.shields.io/badge/license-GPL%20v3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-green.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-supported-blue.svg)](https://www.docker.com/)

[快速开始](#快速开始) • [功能特性](#功能特性) • [部署方式](#部署方式) • [使用指南](#使用指南) • [浏览器插件](#浏览器插件) • [常见问题](#常见问题)

---

</div>

<p align="center">
  <a href="https://t.me/Y2AAuto_bot" target="_blank">
    <img src="https://img.shields.io/badge/Telegram%20Bot-%40Y2AAuto__bot-2CA5E0?logo=telegram&logoColor=white" alt="Telegram Bot" />
  </a>
  <br/>
  <strong>📣 Telegram 转发机器人已上线：</strong>
  <a href="https://t.me/Y2AAuto_bot">@Y2AAuto_bot</a>
  <br/>
  <sub>可先行测试，但可能随时关停或不稳定。可自行部署：
    <a href="https://github.com/fqscfqj/Y2A-Auto-tgbot">Y2A-Auto-tgbot</a>
  </sub>
</p>

## 项目简介

Y2A-Auto 是基于 Flask 的 YouTube 到 AcFun 视频搬运工具，提供完整的自动化处理流程。

### 主要功能

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

#### Windows 可执行文件（推荐）

**最简单的部署方式，无需配置环境**

```bash
# 1. 下载源码
git clone https://github.com/fqscfqj/Y2A-Auto.git
cd Y2A-Auto

# 2. 一键构建exe文件（需要Python环境）
cd build-tools
build.bat

# 3. 启动程序
cd ../dist/Y2A-Auto
start.bat
```

**特点**：
- ✅ 无需复杂环境配置
- ✅ 自动下载 FFmpeg
- ✅ 便携式部署，可直接拷贝使用
- ✅ 包含完整的目录结构
- ✅ 双击即可启动

#### Docker 部署

```bash
git clone https://github.com/fqscfqj/Y2A-Auto.git
cd Y2A-Auto
docker-compose up -d
```

#### 本地构建

```bash
docker-compose -f docker-compose-build.yml up -d --build
```

### 首次配置

1. 访问 Web 界面: <http://localhost:5000>
2. 配置 API 密钥和账号信息
3. 上传 Cookie 文件或设置登录凭据
4. 配置字幕翻译选项
5. 设置 YouTube 监控规则

## 部署方式

<details>
<summary>Windows EXE 部署（推荐）</summary>

### 系统要求

- Windows 10/11 (64位)
- 至少 2GB 可用内存
- 至少 5GB 可用磁盘空间
- 网络连接

### 一键构建

```bash
# 1. 确保已安装 Python 3.8+
python --version

# 2. 下载项目
git clone https://github.com/fqscfqj/Y2A-Auto.git
cd Y2A-Auto

# 3. 双击运行构建脚本
cd build-tools
build.bat
```

### 手动构建

```bash
# 安装构建依赖
pip install pyinstaller requests

# 运行构建脚本
python build-tools/build_exe.py
```

### 构建产物

构建完成后在 `dist/Y2A-Auto/` 目录下包含：

```
dist/Y2A-Auto/
├── Y2A-Auto.exe          # 主程序
├── start.bat             # 启动脚本
├── README.txt            # 使用说明
├── ffmpeg/               # 视频处理工具
│   ├── ffmpeg.exe
│   ├── ffprobe.exe
│   └── ffplay.exe
├── config/               # 配置文件目录
├── db/                   # 数据库文件目录
├── downloads/            # 下载文件目录
├── logs/                 # 日志文件目录
├── cookies/              # Cookie文件目录
└── temp/                 # 临时文件目录
```

### 使用方法

1. 双击 `start.bat` 启动程序
2. 浏览器访问 <http://localhost:5000>
3. 按照首次配置步骤进行设置

### 特点

- ✅ **零依赖**：无需安装 Python、FFmpeg 等
- ✅ **便携性**：整个目录可直接拷贝到其他电脑使用  
- ✅ **完整性**：包含所有必需的组件和工具
- ✅ **易用性**：双击即可启动，无需命令行操作

</details>

<details>
<summary>Docker 部署</summary>

### 快速开始

#### 预构建镜像

```bash
git clone https://github.com/fqscfqj/Y2A-Auto.git
cd Y2A-Auto
docker-compose up -d
```

#### 本地构建

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

### 配置文件和数据管理

#### 初始配置文件

首次运行时，系统会自动创建默认配置文件，包含所有功能的基础设置。

#### 数据持久化

所有数据存储在对应目录中：

- `config/` - 配置文件
- `db/` - SQLite 数据库
- `cookies/` - 认证 Cookie 文件
- `downloads/` - 下载的视频文件
- `logs/` - 系统日志

## 浏览器插件

为了提升使用体验，Y2A-Auto 提供了浏览器扩展和用户脚本：

### 🔥 Y2A-Auto 浏览器扩展（推荐）

完整的浏览器扩展，提供 Cookie 同步和视频推送功能的一体化解决方案。

**主要功能**：

- 🍪 **自动 Cookie 同步** - 访问包括 HttpOnly 在内的所有认证 Cookie
- 📤 **一键视频推送** - 在 YouTube 页面直接添加视频到处理队列
- 🔄 **后台自动同步** - 定时同步 Cookie，确保认证状态有效
- 📊 **实时状态显示** - 可视化同步状态和操作反馈
- ⚙️ **灵活配置** - 支持自定义服务器地址和同步间隔

**安装方式**：

1. **加载扩展到浏览器**
   - 打开浏览器扩展管理页面
   - 启用"开发者模式"
   - 点击"加载已解压的扩展程序"
   - 选择 `userscripts/browser-extension/` 目录

2. **配置服务器地址**
   - 方式一（推荐）：右键扩展图标 → "选项" → 在设置页面配置
   - 方式二：编辑 `background.js` 中的服务器配置
   ```javascript
   const Y2A_AUTO_SERVER = 'http://localhost:5000'; // 修改为实际地址
   ```

**技术优势**：
- ✅ 可访问 HttpOnly Cookie（用户脚本无法实现）
- ✅ 更稳定的后台运行机制
- ✅ 更好的 YouTube 页面集成
- ✅ 无需安装额外的脚本管理器

**详细使用说明**：
📄 [浏览器扩展详细文档](docs/userscripts/Browser-Extension-README.md)

### 📤 YouTube 视频推送脚本

轻量级的油猴脚本，专门用于在 YouTube 页面添加视频推送功能。

**主要功能**：

- ✅ 页面按钮集成
- ✅ 一键视频推送
- ✅ 实时状态反馈
- ✅ 自适应 YouTube 界面

**安装和使用**：
📄 [详细使用说明](docs/userscripts/PushTo-README.md)

### 安装指南

#### 方式一：浏览器扩展（推荐）

直接使用 `userscripts/browser-extension/` 目录中的完整扩展。

#### 方式二：用户脚本

1. **安装 Tampermonkey 扩展**
   - [Chrome](https://chrome.google.com/webstore/detail/tampermonkey/dhdgffkkebhmkfjojejmpbldmpobfkfo)
   - [Firefox](https://addons.mozilla.org/en-US/firefox/addon/tampermonkey/)
   - [Edge](https://microsoftedge.microsoft.com/addons/detail/tampermonkey/iikmkjmpaadaobahmlepeloendndfphd)

2. **安装推送脚本**
   - 复制 `userscripts/PushToY2AAuto.user.js` 内容
   - 在 Tampermonkey 中创建新脚本并粘贴

3. **配置服务器地址**

   ```javascript
   const Y2A_AUTO_SERVER = 'http://localhost:5000'; // 修改为实际地址
   ```

更多详细信息请查看：📚 [脚本使用文档](userscripts/README.md)

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

#### 方法一：浏览器插件

1. 安装 "Get cookies.txt" 插件
2. 登录 AcFun/YouTube
3. 导出 Cookie 文件

#### 方法二：手动提取

1. 登录目标网站
2. 打开开发者工具 (F12)
3. 复制 Application → Cookies 中的数据

#### 文件格式

##### Netscape 格式

```
# Netscape HTTP Cookie File
.acfun.cn TRUE / FALSE 1234567890 cookie_name cookie_value
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

- **Flask 3.1.1** - Web 框架
- **yt-dlp ≥2025.6.9** - YouTube 下载器
- **OpenAI ≥1.0.0** - AI 翻译和增强
- **APScheduler 3.11.0** - 任务调度
- **FFmpeg** - 视频处理
- **SQLite** - 数据存储

### 前端技术

- **Bootstrap 5** - UI 框架
- **JavaScript ES6+** - 交互逻辑

### 部署工具

- **Docker** - 容器化部署
- **Docker Compose** - 多容器编排
- **GitHub Actions** - 自动构建

## 显卡转码（GPU 加速）

Y2A-Auto 支持在嵌字转码阶段使用 GPU 加速，减少处理时间并降低 CPU 占用。可在 Web 界面 `设置 → 视频编码器` 选择：`cpu` / `nvenc` / `qsv` / `amf`。

- `cpu`: 软编码（x264/x265），兼容性最好但速度较慢
- `nvenc`: NVIDIA 显卡（Linux/Windows 均可；Docker 需特别配置）
- `qsv`: Intel Quick Sync（Linux/Windows；Docker 需映射 `/dev/dri`）
- `amf`: AMD AMF（主要在 Windows 原生运行有效；Linux Docker 通常使用 VAAPI 而非 AMF，当前未内置 VAAPI 方案）

> 提示：GPU 是否可用取决于宿主机驱动与容器内 FFmpeg 的编译选项。若在容器内执行 `ffmpeg -encoders` 未看到目标编码器（如 `h264_nvenc`/`hevc_nvenc`/`h264_qsv`/`hevc_qsv`），请参考下文的“自定义镜像（可选）”。

### Docker（NVIDIA NVENC）

前置条件：
- 已安装并正确加载 NVIDIA 驱动，宿主机可执行 `nvidia-smi`
- 已安装 NVIDIA Container Toolkit（参考官方文档）
  - 文档链接：`https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html`
  - 常用命令：
    - `sudo nvidia-ctk runtime configure --runtime=docker`
    - `sudo systemctl restart docker`

Compose 示例（方式一：基于 compose v2 的设备保留声明）

```yaml
services:
  y2a-auto:
    image: fqscfqj/y2a-auto:latest
    container_name: y2a-auto
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - ./config:/app/config
      - ./db:/app/db
      - ./downloads:/app/downloads
      - ./logs:/app/logs
      - ./cookies:/app/cookies
      - ./temp:/app/temp
    environment:
      - TZ=Asia/Shanghai
      - PYTHONIOENCODING=utf-8
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [compute, video, utility]
```

Compose 示例（方式二：兼容旧版 compose 的运行时与环境变量）

```yaml
services:
  y2a-auto:
    image: fqscfqj/y2a-auto:latest
    container_name: y2a-auto
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - ./config:/app/config
      - ./db:/app/db
      - ./downloads:/app/downloads
      - ./logs:/app/logs
      - ./cookies:/app/cookies
      - ./temp:/app/temp
    environment:
      - TZ=Asia/Shanghai
      - PYTHONIOENCODING=utf-8
      - NVIDIA_VISIBLE_DEVICES=all
      - NVIDIA_DRIVER_CAPABILITIES=compute,video,utility
    runtime: nvidia
```

容器内验证：
- `ffmpeg -hwaccels` 应包含 `cuda`
- `ffmpeg -hide_banner -encoders | grep nvenc` 应显示 `h264_nvenc/hev c_nvenc`

在 Web 界面选择 `视频编码器 = NVIDIA NVENC`（或在 `config/config.json` 中将 `VIDEO_ENCODER` 设为 `nvenc`）。

### Docker（Intel QSV）

前置条件：
- BIOS 中启用核显 / iGPU
- 宿主机安装 Intel 媒体驱动（常见为 `iHD`），并存在 `/dev/dri`

Compose 示例：

```yaml
services:
  y2a-auto:
    image: fqscfqj/y2a-auto:latest
    container_name: y2a-auto
    restart: unless-stopped
    ports:
      - "5000:5000"
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - video
      - render
    environment:
      - TZ=Asia/Shanghai
      - PYTHONIOENCODING=utf-8
      - LIBVA_DRIVER_NAME=iHD
    volumes:
      - ./config:/app/config
      - ./db:/app/db
      - ./downloads:/app/downloads
      - ./logs:/app/logs
      - ./cookies:/app/cookies
      - ./temp:/app/temp
```

容器内验证：
- `ffmpeg -hwaccels` 应包含 `qsv`/`vaapi`
- `ffmpeg -hide_banner -encoders | grep qsv` 应显示 `h264_qsv/hevc_qsv`

在 Web 界面选择 `视频编码器 = Intel QSV`（或在 `config/config.json` 中将 `VIDEO_ENCODER` 设为 `qsv`）。

### AMD AMF（说明）

- AMF 编码器（`h264_amf`/`hevc_amf`）主要在 Windows 原生环境有效。Linux 下通常通过 VAAPI，但当前应用未内置 VAAPI 编码路径，Docker 下不建议选择 `amf`。

### 自定义镜像（可选）

若容器内 FFmpeg 不包含所需硬件编码器，可自定义镜像。例如为 NVIDIA 基础环境构建：

```dockerfile
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 python3-pip ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 5000
CMD ["python3", "app.py"]
```

然后使用对应的 Compose 文件构建并运行：

```bash
docker compose -f docker-compose-build.yml up -d --build
```

> 注意：不同发行版的 `ffmpeg` 是否启用 `nvenc/qsv` 会有差异，请在容器内用 `ffmpeg -encoders` 检查。如仍缺失，请选择具备对应功能的基础镜像或采用社区提供的 FFmpeg 预编译镜像。

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
├── userscripts/                # 浏览器插件和脚本
│   ├── browser-extension/      # 浏览器扩展（推荐）
│   │   ├── manifest.json       # 扩展清单文件
│   │   ├── background.js       # 后台服务脚本
│   │   ├── content.js          # 内容脚本
│   │   ├── popup.html          # 扩展弹窗页面
│   │   ├── popup.js            # 弹窗逻辑
│   │   ├── options.html        # 扩展设置页面
│   │   ├── options.js          # 设置页面逻辑
│   │   ├── styles.css          # 样式文件
│   │   └── README.md           # 扩展使用说明
│   ├── PushToY2AAuto.user.js   # 视频推送用户脚本
│   └── README.md               # 插件使用说明
├── docs/                       # 项目文档
│   ├── userscripts/            # 插件详细文档
│   │   ├── Browser-Extension-README.md  # 浏览器扩展详细说明
│   │   └── PushTo-README.md         # 推送脚本详细说明
│   └── README.md               # 文档中心
├── templates/                  # HTML 模板
├── static/                     # 静态资源
├── config/                     # 配置文件
├── cookies/                    # Cookie 文件
├── db/                         # 数据库文件
├── downloads/                  # 下载文件
├── logs/                       # 日志文件
├── temp/                       # 临时文件
├── build-tools/                # Windows exe构建工具
│   ├── build_exe.py            # 构建脚本
│   ├── build.bat               # 一键构建批处理
│   ├── setup_app.py            # 应用启动配置
│   ├── Y2A-Auto.spec           # PyInstaller配置文件
│   └── README.md               # 构建说明文档
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

#### Q: YouTube API 配额不足怎么办？

- 合理设置监控频率
- 使用精确的搜索条件
- 申请更高的 API 配额限制

#### Q: 监控到的视频质量不符合要求？

- 调整筛选条件（观看数、点赞数等）
- 完善关键词过滤规则
- 使用频道黑白名单功能

</details>

<details>
<summary>字幕翻译相关</summary>

#### Q: 字幕翻译失败怎么办？

- 检查 OpenAI API 密钥
- 确认网络连接正常
- 检查源语言设置

#### Q: 如何提高翻译质量？

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

## 致谢

感谢以下开源项目为本项目提供的支持：

- **[acfun_upload](https://github.com/Aruelius/acfun_upload)** - 提供了AcFun上传功能的核心实现和技术参考
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** - 强大的YouTube视频下载工具
- **[FFmpeg](https://ffmpeg.org/)** - 视频处理和转码支持
- **[Flask](https://flask.palletsprojects.com/)** - 轻量级Web框架
- **[OpenAI](https://openai.com/)** - AI翻译和内容增强服务

特别感谢 [@Aruelius](https://github.com/Aruelius) 的 acfun_upload 项目，为本项目的AcFun上传功能提供了重要的技术基础和实现思路。

## 许可证

本项目基于 [GNU General Public License v3.0](LICENSE) 开源协议。

---

<div align="center">

[查看文档](README.md) • [报告问题](../../issues) • [功能建议](../../issues)

</div>
