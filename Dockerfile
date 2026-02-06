# 多阶段构建 Dockerfile
# syntax=docker/dockerfile:1.4

# ============================================================
# 第一阶段：Python 依赖构建
# ============================================================
FROM python:3.11-slim AS builder

WORKDIR /app

# 安装构建依赖
ENV DEBIAN_FRONTEND=noninteractive
RUN --mount=type=cache,target=/var/cache/apt,id=y2a-apt-cache-builder \
    rm -f /var/lib/apt/lists/lock /var/lib/dpkg/lock-frontend /var/cache/apt/archives/lock || true \
    && dpkg --configure -a || true \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        python3-dev \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb \
    && apt-get clean

# 复制依赖文件
COPY requirements.txt .

# 安装Python依赖到本地目录（使用onnxruntime替代torch以减小镜像体积）
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --user -r requirements.txt

# 验证 yt-dlp 安装
RUN /root/.local/bin/yt-dlp --version

# ============================================================
# 第二阶段：FFmpeg 下载
# ============================================================
FROM debian:bookworm-slim AS ffmpeg-builder

ARG TARGETARCH
ARG FFMPEG_VARIANT=btbn

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux \
    && mkdir -p /ffmpeg-out \
    && arch="${TARGETARCH:-amd64}" \
    && tmpdir="$(mktemp -d)" \
    && case "${FFMPEG_VARIANT}" in \
        btbn) \
            case "$arch" in \
                amd64|x86_64) ffmpeg_url="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz" ;; \
                arm64|aarch64) ffmpeg_url="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linuxarm64-gpl.tar.xz" ;; \
                *) echo "FFMPEG_VARIANT=btbn is not available for $arch" >&2 && exit 1 ;; \
            esac ;; \
        static) \
            case "$arch" in \
                amd64|x86_64) ffmpeg_url="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz" ;; \
                arm64|aarch64) ffmpeg_url="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz" ;; \
                arm|armv7l)   ffmpeg_url="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-armhf-static.tar.xz" ;; \
                *) echo "Unsupported TARGETARCH: $arch" >&2 && exit 1 ;; \
            esac ;; \
        *) echo "Unknown FFMPEG_VARIANT: ${FFMPEG_VARIANT}" >&2 && exit 1 ;; \
    esac \
    && curl -fsSL "$ffmpeg_url" -o "$tmpdir/ffmpeg.tar.xz" \
    && tar -xf "$tmpdir/ffmpeg.tar.xz" -C "$tmpdir" \
    && payload_dir="$(find "$tmpdir" -mindepth 1 -maxdepth 1 -type d -name 'ffmpeg*' | head -n 1)" \
    && if [ -z "$payload_dir" ]; then echo "Unable to locate extracted ffmpeg directory" >&2 && exit 1; fi \
    && if [ -x "$payload_dir/bin/ffmpeg" ]; then cp "$payload_dir/bin/ffmpeg" /ffmpeg-out/ffmpeg; \
       elif [ -x "$payload_dir/ffmpeg" ]; then cp "$payload_dir/ffmpeg" /ffmpeg-out/ffmpeg; \
       else echo "WARNING: ffmpeg binary not found in expected locations: '$payload_dir/bin/ffmpeg' or '$payload_dir/ffmpeg'" >&2; fi \
    && if [ -x "$payload_dir/bin/ffprobe" ]; then cp "$payload_dir/bin/ffprobe" /ffmpeg-out/ffprobe; \
       elif [ -x "$payload_dir/ffprobe" ]; then cp "$payload_dir/ffprobe" /ffmpeg-out/ffprobe; \
       else echo "WARNING: ffprobe binary not found in expected locations: '$payload_dir/bin/ffprobe' or '$payload_dir/ffprobe'" >&2; fi \
    && rm -rf "$tmpdir" \
    && if [ ! -f /ffmpeg-out/ffmpeg ]; then echo "ERROR: ffmpeg binary not found" >&2 && exit 1; fi \
    && if [ ! -f /ffmpeg-out/ffprobe ]; then echo "ERROR: ffprobe binary not found" >&2 && exit 1; fi \
    && chmod +x /ffmpeg-out/ffmpeg /ffmpeg-out/ffprobe

# ============================================================
# 第三阶段：运行阶段
# ============================================================
FROM python:3.11-slim

ARG ENABLE_GPU_DRIVERS=false

WORKDIR /app

