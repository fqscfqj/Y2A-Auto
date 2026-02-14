<div align="center">

# Y2A-Auto

把 YouTube 视频搬运到 AcFun 的自动化工具

[![License](https://img.shields.io/badge/license-GPL%20v3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-green.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-supported-blue.svg)](https://www.docker.com/)

从下载、翻译字幕、内容审核、智能打标签，到分区推荐与上传，全流程自动化；附带 Web 管理界面与 YouTube 监控功能。

[快速开始](#快速开始) · [功能特性](#功能特性) · [部署与运行](#部署与运行) · [配置说明](#配置说明) · [使用指南](#使用指南) · [常见问题](#常见问题)

---

</div>

<p align="center">
  <a href="https://t.me/Y2AAuto_bot" target="_blank">
    <img src="https://img.shields.io/badge/Telegram%20Bot-%40Y2AAuto__bot-2CA5E0?logo=telegram&logoColor=white" alt="Telegram Bot" />
  </a>
  <br/>
  <strong>📣 Telegram 转发机器人（试用）：</strong>
  <a href="https://t.me/Y2AAuto_bot">@Y2AAuto_bot</a>
  <br/>
  <sub>自部署版本：<a href="https://github.com/fqscfqj/Y2A-Auto-tgbot">Y2A-Auto-tgbot</a></sub>
</p>

## 功能特性

- 一条龙自动化
  - yt-dlp 下载视频与封面
  - 字幕下载、AI 翻译、自动质检（QC）并可嵌入视频
  - AI 生成标题/描述与标签，推荐分区
  - 内容安全审核（阿里云 Green）
  - 上传至 AcFun（基于 acfun_upload）
- Web 管理后台
  - 任务列表、人工审核、强制上传
  - 设置中心（开关自动模式、并发、代理、字幕等）
  - 登录保护与暴力破解锁定
- YouTube 监控
  - 频道/趋势抓取（需配置 API Key）
  - 定时任务与历史记录
- 视频编码
- Docker 一键部署，或本地运行

## 项目结构

```text
Y2A-Auto/
├─ app.py                         # Flask Web 入口
├─ requirements.txt               # 依赖列表
├─ Dockerfile                     # Docker 构建
├─ docker-compose.yml             # 生产/拉取镜像运行
├─ docker-compose-build.yml       # 本地构建镜像运行
├─ Makefile                       # 常用 Docker 管理命令
├─ README.md                      # 项目说明（此文件）
├─ LICENSE                        # 许可证
├─ acfunid/                       # AcFun 分区映射
│  └─ id_mapping.json
├─ build-tools/                   # 打包/构建相关脚本
│  ├─ build_exe.py
│  ├─ build.bat
│  ├─ README.md
│  └─ setup_app.py
├─ config/                        # 应用配置（首次运行生成）
│  └─ config.json
├─ cookies/                       # Cookie（需自行准备）
│  ├─ ac_cookies.txt
│  └─ yt_cookies.txt
├─ db/                            # SQLite 数据库与持久化数据
├─ downloads/                     # 任务产物（每任务一个子目录）
├─ ffmpeg/                        # 仓库内置 Windows/Linux ffmpeg，可按需替换
├─ fonts/                         # 字体（供字幕嵌入使用）
├─ logs/                          # 运行与任务日志
├─ modules/                       # 核心后端模块（应用逻辑）
│  ├─ __init__.py
│  ├─ acfun_uploader.py
│  ├─ ai_enhancer.py
│  ├─ config_manager.py
│  ├─ content_moderator.py
│  ├─ speech_recognition.py
│  ├─ subtitle_translator.py
│  ├─ subtitle_qc.py              # 字幕质检（可选）
│  ├─ task_manager.py
│  ├─ youtube_handler.py
│  ├─ youtube_monitor.py
│  └─ utils.py
├─ static/                        # 前端静态资源（CSS/JS/图标/第三方库）
│  ├─ css/
│  │  └─ style.css
│  ├─ img/
│  ├─ js/
│  │  └─ main.js
│  └─ lib/
│     └─ bootstrap/
│        ├─ bootstrap.bundle.min.js
│        ├─ bootstrap.min.css
│        └─ jquery.min.js
│     └─ icons/
│        └─ bootstrap-icons.css
├─ temp/                          # 临时文件与中间产物
└─ templates/                     # Jinja2 模板
  ├─ base.html
  ├─ edit_task.html
  ├─ index.html
  ├─ login.html
  ├─ manual_review.html
  ├─ settings.html
  ├─ tasks.html
  ├─ youtube_monitor_config.html
  ├─ youtube_monitor_history.html
  └─ youtube_monitor.html
```

## 快速开始

推荐使用 Docker（无需本地安装 Python/FFmpeg/yt-dlp）：

1. 准备 Cookie（重要）

- 创建 `cookies/yt_cookies.txt`（YouTube 登录 Cookie）
- 创建 `cookies/ac_cookies.txt`（AcFun 登录 Cookie）
- 可用浏览器扩展导出 Cookie（例如「Get cookies.txt」）；注意保护隐私，避免提交到仓库。

1. 启动服务

- 安装好 Docker 与 Docker Compose 后，在项目根目录执行：

```bash
docker compose up -d
```

1. 打开 Web 界面

- 浏览器访问：[http://localhost:5000](http://localhost:5000)
- 首次进入可在「设置」里开启登录保护并设置密码、开启自动模式等。

目录 `config/db/downloads/logs/temp/cookies` 会被挂载到容器，数据持久化保存。

## 部署与运行

### 方案 A：Docker 运行（推荐）

- 使用预构建镜像：`docker-compose.yml` 已配置好端口与挂载目录
- 关闭/重启/查看日志：
  - 关闭：`docker compose down`
  - 重启：`docker compose restart`
  - 日志：`docker compose logs -f`

### 方案 B：本地运行（Windows/macOS/Linux）

前置依赖：

- Python 3.11+
- FFmpeg（仓库已附带 Windows/Linux 版本，可直接使用）
- yt-dlp（`pip install yt-dlp`）

步骤：

```powershell
# 1) 创建并启用虚拟环境（Windows PowerShell）
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2) 安装依赖
pip install -r requirements.txt

# 3) 运行
python app.py
```

访问 [http://127.0.0.1:5000](http://127.0.0.1:5000) 打开 Web 界面。

## 配置说明

应用首次运行会在 `config/config.json` 生成配置文件；你也可以手动编辑。常用项：

```json
{
  "AUTO_MODE_ENABLED": true,
  "password_protection_enabled": true,
  "password": "建议自行设置",

  "YOUTUBE_COOKIES_PATH": "cookies/yt_cookies.txt",
  "ACFUN_COOKIES_PATH": "cookies/ac_cookies.txt",

  "OPENAI_API_KEY": "可选：用于标题/描述/标签、字幕翻译与字幕质检",
  "OPENAI_BASE_URL": "https://api.openai.com/v1",
  "OPENAI_MODEL_NAME": "gpt-3.5-turbo",

  "SUBTITLE_TRANSLATION_ENABLED": true,
  "SUBTITLE_TARGET_LANGUAGE": "zh",

  "SUBTITLE_QC_ENABLED": false,
  "SUBTITLE_QC_THRESHOLD": 0.6,
  "SUBTITLE_QC_SAMPLE_MAX_ITEMS": 80,

  "YOUTUBE_API_KEY": "可选：启用 YouTube 监控",

  "VIDEO_ENCODER": "auto",
  "VIDEO_CUSTOM_PARAMS_ENABLED": false,
  "VIDEO_CUSTOM_PARAMS": ""
}
### 字幕质检（QC）配置

若启用字幕质检（`SUBTITLE_QC_ENABLED: true`），系统会在字幕生成/翻译后自动抽样送 LLM 复核：

- `SUBTITLE_QC_THRESHOLD`（0-1）：通过阈值；LLM 评分低于此值则标记字幕异常
- `SUBTITLE_QC_SAMPLE_MAX_ITEMS`：抽样条目数；多抽可降低误判
- `SUBTITLE_QC_MAX_CHARS`：送检文本最大字符数；超出将截断以控制 token 消耗
- `SUBTITLE_QC_MODEL_NAME`：指定 QC 模型（留空则复用字幕翻译模型）

**QC 失败的行为**：

- 不烧录字幕，但保留字幕文件与原视频
- 继续上传原视频，任务最终标记为"完成"
- 在任务列表中显示"字幕异常"徽标，便于后续排查

场景示例：无人声视频被 ASR 误识别为大量重复句，或翻译质量极差，QC 可自动检出并跳过烧录，避免成片质量降低。

提示：

- 仅在本机安全环境中保存密钥，切勿把包含密钥的文件提交到仓库。
- 若需要代理下载 YouTube，可在设置里启用代理并填写地址/账号密码。
- 字幕 QC 需要 OpenAI API Key 与网络连接；若 API 不可用，QC 自动跳过并放行字幕

## 使用指南

1) 在首页或「任务」页，粘贴 YouTube 视频链接添加任务
2) 自动模式下会依次：下载 →（可选）转写/翻译字幕 → 生成标题/描述/标签 → 内容审核 →（可选）人工审核 → 上传到 AcFun
3) 人工审核可在「人工审核」页修改标题/描述/标签与分区，再点击「强制上传」
4) YouTube 监控：在界面中开启并配置 API Key 后，可添加频道/关键词定时监控

目录说明：

- `downloads/` 每个任务一个子目录，包含 video.mp4、cover.jpg、metadata.json、字幕等
- `logs/` 运行日志与各任务日志（task_xxx.log）
- `db/` SQLite 数据库
- `cookies/` 存放 cookies.txt（需自行准备）

## 内置 FFmpeg

- Release 包含 `ffmpeg/` 目录，内置 Windows 版 BtbN 构建与 Linux 静态版二进制及配套许可证。
- Docker 镜像与本地构建会根据 `FFMPEG_VARIANT`（默认 `btbn`）在线拉取 [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds)。如需最小体积的纯 CPU 版本，可在构建时附加 `--build-arg FFMPEG_VARIANT=static` 回退到 johnvansickle 静态包。
- 运行时始终优先使用 `ffmpeg/` 目录中的二进制；若需要升级，可直接替换该目录并保留许可证文件。
- 预编译二进制包含 NVENC/QSV/VAAPI 等硬件编码器支持。

## GPU 硬件编码加速

本项目支持 NVIDIA、Intel、AMD 三大厂商的 GPU 硬件编码加速，可显著提升字幕嵌入（烧字）的转码速度。

### 视频转码参数配置

在设置页面的"视频转码参数"区域可配置以下参数：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `VIDEO_ENCODER` | 编码器选择：auto/cpu/nvidia/intel/amd | auto |
| `VIDEO_CUSTOM_PARAMS_ENABLED` | 是否启用自定义 FFmpeg 视频参数（覆盖默认策略） | false |
| `VIDEO_CUSTOM_PARAMS` | 自定义视频编码参数字符串 | 空 |

默认情况下系统使用固定质量模式（CRF/CQ），并按分辨率自动设置质量值：

- 4K (2160p+): `22.5`
- 2K (1440p): `23.0`
- 1080p: `23.5`（基准）
- 720p: `24.5`
- <720p: `25.5`

### Docker 环境 GPU 配置

根据您的显卡类型，编辑 `docker-compose.yml` 并取消对应配置的注释：

#### NVIDIA GPU

需要安装 [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)：

```yaml
gpus: all
environment:
  - NVIDIA_VISIBLE_DEVICES=all
  - NVIDIA_DRIVER_CAPABILITIES=compute,video,utility
runtime: nvidia
```

> 说明：`deploy.resources.reservations.devices` 仅在 Docker Swarm（`docker stack deploy`）下生效；
> 普通 `docker compose up` 请使用上面的 `gpus` 写法。

#### Intel GPU (QSV)

```yaml
devices:
  - /dev/dri:/dev/dri
group_add:
  - video
  - render
```

#### AMD GPU (VAAPI)

```yaml
devices:
  - /dev/dri:/dev/dri
group_add:
  - video
  - render
```

### Windows 本地环境

Windows 用户只需确保安装了对应显卡的最新驱动，程序会自动检测并使用可用的硬件编码器：

- **NVIDIA**：安装 GeForce/Studio 驱动即可
- **Intel**：安装 Intel Graphics 驱动及 Intel Media SDK
- **AMD**：安装 Radeon Software 驱动

### 编码器说明

| 显卡厂商 | 编码器 | 平台 |
|----------|--------|------|
| NVIDIA | h264_nvenc | Windows/Linux |
| Intel | h264_qsv | Windows/Linux |
| AMD | h264_amf | Windows |
| AMD | h264_vaapi | Linux |

如果指定的硬件编码器不可用，系统会自动回退到 CPU 软编码（libx264）。

### 自定义镜像（可选）

如果你希望完全控制 FFmpeg 版本，仍可以参考以下模式自定义镜像（示例为纯 CPU 方案）：

```dockerfile
FROM jrottenberg/ffmpeg:6.1-slim AS ffmpeg

FROM python:3.11-slim
WORKDIR /app

COPY --from=ffmpeg /usr/local /usr/local
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "app.py"]
```

构建完自定义镜像后无需挂载 GPU 设备；也可以在默认 Dockerfile 构建时追加 `--build-arg FFMPEG_VARIANT=static` 获得体积更小的纯 CPU 版本。

## 常见问题

- 403 / 需要登录 / not a bot 等错误
  - 通常是 YouTube 反爬或权限问题。请更新 `cookies/yt_cookies.txt`（确保包含有效的 `youtube.com` 登录状态）。
- 找不到 FFmpeg / yt-dlp
  - Docker 用户无需关心；本地运行请确保两者在 PATH 中或通过 `pip install yt-dlp` 安装，并单独安装 FFmpeg。
- 上传到 AcFun 失败
  - 请更新 `cookies/ac_cookies.txt`，并在「人工审核」页确认分区、标题与描述合规。
- 字幕翻译速度慢
  - 可在设置中调大并发与批大小（注意 API 限速）。
- Docker 里没有走 NVENC（日志提示“未检测到 NVENC，回退 CPU”）
  - 先确认 compose 中已启用 `gpus: all` 与 `NVIDIA_DRIVER_CAPABILITIES=compute,video,utility`
  - 主机需安装 `nvidia-container-toolkit`，重启 Docker 后再执行 `docker compose up -d --force-recreate`
  - 容器内可用 `ffmpeg -hide_banner -encoders | grep nvenc` 与 `nvidia-smi` 进行检查

## 贡献与反馈

- 欢迎提交 Issue/PR：问题反馈、功能建议都很棒 → [Issues](../../issues)
- 提交前请避免包含个人 Cookie、密钥等敏感信息。

## 致谢

- [acfun_upload](https://github.com/Aruelius/acfun_upload)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [FFmpeg](https://ffmpeg.org/)
- [Flask](https://flask.palletsprojects.com/)
- [OpenAI](https://openai.com/)

特别感谢 [@Aruelius](https://github.com/Aruelius) 的 acfun_upload 项目为上传实现提供了重要参考。

## 许可证与声明

本项目基于 [GNU GPL v3](LICENSE) 开源。请遵守各平台服务条款，仅在合规前提下用于学习与研究。

---

如果对你有帮助，欢迎 Star 支持 ✨
