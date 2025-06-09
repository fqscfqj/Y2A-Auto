#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Y2A-Auto Windows 可执行文件构建工具
支持中文环境，自动下载依赖，生成便携式exe
"""

import os
import sys
import shutil
import subprocess
import zipfile
import requests
from pathlib import Path

def setup_build_environment():
    """设置构建环境"""
    print("设置构建环境...")
    
    # 确保在build-tools目录中
    if not os.getcwd().endswith('build-tools'):
        if os.path.exists('build-tools'):
            os.chdir('build-tools')
        else:
            print("错误: 未找到build-tools目录")
            sys.exit(1)
    
    current_dir = os.getcwd()
    project_root = os.path.dirname(current_dir)
    
    print(f"构建目录: {current_dir}")
    print(f"项目根目录: {project_root}")
    
    # 检查Python版本
    if sys.version_info < (3, 8):
        print("错误: 需要Python 3.8或更高版本")
        sys.exit(1)
    
    return project_root

def install_dependencies():
    """安装必要的构建依赖"""
    print("检查构建依赖...")
    
    dependencies = ['pyinstaller', 'requests']
    
    for dep in dependencies:
        try:
            __import__(dep)
            print(f"✓ {dep} 已安装")
        except ImportError:
            print(f"安装 {dep}...")
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', dep])

def download_ffmpeg():
    """下载FFmpeg Windows版本"""
    print("正在下载FFmpeg...")
    
    ffmpeg_url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
    ffmpeg_zip = "ffmpeg.zip"
    
    try:
        # 如果已存在则跳过
        if os.path.exists("dist/Y2A-Auto/ffmpeg/ffmpeg.exe"):
            print("FFmpeg已存在，跳过下载")
            return
        
        response = requests.get(ffmpeg_url, stream=True)
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        
        with open(ffmpeg_zip, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    percent = (downloaded / total_size) * 100
                    print(f"\r下载进度: {percent:.1f}%", end='', flush=True)
        
        print("\n解压FFmpeg...")
        
        # 解压FFmpeg
        with zipfile.ZipFile(ffmpeg_zip, 'r') as zip_ref:
            zip_ref.extractall("ffmpeg_temp")
        
        # 移动到dist目录
        ffmpeg_dir = next(Path("ffmpeg_temp").glob("ffmpeg-*"))
        bin_dir = ffmpeg_dir / "bin"
        
        os.makedirs("dist/Y2A-Auto/ffmpeg", exist_ok=True)
        
        for file in bin_dir.glob("*.exe"):
            shutil.copy2(file, "dist/Y2A-Auto/ffmpeg/")
            print(f"✓ 复制 {file.name}")
        
        # 清理
        shutil.rmtree("ffmpeg_temp")
        os.remove(ffmpeg_zip)
        
        print("FFmpeg下载完成")
        
    except Exception as e:
        print(f"FFmpeg下载失败: {e}")
        print("请手动下载FFmpeg并放置到dist/Y2A-Auto/ffmpeg/目录")

def create_spec_file():
    """创建PyInstaller spec文件"""
    print("生成PyInstaller配置文件...")
    
    spec_content = '''# -*- mode: python ; coding: utf-8 -*-

import os

block_cipher = None

# 收集所有数据文件
datas = [
    ('../templates', 'templates'),
    ('../static', 'static'),
    ('../modules', 'modules'),
    ('../userscripts', 'userscripts'),
    ('../docs', 'docs'),
    ('../app.py', '.'),
]

# 隐藏导入 - 包含所有可能需要的模块
hiddenimports = [
    # 核心框架
    'flask',
    'sqlite3',
    'yt_dlp',
    'openai',
    'requests',
    'apscheduler',
    
    # Flask相关
    'flask_cors',
    'werkzeug',
    'jinja2',
    'click',
    'itsdangerous',
    'markupsafe',
    'blinker',
    
    # 网络相关
    'urllib3',
    'certifi',
    'charset_normalizer',
    'idna',
    'websockets',
    'brotli',
    
    # Google API相关
    'googleapiclient',
    'googleapiclient.discovery',
    'googleapiclient.errors',
    'googleapiclient.http',
    'google_auth_oauthlib',
    'google.auth',
    'google.auth.transport',
    'google.oauth2',
    'google.oauth2.credentials',
    
    # 加密相关
    'cryptography',
    'Crypto',
    'Cryptodome',
    'mutagen',
    
    # 图像处理
    'PIL',
    'PIL.Image',
    'PIL.ImageOps',
    'PIL.ImageDraw',
    'PIL.ImageFont',
    'Pillow',
    
    # 系统相关
    'logging',
    'logging.handlers',
    'logging.config',
    'json',
    'datetime',
    'hashlib',
    'hmac',
    'base64',
    'uuid',
    'threading',
    'multiprocessing',
    'concurrent',
    'concurrent.futures',
    'asyncio',
    
    # 邮件相关
    'email',
    'email.mime',
    'email.mime.text',
    'email.mime.multipart',
    
    # 调度相关
    'packaging',
    'six',
    'pytz',
    'tzlocal',
    
    # 系统集成
    'secretstorage',
    'keyring',
    'jeepney',
]

a = Analysis(
    ['setup_app.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Y2A-Auto',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='../static/img/favicon.ico' if os.path.exists('../static/img/favicon.ico') else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Y2A-Auto',
)
'''
    
    with open('Y2A-Auto.spec', 'w', encoding='utf-8') as f:
        f.write(spec_content)
    
    print("✓ PyInstaller配置文件已生成")

def build_executable():
    """构建可执行文件"""
    print("开始构建可执行文件...")
    
    # 生成spec文件
    create_spec_file()
    
    # 运行PyInstaller
    cmd = [sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm', 'Y2A-Auto.spec']
    
    try:
        subprocess.check_call(cmd)
        print("✓ 可执行文件构建完成")
    except subprocess.CalledProcessError as e:
        print(f"构建失败: {e}")
        sys.exit(1)

def create_portable_package():
    """创建便携式包"""
    print("创建便携式包...")
    
    # 确保dist目录存在
    os.makedirs("dist/Y2A-Auto", exist_ok=True)
    
    # 创建必要的目录
    dirs_to_create = [
        'dist/Y2A-Auto/config',
        'dist/Y2A-Auto/db', 
        'dist/Y2A-Auto/downloads',
        'dist/Y2A-Auto/logs',
        'dist/Y2A-Auto/cookies',
        'dist/Y2A-Auto/temp',
        'dist/Y2A-Auto/acfunid',
    ]
    
    for dir_path in dirs_to_create:
        os.makedirs(dir_path, exist_ok=True)
        print(f"✓ 创建目录: {dir_path}")
    
    # 复制配置文件示例
    config_source = '../config'
    if os.path.exists(config_source):
        for file in os.listdir(config_source):
            if file.endswith('.json'):
                shutil.copy2(f'{config_source}/{file}', f'dist/Y2A-Auto/config/')
                print(f"✓ 复制配置文件: {file}")
    
    # 创建启动脚本
    create_start_script()
    
    # 创建说明文档
    create_readme()
    
    print("✓ 便携式包创建完成")

def create_start_script():
    """创建启动脚本"""
    start_script = '''@echo off
chcp 65001 >nul
title Y2A-Auto - YouTube to AcFun 自动化工具

echo.
echo ================================================
echo    Y2A-Auto - YouTube to AcFun 自动化工具
echo ================================================
echo.
echo 正在启动程序...
echo Web界面将在 http://localhost:5000 启动
echo.
echo 首次启动可能需要几分钟时间进行初始化
echo 请耐心等待，不要关闭此窗口
echo.
echo 要停止程序，请按 Ctrl+C
echo ================================================
echo.

Y2A-Auto.exe

if errorlevel 1 (
    echo.
    echo 程序异常退出，错误代码: %errorlevel%
    echo 请检查日志文件或联系技术支持
    echo.
)

echo.
echo 程序已退出，按任意键关闭窗口...
pause >nul
'''
    
    with open('dist/Y2A-Auto/start.bat', 'w', encoding='gbk') as f:
        f.write(start_script)
    
    print("✓ 启动脚本已创建")

def create_readme():
    """创建README文档"""
    readme_content = f'''# Y2A-Auto Windows 便携版

## 快速开始

1. **启动程序**
   双击 `start.bat` 启动程序

2. **访问界面**
   程序启动后，浏览器访问 http://localhost:5000

3. **首次配置**
   - 设置 OpenAI API 密钥（用于翻译）
   - 配置 YouTube API 密钥（用于监控）
   - 添加 AcFun 登录信息

## 目录说明

```
Y2A-Auto/
├── Y2A-Auto.exe        # 主程序
├── start.bat           # 启动脚本
├── README.txt          # 本说明文件
├── ffmpeg/             # 视频处理工具
│   ├── ffmpeg.exe
│   ├── ffprobe.exe
│   └── ffplay.exe
├── config/             # 配置文件
├── db/                 # 数据库文件
├── downloads/          # 下载文件
├── logs/               # 日志文件
├── cookies/            # Cookie文件
├── temp/               # 临时文件
└── acfunid/            # AcFun ID缓存
```

## 功能特性

### ✅ 完全便携
- 无需安装Python环境
- 无需安装FFmpeg
- 无需配置环境变量
- 整个目录可以复制到任何电脑使用

### ✅ 中文优化
- 完美支持中文路径和文件名
- 中文界面友好显示
- 中文日志正确编码

### ✅ 一键启动
- 双击start.bat即可运行
- 自动打开Web管理界面
- 智能错误提示

## 系统要求

- **操作系统**: Windows 10/11 (64位)
- **内存**: 至少 2GB 可用内存
- **存储**: 至少 3GB 可用磁盘空间
- **网络**: 需要互联网连接

## 使用说明

### 首次运行
1. 确保有稳定的网络连接
2. 双击 `start.bat` 启动程序
3. 等待程序初始化完成
4. 浏览器会自动打开管理界面

### 配置步骤
1. **OpenAI配置**: 在设置页面添加OpenAI API密钥
2. **YouTube配置**: 添加YouTube Data API v3密钥
3. **AcFun配置**: 配置AcFun账号信息或Cookie
4. **监控设置**: 添加要监控的YouTube频道

### 日常使用
- 程序会自动监控配置的YouTube频道
- 有新视频时自动下载并上传到AcFun
- 可在Web界面查看任务状态和日志
- 支持手动添加单个视频任务

## 故障排除

### 程序无法启动
1. 检查是否有杀毒软件拦截
2. 尝试以管理员身份运行
3. 检查Windows防火墙设置
4. 查看logs目录中的错误日志

### 下载失败
1. 检查网络连接
2. 确认YouTube API密钥有效
3. 检查视频是否可公开访问
4. 查看具体错误信息

### 上传问题
1. 确认AcFun登录信息正确
2. 检查视频格式是否支持
3. 确保有足够的上传权限
4. 查看上传错误日志

## 技术支持

### 日志文件
程序运行产生的所有日志都保存在 `logs/` 目录中：
- `app.log` - 主程序日志
- `monitor.log` - 监控任务日志
- `upload.log` - 上传任务日志

### 配置文件
所有配置保存在 `config/config.json` 中，可以手动编辑。

### 获取帮助
- 项目主页: https://github.com/fqscfqj/Y2A-Auto
- 问题反馈: 通过GitHub Issues提交
- 使用文档: 项目Wiki页面

## 版本信息

- 构建时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- Python版本: {sys.version.split()[0]}
- 构建环境: Windows x64

---

**注意**: 首次使用请仔细阅读项目文档，正确配置各项参数后再开始使用。
'''
    
    with open('dist/Y2A-Auto/README.txt', 'w', encoding='utf-8') as f:
        f.write(readme_content)
    
    print("✓ 说明文档已创建")

def cleanup_build_files():
    """清理构建临时文件"""
    print("清理临时文件...")
    
    # 保留dist目录，清理其他临时文件
    cleanup_dirs = ['build']
    
    for dir_name in cleanup_dirs:
        if os.path.exists(dir_name):
            try:
                shutil.rmtree(dir_name)
                print(f"✓ 清理: {dir_name}")
            except Exception as e:
                print(f"清理 {dir_name} 时出错: {e}")

def main():
    """主函数"""
    print("=" * 60)
    print("    Y2A-Auto Windows 可执行文件构建工具")
    print("=" * 60)
    print()
    
    try:
        # 设置环境
        project_root = setup_build_environment()
        
        # 安装依赖
        install_dependencies()
        
        # 构建可执行文件
        build_executable()
        
        # 下载FFmpeg
        download_ffmpeg()
        
        # 创建便携式包
        create_portable_package()
        
        # 清理临时文件
        cleanup_build_files()
        
        print()
        print("=" * 60)
        print("🎉 构建完成!")
        print("=" * 60)
        print(f"📁 可执行文件位置: build-tools/dist/Y2A-Auto/")
        print(f"🚀 运行方式: 双击 build-tools/dist/Y2A-Auto/start.bat")
        print(f"🌐 Web界面: http://localhost:5000")
        print(f"📖 使用说明: build-tools/dist/Y2A-Auto/README.txt")
        print("=" * 60)
        
    except KeyboardInterrupt:
        print("\n构建被用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 构建失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == '__main__':
    main() 