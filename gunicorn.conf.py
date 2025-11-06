"""Gunicorn 生产环境配置文件

此配置文件针对 Y2A-Auto 应用进行了优化,
使用线程工作模式以支持长时间运行的视频处理任务。
"""

import multiprocessing
import os

# 服务器套接字
bind = "0.0.0.0:5000"
backlog = 2048

# Worker 进程
workers = int(os.getenv("GUNICORN_WORKERS", "2"))
worker_class = "gthread"
threads = int(os.getenv("GUNICORN_THREADS", "4"))
worker_connections = 1000
max_requests = 1000
max_requests_jitter = 50
timeout = 120
graceful_timeout = 30
keepalive = 5

# 日志
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'
capture_output = True
enable_stdio_inheritance = True

# 进程命名
proc_name = "y2a-auto"

# 服务器机制
daemon = False
pidfile = None
umask = 0
user = None
group = None
tmp_upload_dir = None

# SSL (如果需要)
# keyfile = None
# certfile = None

# 性能调优
worker_tmp_dir = "/dev/shm"  # 使用内存文件系统提升性能
preload_app = False  # 为了支持热重载,设为 False
reload = False
reload_engine = "auto"

# 钩子函数
def on_starting(server):
    """服务器启动前执行"""
    print("🚀 Gunicorn 正在启动 Y2A-Auto 应用...")

def on_reload(server):
    """服务器重载时执行"""
    print("♻️  Gunicorn 正在重载配置...")

def when_ready(server):
    """服务器就绪时执行"""
    print("✅ Y2A-Auto 应用已就绪,正在监听 {}".format(bind))

def worker_int(worker):
    """Worker 接收到 INT 或 QUIT 信号时执行"""
    print("⚠️  Worker {} 收到终止信号".format(worker.pid))

def worker_abort(worker):
    """Worker 接收到 SIGABRT 信号时执行"""
    print("❌ Worker {} 异常终止".format(worker.pid))

def pre_fork(server, worker):
    """Worker fork 前执行"""
    pass

def post_fork(server, worker):
    """Worker fork 后执行"""
    print("👷 Worker {} 已启动".format(worker.pid))

def pre_exec(server):
    """在新的 master 进程 fork 前执行"""
    print("🔄 正在准备新的 master 进程...")

def pre_request(worker, req):
    """处理请求前执行"""
    worker.log.debug("正在处理请求: %s %s", req.method, req.path)

def post_request(worker, req, environ, resp):
    """处理请求后执行"""
    pass

def child_exit(server, worker):
    """Worker 退出时执行"""
    print("👋 Worker {} 已退出".format(worker.pid))

def worker_exit(server, worker):
    """Worker 退出时执行(在 master 进程中)"""
    pass

def nworkers_changed(server, new_value, old_value):
    """Worker 数量改变时执行"""
    print("📊 Worker 数量从 {} 变更为 {}".format(old_value, new_value))

def on_exit(server):
    """服务器退出时执行"""
    print("👋 Gunicorn 服务器已关闭")
