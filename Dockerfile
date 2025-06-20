# 多阶段构建 Dockerfile
# 第一阶段：构建阶段
FROM python:3.10-slim as builder

# 设置工作目录
WORKDIR /app

# 安装构建依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装Python依赖到本地目录
RUN pip install --user --no-cache-dir --trusted-host pypi.python.org --trusted-host pypi.org --trusted-host files.pythonhosted.org -r requirements.txt

# 验证 yt-dlp 安装
RUN /root/.local/bin/yt-dlp --version

# 第二阶段：运行阶段
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 安装运行时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash y2a

# 从构建阶段复制Python包
COPY --from=builder /root/.local /home/y2a/.local

# 复制应用代码
COPY --chown=y2a:y2a . .

# 创建必要的目录并设置权限
RUN mkdir -p /app/config /app/db /app/downloads /app/logs /app/cookies /app/temp \
    && chown -R y2a:y2a /app \
    && chown -R y2a:y2a /home/y2a/.local \
    && chmod +x /home/y2a/.local/bin/* 2>/dev/null || true \
    && chmod 755 /app/config /app/db /app/downloads /app/logs /app/cookies /app/temp

# 创建内联启动脚本
RUN echo '#!/bin/bash\n\
set -e\n\
echo "🚀 Y2A-Auto Docker 容器启动中..."\n\
export PYTHONUNBUFFERED=1\n\
export PYTHONIOENCODING=utf-8\n\
\n\
# 确保目录权限\n\
for dir in /app/config /app/db /app/downloads /app/logs /app/cookies /app/temp; do\n\
    [ -d "$dir" ] || mkdir -p "$dir"\n\
    [ -w "$dir" ] || chmod 755 "$dir" 2>/dev/null || true\n\
done\n\
\n\
echo "🎯 启动 Y2A-Auto 应用..."\n\
exec "$@"' > /usr/local/bin/docker-entrypoint.sh \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

# 确保本地包在PATH中
ENV PATH=/home/y2a/.local/bin:$PATH
ENV PYTHONPATH=/home/y2a/.local/lib/python3.10/site-packages:$PYTHONPATH

# 切换到非root用户
USER y2a

# 验证 yt-dlp 在运行阶段可用
RUN yt-dlp --version

# 应用程序监听的端口
EXPOSE 5000

# 添加健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

# 设置入口点
ENTRYPOINT ["docker-entrypoint.sh"]

# 启动应用
CMD ["python", "app.py"] 