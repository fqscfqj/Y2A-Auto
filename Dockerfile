# 使用官方 Python 运行时作为父镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖 (包括ffmpeg)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 将依赖文件复制到工作目录
COPY requirements.txt ./

# 安装项目依赖
# --no-cache-dir 确保 pip 不会存储下载缓存，减小镜像体积
# --trusted-host pypi.python.org --trusted-host pypi.org --trusted-host files.pythonhosted.org 用于处理可能的网络问题
RUN pip install --no-cache-dir --trusted-host pypi.python.org --trusted-host pypi.org --trusted-host files.pythonhosted.org -r requirements.txt

# 将项目文件复制到工作目录
COPY . .

# 应用程序监听的端口
EXPOSE 5000

# 定义容器启动时运行的命令
# 启动应用
CMD ["python", "app.py"] 