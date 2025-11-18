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
- 需要硬件编码时，请先在 README 的“硬件转码指南”章节确认驱动/容器挂载是否已就绪，再在设置页选择 `VIDEO_ENCODER`。

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

- 仓库自带 `ffmpeg/` 目录，其中包含：
  - `ffmpeg.exe` / `ffprobe.exe`：Windows 64 位版本（来自 BtbN Builds）。
  - `ffmpeg` / `ffprobe`：Linux/amd64 静态版本（来自 johnvansickle.com）。
  - `FFMPEG_GPLv3.txt` 与 `FFMPEG_README.txt`：对应的许可证与上游说明。
- 本地运行、Docker 镜像以及 Windows 打包版本都会优先使用该目录，无需首次启动时在线下载。
- 若需要升级 FFmpeg，请将新的二进制文件覆盖到 `ffmpeg/` 目录，并保留相应的许可证文件；Docker 镜像与打包脚本会自动随仓库内容更新。
- 预编译二进制已启用 NVENC / QSV / AMF / VA-API / libx264 等常用编码器（以 `ffmpeg -hide_banner -encoders` 输出为准）。GPU 能否成功加速仍取决于宿主机驱动或容器是否正确挂载对应设备。

## 嵌字转码参数与硬件加速

仅当在设置中勾选“将字幕嵌入视频”时，本段所述的转码参数才会生效。应用会根据 `VIDEO_ENCODER` 选择编码器并使用统一参数：

- CPU：libx264，CRF 23，preset=slow，profile=high，level=4.2，yuv420p
- NVIDIA NVENC：hevc_nvenc，preset=p6，cq=25，rc-lookahead=32；若源为 10bit，自动使用 profile=main10 并输出 p010le，否则 profile=main + yuv420p
- 音频：AAC 320kbps，采样率跟随原视频

提示：NVENC/QSV/AMF 的可用性仍取决于系统驱动、容器所挂设备以及硬件型号；不可用时应用会自动回退到 CPU 并在日志中给出提示。

## 硬件转码指南

应用通过 `VIDEO_ENCODER` 控制所使用的编码器：`cpu` / `nvenc` / `qsv` / `amf`。项目内置的 FFmpeg 已包含这些编码器，额外需要做的是：

1. 宿主机或容器必须能访问相应的 GPU 设备。
2. 设备驱动/运行时需已正确安装（NVIDIA 驱动 + Container Toolkit、Intel VAAPI/QSV 驱动、AMD Adrenalin/ROCm 等）。
3. 在设置页选择编码器，或在 `config/config.json` 写入 `"VIDEO_ENCODER": "nvenc"` 等配置。

### Windows / 裸机 Linux

- Windows 版本直接使用 `ffmpeg/ffmpeg.exe`。只要显卡驱动支持 NVENC/QSV/AMF，对应选项即可生效。
- Linux 裸机运行（非容器）时，同样使用仓库 `ffmpeg/ffmpeg`，需要确保用户对 `/dev/dri`（QSV/VA-API）或 NVIDIA 设备节点有访问权限。
- 自检命令：

```powershell
# Windows PowerShell
.fmpeg\ffmpeg.exe -hide_banner -encoders ^| Select-String nvenc
```

```bash
# Linux 裸机/WSL
./ffmpeg/ffmpeg -hide_banner -encoders | grep -Ei "nvenc|qsv|amf"
```

若自检命令未输出对应编码器，请更新显卡驱动或将 `ffmpeg/` 替换为拥有目标编码器的版本。

### Docker（Linux）

容器镜像会打包 `ffmpeg/` 目录；要让硬件编码生效，需要按厂商类型进行额外挂载：

#### NVIDIA NVENC

1. 宿主机安装官方 NVIDIA 驱动及 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)。
1. 运行容器时附加 GPU 资源，例如：

```bash
docker compose --profile gpu up -d --build
```

`docker-compose.yml` 片段：

```yaml
services:
  y2a-auto:
    image: fqscfqj/y2a-auto:latest
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    # 对于 Compose v2 也可以使用
    # runtime: nvidia
    # device_requests:
    #   - driver: nvidia
    #     count: 1
    #     capabilities: [[gpu]]
```

1. 设置 `VIDEO_ENCODER=nvenc`，并在容器内自检：

```bash
ffmpeg -hide_banner -encoders | grep -i nvenc
```

#### Intel QSV / VA-API

1. 启用 Intel iGPU，并安装 VAAPI/QSV 驱动（例如 `intel-media-va-driver-non-free`）。
1. 将 `/dev/dri` 映射进容器，同时根据需要设置 `LIBVA_DRIVER_NAME`。

```yaml
services:
  y2a-auto:
    image: fqscfqj/y2a-auto:latest
    devices:
      - /dev/dri:/dev/dri
    environment:
      - LIBVA_DRIVER_NAME=iHD
```

1. 设置 `VIDEO_ENCODER=qsv`，并在容器内执行 `ffmpeg -hide_banner -encoders | grep -i qsv` 进行确认。

#### AMD AMF / VAAPI

- AMF 仅在 Windows 上可用；Linux 环境可使用 VA-API (`VIDEO_ENCODER=cpu` + `-vf subtitles` + `-vaapi_device`) 或自行更换带有 `h264_vaapi`/`hevc_vaapi` 的 FFmpeg 并调整代码。
- 如需 Linux 上的 AMD 编码，可在 `ffmpeg/` 中放置包含 `h264_vaapi`/`hevc_vaapi` 的构建，并在 Docker 运行时挂载 `/dev/dri`；随后在 `config.json` 中设置 `VIDEO_ENCODER` 为 `cpu` 并在 `FFMPEG_LOCATION` 指向自定义脚本。

> 提示：容器内 `ffmpeg -encoders` 是判断编码器是否可用的唯一依据；若输出缺失，请检查驱动或替换 `ffmpeg/` 内容。应用在检测到硬件编码失败时会写入任务日志，并自动回退到 CPU。

### 自定义镜像（可选）

如果你希望完全控制 FFmpeg 版本，仍可以参考以下模式自定义镜像（例如从 `jrottenberg/ffmpeg` 提取 ffmpeg）：

```dockerfile
FROM jrottenberg/ffmpeg:6.1-nvidia AS ffmpeg

FROM python:3.11-slim
WORKDIR /app

COPY --from=ffmpeg /usr/local /usr/local
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "app.py"]
```

构建完自定义镜像后，仍需按上文步骤为容器挂载 GPU 设备。

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
