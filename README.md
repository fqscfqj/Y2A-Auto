<div align="center">

# Y2A-Auto

把 YouTube 视频搬运到 AcFun 的自动化工具

[![License](https://img.shields.io/badge/license-GPL%20v3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-green.svg)](https://www.python.org/)
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
  - 字幕下载、AI 翻译并可嵌入视频
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
- 可选 GPU/硬件加速
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
├─ acfunid/                       # AcFun 分区映射
│  └─ id_mapping.json
├─ modules/                       # 核心后端模块
│  ├─ youtube_handler.py          # YouTube 下载/元数据/封面/字幕
│  ├─ youtube_monitor.py          # YouTube 监控与定时任务
│  ├─ acfun_uploader.py           # AcFun 上传
│  ├─ subtitle_translator.py      # 字幕翻译与嵌入
│  ├─ ai_enhancer.py              # 标题/描述/标签 AI 生成
│  ├─ content_moderator.py        # 内容审核
│  ├─ speech_recognition.py       # 语音转写（Whisper/OpenAI 兼容）
│  ├─ task_manager.py             # 任务编排、并发与转码
│  ├─ config_manager.py           # 配置读写与默认项
│  └─ utils.py                    # 工具函数
├─ templates/                     # 前端页面（Jinja2）
├─ static/                        # 前端静态资源（CSS/JS/图片）
├─ config/                        # 应用配置（首次运行生成）
│  └─ config.json
├─ cookies/                       # Cookie（自备：yt_cookies.txt、ac_cookies.txt）
├─ db/                            # SQLite 数据库
├─ downloads/                     # 任务产物（视频/封面/字幕/元数据）
├─ logs/                          # 运行与任务日志（task_xxx.log）
├─ fonts/                         # 字幕字体（思源黑体变体）
├─ temp/                          # 临时目录
└─ build-tools/                   # 打包相关脚本
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

- Python 3.10+
- FFmpeg（命令行可执行）
- yt-dlp（`pip install yt-dlp`）

步骤：

```powershell
# 1) 创建并启用虚拟环境（Windows PowerShell）
py -3.10 -m venv .venv
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

  "OPENAI_API_KEY": "可选：用于标题/描述/标签与字幕翻译",
  "OPENAI_BASE_URL": "https://api.openai.com/v1",
  "OPENAI_MODEL_NAME": "gpt-3.5-turbo",

  "SUBTITLE_TRANSLATION_ENABLED": true,
  "SUBTITLE_TARGET_LANGUAGE": "zh",

  "YOUTUBE_API_KEY": "可选：启用 YouTube 监控",

  "VIDEO_ENCODER": "cpu"  // 也可 nvenc/qsv/amf
}
```

提示：

- 仅在本机安全环境中保存密钥，切勿把包含密钥的文件提交到仓库。
- 若需要代理下载 YouTube，可在设置里启用代理并填写地址/账号密码。
- Windows/NVIDIA 用户可将 `VIDEO_ENCODER` 设为 `nvenc` 获得更快的嵌字/转码。

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

## 嵌字转码参数与硬件加速

仅当在设置中勾选“将字幕嵌入视频”时，本段所述的转码参数才会生效。应用会根据 `VIDEO_ENCODER` 选择编码器并使用统一参数：

- CPU：libx264，CRF 18，preset=slow，profile=high，level=4.2，yuv420p
- NVIDIA NVENC：hevc_nvenc，preset=p6，cq=20，rc-lookahead=32；若源为 10bit，自动使用 profile=main10 并输出 p010le，否则 profile=main + yuv420p
- 音频：AAC 320kbps，采样率跟随原视频

提示：NVENC/QSV/AMF 取决于系统与 ffmpeg 的编译是否包含对应硬编支持；不可用时会自动回退到 CPU。

## 硬件转码（Docker）

应用支持通过 `VIDEO_ENCODER` 选择编码器：`cpu`（默认）/ `nvenc`（NVIDIA）/ `qsv`（Intel）。注意：容器内需有“包含对应硬件编码器的 ffmpeg”。默认镜像为发行版 ffmpeg，通常不含 NVENC/QSV；若需硬件转码，请按下述方案：

- 使用自定义镜像引入已启用 NVENC/QSV 的 ffmpeg
- 或改用已包含硬件编码器的 ffmpeg 基础镜像

### NVIDIA NVENC（Linux 宿主机）

前提：安装 NVIDIA 驱动与 NVIDIA Container Toolkit。

docker-compose 关键配置示例：

```yaml
services:
  y2a-auto:
    image: fqscfqj/y2a-auto:latest
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
              capabilities: [gpu]
```

并在应用设置或 `config/config.json` 中设置：

```json
{"VIDEO_ENCODER": "nvenc"}
```

可选自检（容器内）：

```bash
ffmpeg -hide_banner -encoders | grep -i nvenc
```

### Intel QSV（Linux 宿主机）

前提：宿主机启用 iGPU，驱动正常；容器映射 `/dev/dri`。

docker-compose 关键配置示例：

```yaml
services:
  y2a-auto:
    image: fqscfqj/y2a-auto:latest
    devices:
      - /dev/dri:/dev/dri
    environment:
      - LIBVA_DRIVER_NAME=iHD
      - TZ=Asia/Shanghai
      - PYTHONIOENCODING=utf-8
```

并在应用设置或 `config/config.json` 中设置：

```json
{"VIDEO_ENCODER": "qsv"}
```

可选自检（容器内）：

```bash
ffmpeg -hide_banner -encoders | grep -i qsv
```

### 自定义镜像内置硬件编码 ffmpeg（示例）

若默认镜像缺少硬件编码器，可在自定义镜像中引入已编译好的 ffmpeg，例如基于 `jrottenberg/ffmpeg`（示意）：

```dockerfile
FROM jrottenberg/ffmpeg:6.1-nvidia AS ffmpeg

FROM python:3.10-slim
WORKDIR /app

# 拷贝 ffmpeg 到运行镜像
COPY --from=ffmpeg /usr/local /usr/local

# 安装依赖与应用
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "app.py"]
```

构建完成后，按前述 NVENC/QSV 的 compose 示例分配设备即可。

提示：容器内 ffmpeg 的编码器可用性以 `ffmpeg -encoders` 为准；若不可用，请更换镜像或自行编译。

## 常见问题

- 403 / 需要登录 / not a bot 等错误
  - 通常是 YouTube 反爬或权限问题。请更新 `cookies/yt_cookies.txt`（确保包含有效的 `youtube.com` 登录状态）。
- 找不到 FFmpeg / yt-dlp
  - Docker 用户无需关心；本地运行请确保两者在 PATH 中或通过 `pip install yt-dlp` 安装，并单独安装 FFmpeg。
- 上传到 AcFun 失败
  - 请更新 `cookies/ac_cookies.txt`，并在「人工审核」页确认分区、标题与描述合规。
- 字幕翻译速度慢
  - 可在设置中调大并发与批大小（注意 API 限速），或使用硬件编码器加速视频处理。

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
