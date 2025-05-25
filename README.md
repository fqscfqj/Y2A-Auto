<div align="center">

# 🎬 Y2A-Auto

**YouTube to AcFun 自动化工具**

*一键从 YouTube 搬运视频到 AcFun，支持 AI 翻译、内容审核、智能标签生成*

[![License](https://img.shields.io/badge/license-GPL%20v3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-green.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-supported-blue.svg)](https://www.docker.com/)
[![Status](https://img.shields.io/badge/status-stable-brightgreen.svg)]()

[🚀 快速开始](#-快速开始) • [📖 功能特性](#-功能特性) • [🔧 部署方式](#-部署方式) • [📱 使用指南](#-使用指南) • [❓ 常见问题](#-常见问题)

---

</div>

## 📖 项目简介

Y2A-Auto 是一个现代化的 YouTube 到 AcFun 视频搬运自动化工具，提供完整的端到端解决方案：

🎯 **核心价值**
- 🤖 **全自动化流程** - 从 YouTube 下载到 AcFun 发布一站式完成
- 🧠 **AI 智能处理** - 自动翻译、标签生成、内容审核
- 🎨 **友好的 Web 界面** - 直观的任务管理和状态监控
- 📱 **浏览器集成** - 在 YouTube 页面一键推送视频

## ✨ 功能特性

### 🎥 视频处理
- **📥 智能下载** - 使用最新 yt-dlp，支持多格式回退机制
- **🖼️ 封面处理** - 自动裁剪/填充适配 AcFun 封面规格
- **📊 格式优化** - 自动选择最佳视频质量和格式

### 🤖 AI 增强
- **🌐 智能翻译** - 基于 OpenAI API 的标题和描述翻译
- **🏷️ 标签生成** - AI 自动生成相关标签和关键词
- **🛡️ 内容审核** - 集成阿里云内容安全，自动检测风险内容
- **🎯 分区推荐** - 智能推荐最适合的 AcFun 分区

### 💼 任务管理
- **📋 任务队列** - 完整的任务生命周期管理
- **👁️ 人工审核** - 支持人工介入审核和调整
- **📈 状态追踪** - 实时任务状态显示（需手动刷新）
- **🔄 批量操作** - 支持批量添加、删除、重试任务

### 🔧 系统特性
- **🍪 Cookie 登录** - 支持 Cookie 文件登录，更稳定可靠
- **🐳 Docker 部署** - 一键容器化部署
- **📝 日志管理** - 详细日志记录，支持自动清理
- **⚙️ 灵活配置** - Web 界面配置管理

## 🚀 快速开始

### 📋 环境要求

- 🐳 **Docker & Docker Compose** (推荐)
- 🐍 **Python 3.10+** (本地部署)
- 🌐 **现代浏览器** (Chrome/Firefox/Edge)

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
4. **✅ 测试功能**: 添加第一个测试任务

## 🔧 部署方式

<details>
<summary>📦 Docker 部署 (推荐)</summary>

### 🎯 优势
- ✅ 环境一致性
- ✅ 快速部署
- ✅ 便于维护

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

# 3. 安装依赖
pip install -r requirements.txt

# 4. 启动应用
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

#### ⚙️ 系统设置
- **🔑 API 配置**: OpenAI、阿里云等服务密钥
- **🍪 账号管理**: Cookie 文件上传和登录配置
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

## 🍪 Cookie 配置指南

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
│   🔗 视频链接    │    │   🧠 AI 增强     │    │   📊 数据统计    │
└─────────────────┘    │   👁️ 人工审核    │    └─────────────────┘
                       │   📋 任务管理    │
                       └─────────────────┘
                               │
                       ┌─────────────────┐
                       │   🌐 Web 界面    │
                       │   📱 浏览器插件   │
                       └─────────────────┘
```

## 📂 项目结构

```
Y2A-Auto/
├── 📁 acfunid/             # AcFun 分区映射
├── 📁 config/              # 配置文件
├── 📁 cookies/             # Cookie 文件
│   ├── 🍪 ac_cookies.txt
│   └── 🍪 yt_cookies.txt
├── 📁 db/                  # 数据库文件
├── 📁 downloads/           # 下载文件
├── 📁 logs/                # 日志文件
├── 📁 modules/             # Python 模块
├── 📁 static/              # 静态资源
├── 📁 templates/           # HTML 模板
├── 📁 temp/                # 临时文件
├── 🐳 docker-compose.yml   # Docker 配置
├── 🐳 Dockerfile           # Docker 镜像
├── 🐍 app.py               # 主应用
├── 📋 requirements.txt     # Python 依赖
└── 🔧 PushToY2AAuto.user.js # 浏览器脚本
```

## ❓ 常见问题

<details>
<summary>🍪 Cookie 过期怎么办？</summary>

**解决方案**:
- 重新获取 Cookie 文件并上传
- 或使用备用的用户名密码登录
- 建议定期更新 Cookie 以确保稳定性
</details>

<details>
<summary>📤 上传失败怎么办？</summary>

**检查清单**:
1. ✅ Cookie 文件格式是否正确
2. ✅ 网络连接是否正常  
3. ✅ 服务器地址是否正确
4. ✅ 防火墙设置是否阻止连接
5. ✅ Cookie 是否已过期
</details>

<details>
<summary>📊 如何查看任务进度？</summary>

**操作方法**:
- 点击浏览器刷新按钮 (F5) 查看最新状态
- 查看日志文件了解详细处理过程
- 在任务列表页面监控状态变化
</details>

<details>
<summary>🐳 Docker 部署失败？</summary>

**排查步骤**:
1. 检查 Docker 和 Docker Compose 版本
2. 确认端口 5000 未被占用
3. 查看容器日志: `docker-compose logs -f`
4. 重新构建: `docker-compose up -d --build`
</details>

<details>
<summary>🔧 浏览器脚本不工作？</summary>

**解决方案**:
- 确认 Tampermonkey 已启用脚本
- 检查服务器地址是否正确
- 确认 Y2A-Auto 服务正在运行
- 在 YouTube 视频页面刷新重试
</details>

## 🔒 安全提示

> ⚠️ **重要提醒**

- 🔐 **保护 Cookie 文件**: 包含敏感登录信息，请妥善保管
- 🔄 **定期更新凭据**: 建议定期更新 Cookie 和密码
- 💾 **备份重要数据**: 建议同时配置多种登录方式
- 🛡️ **网络安全**: 在受信任的网络环境中使用

## 📄 许可证

本项目基于 [GNU General Public License v3.0](LICENSE) 开源协议。

---

<div align="center">

**🎉 享受自动化的乐趣！**

如果这个项目对您有帮助，请考虑给它一个 ⭐

[📚 查看文档](README.md) • [🐛 报告问题](../../issues) • [💡 功能建议](../../issues)

</div>