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

# 创建必要的目录
RUN mkdir -p /app/config /app/db /app/downloads /app/logs /app/cookies /app/temp \
    && chown -R y2a:y2a /app

# 确保本地包在PATH中
ENV PATH=/home/y2a/.local/bin:$PATH
ENV PYTHONPATH=/home/y2a/.local/lib/python3.10/site-packages:$PYTHONPATH

# 切换到非root用户
USER y2a

# 应用程序监听的端口
EXPOSE 5000

# 添加健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

# 启动应用
CMD ["python", "app.py"] 