# 安装运行时依赖（不再需要 curl 和 xz-utils，FFmpeg 已在构建阶段下载）
ENV DEBIAN_FRONTEND=noninteractive
RUN --mount=type=cache,target=/var/cache/apt,id=y2a-apt-cache-runtime \
    rm -f /var/lib/apt/lists/lock /var/lib/dpkg/lock-frontend /var/cache/apt/archives/lock || true \
    && dpkg --configure -a || true \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libfontconfig1 \
        libfreetype6 \
        libfribidi0 \
        libgnutls30 \
        libgomp1 \
        libharfbuzz0b \
        libunistring5 \
        libxml2 \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb \
    && apt-get clean \
    && useradd --create-home --shell /bin/bash y2a

# 可选：安装 GPU 编码支持库（VAAPI/Intel/AMD），通过 --build-arg ENABLE_GPU_DRIVERS=true 启用
RUN --mount=type=cache,target=/var/cache/apt,id=y2a-apt-cache-gpu \
    if [ "${ENABLE_GPU_DRIVERS}" = "true" ]; then \
        rm -rf /var/lib/apt/lists/* \
        && apt-get update \
        && apt-get install -y --no-install-recommends \
            libva2 \
            libva-drm2 \
            vainfo \
        && (apt-get install -y --no-install-recommends intel-media-va-driver-non-free 2>/dev/null || echo "Intel VA driver not available") \
        && (apt-get install -y --no-install-recommends mesa-va-drivers 2>/dev/null || echo "Mesa VA drivers not available") \
        && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb \
        && apt-get clean \
        && echo "GPU driver packages installed"; \
    else \
        echo "GPU drivers skipped (use --build-arg ENABLE_GPU_DRIVERS=true to enable)"; \
    fi

# 从构建阶段复制Python包
COPY --from=builder /root/.local /home/y2a/.local

# 从 ffmpeg-builder 阶段复制二进制文件（仅 ffmpeg + ffprobe）
COPY --from=ffmpeg-builder /ffmpeg-out/ffmpeg /app/ffmpeg/bin/ffmpeg
COPY --from=ffmpeg-builder /ffmpeg-out/ffprobe /app/ffmpeg/bin/ffprobe

# 复制应用代码
COPY --chown=y2a:y2a . .

# 创建必要的目录和符号链接，设置权限
RUN mkdir -p /app/config /app/db /app/downloads /app/logs /app/cookies /app/temp \
    && ln -sf /app/ffmpeg/bin/ffmpeg /app/ffmpeg/ffmpeg \
    && ln -sf /app/ffmpeg/bin/ffprobe /app/ffmpeg/ffprobe \
    && ln -sf /app/ffmpeg/bin/ffmpeg /usr/local/bin/ffmpeg \
    && ln -sf /app/ffmpeg/bin/ffprobe /usr/local/bin/ffprobe \
    && chown -R y2a:y2a /app \
    && chown -R y2a:y2a /home/y2a/.local \
    && chmod +x /home/y2a/.local/bin/* 2>/dev/null || true \
    && chmod 755 /app/config /app/db /app/downloads /app/logs /app/cookies /app/temp

# 创建内联启动脚本
RUN echo '#!/bin/bash\n\
set -e\n\
echo "Y2A-Auto Docker starting..."\n\
export PYTHONUNBUFFERED=1\n\
export PYTHONIOENCODING=utf-8\n\
\n\
# 确保目录权限\n\
for dir in /app/config /app/db /app/downloads /app/logs /app/cookies /app/temp; do\n\
    [ -d "$dir" ] || mkdir -p "$dir"\n\
    [ -w "$dir" ] || chmod 755 "$dir" 2>/dev/null || true\n\
done\n\
\n\
echo "Starting Y2A-Auto..."\n\
exec "$@"' > /usr/local/bin/docker-entrypoint.sh \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

# 确保本地包在PATH中
ENV PATH=/home/y2a/.local/bin:$PATH
# 避免引用未定义变量的告警，直接补充常见站点路径
ENV PYTHONPATH=/home/y2a/.local/lib/python3.11/site-packages:/usr/local/lib/python3.11/site-packages

# 切换到非root用户
USER y2a

# 验证 yt-dlp 在运行阶段可用
RUN yt-dlp --version

# 应用程序监听的端口
EXPOSE 5000

# 添加健康检查（使用 Python 替代 curl，避免在运行阶段安装 curl）
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/')" || exit 1

# 设置入口点
ENTRYPOINT ["docker-entrypoint.sh"]

# 启动应用
CMD ["python", "app.py"]
