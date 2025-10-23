# Y2A-Auto Windows 可执行文件构建系统

本目录包含完整的Windows exe构建工具，支持一键生成便携式可执行文件。

## 🚀 快速开始

### 方法一：一键构建（推荐）
```bash
# 在build-tools目录中双击运行
build.bat
```

### 方法二：命令行构建
```bash
# 在build-tools目录中运行
python build_exe.py
```

## 📋 系统要求

### 构建环境要求
- **操作系统**: Windows 10/11 (64位)
- **Python**: 3.11 或更高版本
- **内存**: 至少 4GB 可用内存
- **磁盘**: 至少 5GB 可用空间
- **网络**: 稳定的互联网连接（下载依赖）

### 运行环境要求（最终用户）
- **操作系统**: Windows 10/11 (64位)
- **内存**: 至少 2GB 可用内存（已优化内存使用，支持并发控制）
- **磁盘**: 至少 3GB 可用空间
- **网络**: 互联网连接

## 🛠️ 构建工具说明

### 核心文件
- `build_exe.py` - 主构建脚本
- `setup_app.py` - 应用启动配置
- `build.bat` - 一键构建批处理
- `README.md` - 本说明文档

### 构建流程
1. **环境检查** - 验证Python版本和依赖
2. **依赖安装** - 自动安装PyInstaller等工具
3. **代码打包** - 使用PyInstaller打包Python代码
4. **资源下载** - 自动下载FFmpeg等依赖
5. **目录创建** - 生成完整的应用目录结构
6. **文档生成** - 创建启动脚本和说明文档
7. **清理优化** - 清理临时文件，优化包大小

## 🎯 特性优势

### ✅ 完全便携
- 无需安装Python环境
- 无需安装FFmpeg
- 无需配置环境变量
- 整个目录可复制使用

### ✅ 中文优化
- 完美支持中文路径
- 中文界面正确显示
- 中文日志正确编码
- UTF-8编码优化

### ✅ 智能构建
- 自动下载所需依赖
- 智能错误检测和提示
- 增量构建优化
- 构建过程可视化

### ✅ 用户友好
- 一键构建和运行
- 详细的使用说明
- 完善的错误处理
- 直观的Web界面

## 📁 构建产物

构建完成后在 `build-tools/dist/Y2A-Auto/` 目录：

```
dist/Y2A-Auto/
├── Y2A-Auto.exe           # 主程序
├── start.bat              # 启动脚本
├── README.txt             # 用户说明
├── _internal/             # 程序依赖（PyInstaller生成）
├── ffmpeg/                # 视频处理工具
│   ├── ffmpeg.exe
│   ├── ffprobe.exe
│   └── ffplay.exe
├── config/                # 配置文件目录
├── db/                    # 数据库文件目录
├── downloads/             # 下载文件目录
├── logs/                  # 日志文件目录
├── cookies/               # Cookie文件目录
├── temp/                  # 临时文件目录
└── acfunid/               # AcFun ID缓存目录
```

## 🔧 构建选项

### 自定义构建
可以修改 `build_exe.py` 中的配置：

```python
# FFmpeg下载地址
ffmpeg_url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"

# 隐藏导入模块列表
hiddenimports = [...]

# 数据文件收集
datas = [...]
```

### 环境变量
支持以下环境变量：
- `PYTHONIOENCODING=utf-8` - Python编码设置
- `LANG=zh_CN.UTF-8` - 系统语言设置

## 🚨 故障排除

### 构建问题

#### Python版本错误
```
错误: 需要Python 3.11或更高版本
```
**解决方案**: 升级Python到3.11+

#### 网络连接失败
```
FFmpeg下载失败: 网络连接错误
```
**解决方案**: 
1. 检查网络连接
2. 使用代理或VPN
3. 手动下载FFmpeg到指定目录

#### 依赖安装失败
```
pip install pyinstaller 失败
```
**解决方案**:
1. 升级pip: `python -m pip install --upgrade pip`
2. 使用国内镜像: `pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller`

#### 内存不足
```
构建过程中内存溢出
```
**解决方案**:
1. 关闭其他程序释放内存
2. 增加虚拟内存
3. 使用更强配置的机器

### 运行问题

#### 杀毒软件拦截
**现象**: exe文件被删除或无法运行
**解决方案**: 
1. 添加到杀毒软件白名单
2. 临时关闭实时保护
3. 使用Windows Defender排除项

#### 中文乱码
**现象**: 界面或日志出现乱码
**解决方案**:
1. 确保Windows系统支持UTF-8
2. 检查系统区域设置
3. 使用管理员权限运行

#### 端口占用
**现象**: 5000端口被占用
**解决方案**:
1. 关闭占用端口的程序
2. 修改程序配置使用其他端口
3. 重启系统

## 🔄 更新说明

### 版本兼容性
- 支持Python 3.11+
- 兼容Windows 10/11
- 向前兼容旧版配置文件

### 更新方法
1. 替换构建工具文件
2. 重新运行构建脚本
3. 测试新版本功能

## 📞 技术支持

### 获取帮助
- **项目主页**: https://github.com/fqscfqj/Y2A-Auto
- **问题反馈**: GitHub Issues
- **使用文档**: 项目Wiki

### 日志调试
构建过程中的日志信息可以帮助诊断问题：
- 详细的错误信息
- 构建步骤追踪
- 依赖检查结果

### 常见问题
查看项目Wiki的FAQ部分获取更多帮助。

---

**注意**: 首次构建需要下载大量依赖，请确保网络稳定。构建完成的exe文件可以在任何Windows系统上运行，无需额外安装。 