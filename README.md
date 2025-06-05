<div align="center">

# 🎬 Y2A-Auto

**YouTube to AcFun 自动化工具**

*一键从 YouTube 搬运视频到 AcFun，支持 AI 翻译、字幕翻译、内容审核、智能标签生成*

[![License](https://img.shields.io/badge/license-GPL%20v3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-green.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-supported-blue.svg)](https://www.docker.com/)
[![Flask](https://img.shields.io/badge/flask-2.3+-blue.svg)](https://flask.palletsprojects.com/)
[![Status](https://img.shields.io/badge/status-stable-brightgreen.svg)]()

[🚀 快速开始](#-快速开始) • [📖 功能特性](#-功能特性) • [🔧 部署方式](#-部署方式) • [📱 使用指南](#-使用指南) • [❓ 常见问题](#-常见问题)

---

</div>

## 📖 项目简介

Y2A-Auto 是一个现代化的 YouTube 到 AcFun 视频搬运自动化工具，基于 **Flask Web 框架** 构建，提供完整的端到端解决方案：

🎯 **核心价值**
- 🤖 **全自动化流程** - 从 YouTube 下载到 AcFun 发布一站式完成
- 🧠 **AI 智能处理** - 自动翻译、标签生成、内容审核
- 🎞️ **字幕翻译** - 自动下载和翻译字幕，支持多种语言
- 🎨 **友好的 Web 界面** - 直观的任务管理和状态监控
- 📱 **浏览器集成** - 在 YouTube 页面一键推送视频

## ✨ 功能特性

### 🎥 视频处理
- **📥 智能下载** - 使用最新 yt-dlp (≥2025.5.22)，支持多格式回退机制
- **🎞️ 字幕处理** - 自动下载、翻译和嵌入字幕
- **🖼️ 封面处理** - 自动裁剪/填充适配 AcFun 封面规格
- **📊 格式优化** - 自动选择最佳视频质量和格式
- **🎬 FFmpeg 集成** - 专业视频处理和转码

### 🤖 AI 增强
- **🌐 智能翻译** - 基于 OpenAI API 的标题和描述翻译
- **🎞️ 字幕翻译** - 支持批量字幕翻译，可配置源语言和目标语言
- **🏷️ 标签生成** - AI 自动生成相关标签和关键词
- **🛡️ 内容审核** - 集成阿里云内容安全，自动检测风险内容
- **🎯 分区推荐** - 智能推荐最适合的 AcFun 分区

### 💼 任务管理
- **📋 任务队列** - 完整的任务生命周期管理，支持多种状态
- **👁️ 人工审核** - 支持人工介入审核和调整
- **📈 状态追踪** - 实时任务状态显示（需手动刷新）
- **🔄 批量操作** - 支持批量添加、删除、重试任务
- **📊 任务统计** - 详细的任务执行统计和历史记录

### 🔧 系统特性
- **🍪 Cookie 登录** - 支持 Cookie 文件登录，更稳定可靠
- **🐳 Docker 部署** - 一键容器化部署，支持多架构
- **📝 日志管理** - 详细日志记录，支持自动清理和轮转
- **⚙️ 灵活配置** - Web 界面配置管理，支持热更新
- **🔄 定时任务** - 基于 APScheduler 的任务调度
- **🌐 跨域支持** - 支持浏览器插件跨域访问

## 🚀 快速开始

### 📋 环境要求

- 🐳 **Docker & Docker Compose** (推荐)
- 🐍 **Python 3.10+** (本地部署)
- 🌐 **现代浏览器** (Chrome/Firefox/Edge)
- 🎬 **FFmpeg** (Docker 镜像已包含)

### ⚡ 一键部署

**方式一：使用预构建镜像**
```bash
# 克隆项目
git clone https://github.com/fqscfqj/Y2A-Auto.git
cd Y2A-Auto

# 启动服务
docker-compose up -d

# 访问界面
open http://localhost:5000
```

**方式二：本地构建**
```bash
# 本地构建并部署
docker-compose -f docker-compose-build.yml up -d --build
```

### 🎯 首次配置

1. **🌐 访问 Web 界面**: http://localhost:5000
2. **⚙️ 进入设置页面**: 配置 API 密钥和账号信息
3. **🍪 上传 Cookie 文件**: 配置 AcFun 和 YouTube 登录凭据
4. **🎞️ 配置字幕选项**: 设置字幕翻译语言和处理方式
5. **✅ 测试功能**: 添加第一个测试任务

## 🔧 部署方式

<details>
<summary>📦 Docker 部署 (推荐)</summary>

### 🎯 优势
- ✅ 环境一致性
- ✅ 快速部署
- ✅ 便于维护
- ✅ 内置 FFmpeg

### 📝 步骤
```bash
# 1. 获取项目
git clone https://github.com/fqscfqj/Y2A-Auto.git
cd Y2A-Auto

# 2. 选择部署方式
# 预构建镜像 (推荐)
docker-compose up -d

# 或本地构建
docker-compose -f docker-compose-build.yml up -d --build

# 3. 查看状态
docker-compose ps
docker-compose logs -f app
```

### 🛑 停止服务
```bash
docker-compose down
```
</details>

<details>
<summary>🐍 本地部署</summary>

### 📝 步骤
```bash
# 1. 克隆项目
git clone https://github.com/fqscfqj/Y2A-Auto.git
cd Y2A-Auto

# 2. 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# 3. 安装系统依赖
# Ubuntu/Debian:
sudo apt-get install ffmpeg
# macOS:
brew install ffmpeg
# Windows: 下载 FFmpeg 并配置环境变量

# 4. 安装Python依赖
pip install -r requirements.txt

# 5. 启动应用
python app.py
```
</details>

## 📱 使用指南

### 🖥️ Web 界面操作

#### 📋 任务管理
1. **➕ 添加任务**: 输入 YouTube 视频 URL
2. **▶️ 启动处理**: 开始下载和 AI 处理流程
3. **👁️ 人工审核**: 检查和调整 AI 生成的内容
4. **🚀 上传发布**: 后台上传到 AcFun

#### 🎞️ 字幕处理流程
1. **📥 字幕下载**: 自动下载可用的字幕文件 (.vtt 格式)
2. **🔍 语言检测**: 自动识别字幕源语言
3. **🌐 智能翻译**: 批量翻译字幕内容
4. **🎬 视频嵌入**: 将翻译后的字幕嵌入视频文件
5. **📋 文件保留**: 可选择保留原始字幕文件

#### ⚙️ 系统设置
- **🔑 API 配置**: OpenAI、阿里云等服务密钥
- **🍪 账号管理**: Cookie 文件上传和登录配置
- **🎞️ 字幕设置**: 翻译语言、批次大小、并发数配置
- **📝 日志管理**: 自动清理和手动清空

### 🔌 浏览器插件

#### 📦 安装 Tampermonkey 脚本
1. **🔧 安装扩展**: 在浏览器中安装 [Tampermonkey](https://www.tampermonkey.net/)
2. **📄 导入脚本**: 复制 `PushToY2AAuto.user.js` 内容到新脚本
3. **⚙️ 配置服务器**: 修改脚本中的服务器地址
   ```javascript
   const Y2A_AUTO_SERVER = 'http://localhost:5000';
   ```

#### 🎬 使用方法
1. **🎥 访问 YouTube**: 打开任意 YouTube 视频页面
2. **📤 点击按钮**: 使用"📤 推送到Y2A-Auto"按钮
3. **✅ 确认推送**: 等待推送成功通知

## ⚙️ 配置说明

### 🔧 主要配置项

#### 🤖 AI 配置
```json
{
  "OPENAI_API_KEY": "your-openai-api-key",
  "OPENAI_BASE_URL": "https://api.openai.com/v1",
  "OPENAI_MODEL_NAME": "gpt-3.5-turbo",
  "TRANSLATE_TITLE": true,
  "TRANSLATE_DESCRIPTION": true,
  "GENERATE_TAGS": true,
  "RECOMMEND_PARTITION": true
}
```

#### 🎞️ 字幕翻译配置
```json
{
  "SUBTITLE_TRANSLATION_ENABLED": true,
  "SUBTITLE_SOURCE_LANGUAGE": "auto",
  "SUBTITLE_TARGET_LANGUAGE": "zh",
  "SUBTITLE_API_PROVIDER": "openai",
  "SUBTITLE_BATCH_SIZE": 5,
  "SUBTITLE_MAX_WORKERS": 3,
  "SUBTITLE_EMBED_IN_VIDEO": true,
  "SUBTITLE_KEEP_ORIGINAL": true
}
```

#### 🛡️ 内容审核配置
```json
{
  "CONTENT_MODERATION_ENABLED": true,
  "ALIYUN_ACCESS_KEY_ID": "your-access-key-id",
  "ALIYUN_ACCESS_KEY_SECRET": "your-access-key-secret",
  "ALIYUN_CONTENT_MODERATION_REGION": "cn-shanghai",
  "ALIYUN_TEXT_MODERATION_SERVICE": "comment_detection_pro"
}
```

#### 📝 日志管理配置
```json
{
  "LOG_CLEANUP_ENABLED": true,
  "LOG_CLEANUP_HOURS": 168,
  "LOG_CLEANUP_INTERVAL": 12
}
```

### 🍪 Cookie 配置指南

### 🎯 为什么使用 Cookie？
- ✅ **更稳定的登录** - 避免验证码和登录限制
- ✅ **支持高级功能** - 双因素认证等
- ✅ **会话持久化** - 自动维护登录状态

### 📥 获取 Cookie 文件

<details>
<summary>🔧 方法一：浏览器插件 (推荐)</summary>

1. **📦 安装插件**: 安装 "Get cookies.txt" 或类似插件
2. **🔐 登录网站**: 
   - AcFun: https://www.acfun.cn
   - YouTube: https://www.youtube.com
3. **📄 导出文件**: 使用插件导出 Cookie 文件
</details>

<details>
<summary>🛠️ 方法二：手动提取</summary>

1. **🔐 登录网站**: 登录目标网站
2. **🔍 打开开发者工具**: 按 F12
3. **📂 找到 Cookies**: Application → Storage → Cookies
4. **📋 复制信息**: 复制所有 Cookie 数据
</details>

### 📁 文件格式

**Netscape 格式 (推荐)**
```
# Netscape HTTP Cookie File
.acfun.cn	TRUE	/	FALSE	1234567890	cookie_name	cookie_value
```

**JSON 格式**
```json
[{"name": "cookie_name", "value": "cookie_value", "domain": ".acfun.cn"}]
```

### ⚙️ 配置方式

**🌐 Web 界面配置 (推荐)**
1. 进入"系统设置"页面
2. 在相应账号设置部分上传 Cookie 文件

**📁 手动文件配置**
```
Y2A-Auto/
├── cookies/
│   ├── ac_cookies.txt      # AcFun Cookie
│   └── yt_cookies.txt      # YouTube Cookie
```

## 📝 日志管理

### 🔄 自动清理
- **⏰ 定时清理**: 配置保留天数和清理间隔
- **📊 智能管理**: 按文件大小和时间自动清理
- **🔄 轮转机制**: 使用 RotatingFileHandler 防止单个日志文件过大

### 🧹 手动清理
- **🗑️ 立即清空**: 一键清空所有日志文件
- **📋 详细反馈**: 显示清理的文件数量和释放空间
- **🎨 改进界面**: 页面内确认，无弹窗干扰

## 📊 系统架构

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   🎬 YouTube     │    │   🤖 Y2A-Auto    │    │   🎪 AcFun       │
│                 │    │                 │    │                 │
│   📹 视频源      │─▶─│   📥 下载处理    │─▶─│   🚀 自动发布    │
│   🔗 视频链接    │    │   🎞️ 字幕翻译    │    │   📊 数据统计    │
│   🎞️ 字幕文件    │    │   🧠 AI 增强     │    └─────────────────┘
└─────────────────┘    │   👁️ 人工审核    │
                       │   📋 任务管理    │
                       └─────────────────┘
                               │
                       ┌─────────────────┐
                       │   🌐 Flask Web   │
                       │   📱 浏览器插件   │
                       │   🔄 定时任务    │
                       └─────────────────┘
```

## 🏗️ 技术栈

### 🐍 后端技术
- **Flask 2.3+** - Web 框架
- **yt-dlp ≥2025.5.22** - YouTube 下载器
- **OpenAI ≥1.0.0** - AI 翻译和增强
- **APScheduler 3.10+** - 任务调度
- **Pillow 10.0** - 图像处理
- **FFmpeg** - 视频处理

### 🌐 前端技术
- **Bootstrap 5** - UI 框架
- **JavaScript ES6+** - 交互逻辑
- **Font Awesome** - 图标库

### 🗄️ 数据存储
- **SQLite** - 本地数据库
- **JSON** - 配置文件格式
- **文件系统** - 媒体文件存储

### 🔧 部署工具
- **Docker** - 容器化部署
- **Docker Compose** - 多容器编排
- **Python venv** - 虚拟环境

## 📂 项目结构

```
Y2A-Auto/
├── 📁 modules/             # 核心功能模块
│   ├── 🎬 youtube_handler.py      # YouTube 视频处理
│   ├── 📤 acfun_uploader.py       # AcFun 上传器
│   ├── 🎞️ subtitle_translator.py  # 字幕翻译器
│   ├── 🤖 ai_enhancer.py          # AI 增强功能
│   ├── 🛡️ content_moderator.py    # 内容审核
│   ├── 📋 task_manager.py         # 任务管理器
│   ├── ⚙️ config_manager.py       # 配置管理
│   └── 🔧 utils.py               # 工具函数
├── 📁 templates/           # HTML 模板
├── 📁 static/              # 静态资源
│   ├── 🎨 css/                   # 样式文件
│   ├── 📜 js/                    # JavaScript 文件
│   ├── 🖼️ img/                   # 图片资源
│   └── 📚 lib/                   # 第三方库
├── 📁 acfunid/             # AcFun 分区映射
├── 📁 config/              # 配置文件
├── 📁 cookies/             # Cookie 文件
├── 📁 db/                  # 数据库文件
├── 📁 downloads/           # 下载文件
├── 📁 logs/                # 日志文件
├── 📁 temp/                # 临时文件
├── 🐳 docker-compose.yml   # Docker 配置
├── 🐳 Dockerfile           # Docker 镜像
├── 🐍 app.py               # Flask 主应用
├── 📋 requirements.txt     # Python 依赖
└── 🔧 PushToY2AAuto.user.js # 浏览器脚本
```

## 🔌 API 接口

### 📋 任务管理接口
- `POST /tasks/add` - 添加新任务
- `POST /tasks/{id}/start` - 启动任务
- `POST /tasks/{id}/delete` - 删除任务
- `POST /tasks/{id}/force_upload` - 强制上传
- `POST /tasks/add_via_extension` - 浏览器插件推送

### ⚙️ 系统管理接口
- `GET/POST /settings` - 系统配置管理
- `POST /maintenance/cleanup_logs` - 清理日志
- `POST /maintenance/clear_logs` - 清空日志

### 📊 状态查询接口
- `GET /tasks` - 获取任务列表
- `GET /manual_review` - 获取待审核任务
- `GET /covers/{task_id}` - 获取任务封面

## ❓ 常见问题

<details>
<summary>🎞️ 字幕翻译相关问题</summary>

**Q: 字幕翻译失败怎么办？**
- 检查 OpenAI API 密钥是否正确
- 确认网络连接正常
- 查看源语言设置是否正确
- 检查字幕文件是否存在

**Q: 如何提高字幕翻译质量？**
- 使用更高级的 OpenAI 模型 (如 gpt-4)
- 调整批次大小，减少单次翻译的文本量
- 确保源语言设置准确
</details>

<details>
<summary>🍪 Cookie 过期怎么办？</summary>

**解决方案**:
- 重新获取 Cookie 文件并上传
- 或使用备用的用户名密码登录
- 建议定期更新 Cookie 以确保稳定性
- 可以同时配置多种登录方式作为备用
</details>

<details>
<summary>📤 上传失败怎么办？</summary>

**检查清单**:
1. ✅ Cookie 文件格式是否正确
2. ✅ 网络连接是否正常  
3. ✅ 服务器地址是否正确
4. ✅ 防火墙设置是否阻止连接
5. ✅ Cookie 是否已过期
6. ✅ 视频文件是否完整
7. ✅ AcFun 服务器是否正常
</details>

<details>
<summary>📊 如何查看任务进度？</summary>

**操作方法**:
- 点击浏览器刷新按钮 (F5) 查看最新状态
- 查看日志文件了解详细处理过程
- 在任务列表页面监控状态变化
- 查看任务详情页面了解具体进度
</details>

<details>
<summary>🐳 Docker 部署失败？</summary>

**排查步骤**:
1. 检查 Docker 和 Docker Compose 版本
2. 确认端口 5000 未被占用
3. 查看容器日志: `docker-compose logs -f`
4. 重新构建: `docker-compose up -d --build`
5. 检查磁盘空间是否充足
6. 确认网络连接正常
</details>

<details>
<summary>🔧 浏览器脚本不工作？</summary>

**解决方案**:
- 确认 Tampermonkey 已启用脚本
- 检查服务器地址是否正确
- 确认 Y2A-Auto 服务正在运行
- 在 YouTube 视频页面刷新重试
- 检查浏览器控制台是否有错误信息
</details>

<details>
<summary>🤖 AI 功能不可用？</summary>

**解决方案**:
- 检查 OpenAI API 密钥配置
- 确认 API 余额是否充足
- 测试网络连接到 OpenAI 服务器
- 检查 API 基础 URL 设置
- 查看相关日志文件了解详细错误
</details>

<details>
<summary>⚡ 系统性能优化？</summary>

**优化建议**:
- 调整字幕翻译并发数
- 配置日志自动清理
- 定期清理下载文件
- 监控系统资源使用情况
- 使用 SSD 存储提高 I/O 性能
</details>

## 🔒 安全提示

> ⚠️ **重要提醒**

- 🔐 **保护 Cookie 文件**: 包含敏感登录信息，请妥善保管
- 🔄 **定期更新凭据**: 建议定期更新 Cookie 和密码
- 💾 **备份重要数据**: 建议同时配置多种登录方式
- 🛡️ **网络安全**: 在受信任的网络环境中使用
- 🔑 **API 密钥管理**: 妥善保管各种 API 密钥，避免泄露
- 📝 **日志隐私**: 定期清理日志文件，避免敏感信息泄露

## 🚀 未来规划

- 🎥 **更多视频平台支持** - 支持 Bilibili、抖音等平台
- 🌐 **多语言界面** - 支持英文、日文等界面语言
- 📱 **移动端适配** - 响应式设计，支持手机访问
- 🔄 **增量更新** - 支持视频更新和版本管理
- 📊 **数据分析** - 视频表现统计和分析功能
- 🎛️ **高级调度** - 更灵活的任务调度和批处理
- 🔌 **插件系统** - 支持第三方插件扩展

## 📄 许可证

本项目基于 [GNU General Public License v3.0](LICENSE) 开源协议。

---

<div align="center">

**🎉 享受自动化的乐趣！**

如果这个项目对您有帮助，请考虑给它一个 ⭐

[📚 查看文档](README.md) • [🐛 报告问题](../../issues) • [💡 功能建议](../../issues)

</div>