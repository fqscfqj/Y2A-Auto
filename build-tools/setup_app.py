#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Y2A-Auto 应用程序启动配置
支持打包环境和开发环境，优化中文编码支持
"""

import os
import sys
import platform
import locale
from pathlib import Path

def setup_chinese_encoding():
    """设置中文编码支持"""
    if platform.system() == "Windows":
        # 设置控制台编码为UTF-8
        os.system('chcp 65001 >nul 2>&1')
        
        # 设置环境变量
        os.environ["PYTHONIOENCODING"] = "utf-8"
        os.environ["LANG"] = "zh_CN.UTF-8"
        
        # 尝试设置系统区域设置
        try:
            locale.setlocale(locale.LC_ALL, 'zh_CN.UTF-8')
        except:
            try:
                locale.setlocale(locale.LC_ALL, 'Chinese (Simplified)_China.UTF-8')
            except:
                pass  # 如果设置失败也不影响运行

def setup_environment():
    """设置应用运行环境"""
    
    # 首先设置中文编码
    setup_chinese_encoding()
    
    # 检测运行环境
    if getattr(sys, 'frozen', False):
        # 运行在PyInstaller打包的exe中
        application_path = os.path.dirname(sys.executable)
        is_frozen = True
        print("运行模式: 打包版本")
    else:
        # 运行在正常Python环境中
        application_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        is_frozen = False
        print("运行模式: 开发版本")
    
    print(f"应用路径: {application_path}")
    
    # 设置工作目录
    os.chdir(application_path)
    
    # 在打包版本中调整Python路径
    if is_frozen:
        # 添加_internal目录到Python路径，这样可以找到app.py
        internal_dir = os.path.join(application_path, '_internal')
        if os.path.exists(internal_dir) and internal_dir not in sys.path:
            sys.path.insert(0, internal_dir)
            print(f"✓ 添加内部模块路径: {internal_dir}")
    
    # 添加FFmpeg路径
    if is_frozen and platform.system() == "Windows":
        ffmpeg_path = os.path.join(application_path, "ffmpeg")
        if os.path.exists(ffmpeg_path):
            current_path = os.environ.get("PATH", "")
            if ffmpeg_path not in current_path:
                os.environ["PATH"] = ffmpeg_path + os.pathsep + current_path
                print(f"✓ 添加FFmpeg路径: {ffmpeg_path}")
    
    # 创建必要的目录
    directories = [
        "config", "db", "downloads", "logs", 
        "cookies", "temp", "acfunid", "fonts"
    ]
    
    for directory in directories:
        dir_path = os.path.join(application_path, directory)
        os.makedirs(dir_path, exist_ok=True)
    
    print("✓ 工作目录初始化完成")
    
    return application_path, is_frozen

def check_dependencies():
    """检查运行时依赖"""
    required_modules = [
        'flask', 'yt_dlp', 'requests', 'sqlite3',
        'openai', 'apscheduler', 'waitress'
    ]
    
    missing_modules = []
    
    for module in required_modules:
        try:
            __import__(module)
        except ImportError:
            missing_modules.append(module)
    
    if missing_modules:
        print(f"❌ 缺少必要依赖: {', '.join(missing_modules)}")
        return False
    
    print("✓ 依赖检查通过")
    return True

def check_ffmpeg():
    """检查FFmpeg是否可用"""
    ffmpeg_path = "ffmpeg"
    
    try:
        import subprocess
        result = subprocess.run(
            [ffmpeg_path, '-version'], 
            capture_output=True, 
            text=True, 
            timeout=5,
            encoding='utf-8',
            errors='replace'
        )
        if result.returncode == 0:
            print("✓ FFmpeg 可用")
            return True
    except:
        pass
    
    print("⚠ FFmpeg 未找到或不可用")
    return False

def start_application(app_path, is_frozen):
    """启动主应用"""
    app_module = None
    try:
        # 导入主应用模块
        import app as app_module

        print("✓ 主应用模块加载成功")

        flask_app = app_module.initialize_runtime()
        flask_app.config['DEBUG'] = False
        flask_app.config['TEMPLATES_AUTO_RELOAD'] = False

        def _safe_int(value, default):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        host = os.environ.get("Y2A_HOST", "0.0.0.0") or "0.0.0.0"
        port = _safe_int(os.environ.get("Y2A_PORT", 5000), 5000)
        threads = max(1, _safe_int(os.environ.get("Y2A_WAITRESS_THREADS", 8), 8))

        print("启动Web服务...")
        print("=" * 50)
        print(f"🌐 Web界面地址: http://{host}:{port}")
        print("📝 使用说明: README.txt")
        print("📋 按 Ctrl+C 停止程序")
        print("=" * 50)

        try:
            from waitress import serve
        except ImportError:
            print("⚠ 未检测到 Waitress，回退到 Flask 开发服务器（仅适用于调试）。")
            flask_app.run(
                host=host,
                port=port,
                debug=False,
                use_reloader=False,
                threaded=True
            )
        else:
            print(f"🚀 使用 Waitress WSGI 服务器启动，线程数: {threads}")
            serve(flask_app, host=host, port=port, threads=threads)
        
    except KeyboardInterrupt:
        print("\n程序被用户停止")
        sys.exit(0)
    except Exception as e:
        print(f"❌ 启动失败: {e}")
        
        # 调试信息
        print(f"\n调试信息:")
        print(f"当前工作目录: {os.getcwd()}")
        print(f"Python路径: {sys.path[:3]}...")  # 只显示前3个路径
        
        if is_frozen:
            internal_dir = os.path.join(app_path, '_internal')
            print(f"_internal目录存在: {os.path.exists(internal_dir)}")
            if os.path.exists(internal_dir):
                files = os.listdir(internal_dir)
                app_files = [f for f in files if 'app' in f.lower()]
                print(f"相关文件: {app_files}")
        
        input("\n按回车键退出...")
        sys.exit(1)
    finally:
        if app_module is not None:
            try:
                app_module.shutdown_runtime()
            except Exception as e:
                # 忽略关闭时的异常，但记录错误以便调试
                print(f"⚠ 关闭应用时发生异常: {e}")

def main():
    """主函数"""
    print("初始化 Y2A-Auto 应用环境...")
    
    try:
        # 设置环境
        app_path, is_frozen = setup_environment()
        
        # 检查依赖
        if not check_dependencies():
            input("按回车键退出...")
            sys.exit(1)
        
        # 检查FFmpeg（警告但不阻止启动）
        check_ffmpeg()
        
        # 启动应用
        start_application(app_path, is_frozen)
        
    except Exception as e:
        print(f"❌ 初始化失败: {e}")
        import traceback
        traceback.print_exc()
        input("按回车键退出...")
        sys.exit(1)

if __name__ == '__main__':
    main() 