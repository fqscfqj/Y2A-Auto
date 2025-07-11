# -*- mode: python ; coding: utf-8 -*-

import os

block_cipher = None

# 收集所有数据文件
datas = [
    ('../templates', 'templates'),
    ('../static', 'static'),
    ('../modules', 'modules'),
    ('../userscripts', 'userscripts'),
    ('../docs', 'docs'),
    ('../acfunid', 'acfunid'),
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
    
    # 阿里云内容审核相关
    'alibabacloud_green20220302',
    'alibabacloud_green20220302.client',
    'alibabacloud_green20220302.models',
    'alibabacloud_tea_openapi',
    'alibabacloud_tea_openapi.models',
    'alibabacloud_tea_util',
    'alibabacloud_tea_util.models',
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
