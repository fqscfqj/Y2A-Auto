# Y2A-Auto

## 简介

Y2A-Auto (YouTube to AcFun Auto) 是一个自动化工具，旨在简化从 YouTube 下载视频、进行一系列智能处理（包括AI翻译、内容审核、标签生成等），并最终发布到 AcFun 弹幕视频网的过程。它提供了一个Web界面来管理任务，并支持通过浏览器用户脚本从 YouTube 页面直接推送视频进行处理。

## 主要特性

- **自动化视频处理流程**: 从YouTube视频URL到AcFun发布的端到端自动化。
- **Web任务管理界面**:
    - 查看任务列表及其详细状态（等待、下载中、处理中、审核、上传中、完成、失败）。
    - 手动添加、编辑、删除、重试任务。
    - 查看任务处理详情，包括AI生成的标签、翻译建议、审核结果。
    - 手动审核和调整待发布内容。
- **YouTube集成**:
    - 通过 Tampermonkey 用户脚本在 YouTube 观看页面添加 "推送到Y2A-Auto" 按钮，一键提交任务。
- **智能处理**:
    - **视频下载**: 使用 `yt-dlp` 从 YouTube 下载视频和封面。
    - **AI 内容生成与辅助**:
        - 可能利用 OpenAI API 进行标题/简介翻译、内容摘要、关键词/标签生成。
    - **内容审核**: 集成阿里云内容安全 (Green) 服务，对视频内容进行自动审核。
    - **封面处理**: 使用 Pillow 处理封面图片。
- **AcFun发布**:
    - 自动或手动选择 AcFun 分区。
    - 自动上传处理完成的视频到 AcFun。
- **配置灵活**:
    - 通过 Web 界面或配置文件管理应用设置。
    - 支持 `.env` 文件配置敏感信息 (如API密钥)。
- **实时通知**: 通过 WebSocket 向前端实时推送任务状态更新。
- **后台任务调度**: 使用 APScheduler 进行如日志清理等后台任务。
- **Docker化部署**: 提供 `Dockerfile` 和 `docker-compose.yml` 方便快速部署和环境一致性。
- **日志系统**: 详细的日志记录，方便追踪和排错。

## 技术栈

- **后端**: Python, Flask, Flask-SocketIO, eventlet
- **前端**: HTML, CSS, JavaScript (使用 Jinja2 模板引擎)
- **视频下载**: yt-dlp
- **图像处理**: Pillow
- **AI 服务**:
    - OpenAI API (用于文本生成、翻译等)
    - 阿里云内容安全 (Green) SDK (用于内容审核)
- **任务调度**: APScheduler
- **数据库/存储**: (可能基于文件系统或简单的数据库，如SQLite，具体见 `db/` 目录)
- **部署**: Docker, Docker Compose
- **浏览器脚本**: Tampermonkey (JavaScript)

## 模块/组件说明

- **`app.py`**: Flask应用主文件，包含路由、WebSocket处理、任务调度初始化等。
- **`modules/`**:
    - **`youtube_handler.py`**: 处理YouTube视频下载逻辑。
    - **`task_manager.py`**: 核心任务管理模块，包括任务状态机、数据库交互等。
    - **`config_manager.py`**: 加载和管理应用程序配置。
    - **`utils.py`**: 通用工具函数。
    - *(其他可能的模块，如 `ai_handler.py` 或 `acfun_uploader.py`，根据具体实现)*
- **`static/`**: 存放CSS、JavaScript、图片等静态资源。
- **`templates/`**: 存放Flask应用的HTML模板。
- **`acfunid/`**: 存放AcFun分区ID与名称的映射文件 (`id_mapping.json`)。
- **`config/`**: 存放应用配置文件 (如 `config.json`)。
- **`db/`**: 存放应用数据库文件 (如 `tasks.json` 或 SQLite 数据库)。
- **`downloads/`**: 存放下载的原始视频和相关素材。
- **`logs/`**: 存放应用运行日志。
- **`temp/`**: 存放临时文件。
- **`PushToY2AAuto.user.js`**: Tampermonkey 用户脚本，用于从 YouTube 页面推送视频。

## 部署

### 先决条件

- Docker 和 Docker Compose
- Python 3.10 (如果本地运行或开发)
- 一个支持 Tampermonkey 的浏览器 (如 Chrome, Firefox, Edge) 用于安装用户脚本。
- OpenAI API 密钥和阿里云相关服务的访问凭证 (如果使用这些AI功能)。

### 配置

1.  **环境变量**:
    项目可能使用 `.env` 文件来管理敏感配置（如API密钥）。如果存在 `config/example.env` 或类似文件，请复制为 `config/.env` 并填入您的凭证。
    常见的配置项可能包括：
    - `OPENAI_API_KEY`
    - `ALIYUN_ACCESS_KEY_ID`
    - `ALIYUN_ACCESS_KEY_SECRET`
    *(具体请参照 `modules/config_manager.py` 或相关文档)*

2.  **应用配置 (`config/config.json`)**:
    应用的主要配置（如默认下载路径、AcFun账户信息等）通常存储在 `config/config.json` 中。首次运行时可能会生成默认配置，或需要手动创建。

### 使用 Docker 运行

这是推荐的部署方式。

1.  **克隆仓库**:
    ```bash
    git clone <your-repository-url>
    cd Y2A-Auto
    ```

2.  **准备配置文件**:
    - 确保 `config/` 目录下有必要的配置文件。如果需要，创建 `.env` 文件并放入 `config/` 目录，或根据 `docker-compose.yml` 的volumes映射进行调整。
    - 确保 `acfunid/id_mapping.json` 文件存在且正确。

3.  **构建并启动服务**:
    ```bash
    docker-compose up -d --build
    ```
    服务将在后台启动。

4.  **访问应用**:
    打开浏览器，访问 `http://localhost:5000`。

5.  **查看日志**:
    ```bash
    docker-compose logs -f app
    ```

6.  **停止服务**:
    ```bash
    docker-compose down
    ```

## 使用方法

### 1. Web 界面

-   **访问**: 部署成功后，通过浏览器访问 `http://<your-server-ip-or-domain>:5000` (本地部署默认为 `http://localhost:5000`)。
-   **任务管理**:
    -   在首页或 "任务列表" 页面查看所有任务。
    -   通过 "添加任务" 按钮手动输入 YouTube 视频 URL 添加新任务。
    -   对任务进行编辑、启动、删除、强制上传等操作。
    -   进入 "手动审核" 页面处理需要人工介入的任务。
-   **设置**: 在 "设置" 页面配置应用参数。

### 2. 浏览器用户脚本 (Tampermonkey)

1.  **安装 Tampermonkey**: 在您的浏览器 (Chrome, Firefox, Edge 等) 中安装 Tampermonkey 扩展。
2.  **安装用户脚本**:
    -   打开 Tampermonkey 管理面板。
    -   选择 "新建脚本" 或 "从文件导入"。
    -   将 `PushToY2AAuto.user.js` 文件的内容复制粘贴进去，或者直接导入该文件。
    -   **重要**: 检查脚本中的 `Y2A_AUTO_SERVER` 常量，确保它指向您 Y2A-Auto 服务的正确地址 (默认为 `http://localhost:5000`)。如果您的服务部署在其他地址或域名，请修改此常量。
    ```javascript
    // Y2A-Auto服务器地址，可根据实际部署情况修改
    const Y2A_AUTO_SERVER = 'http://your-y2a-auto-server-address:port';
    ```
    -   保存脚本。
3.  **使用**:
    -   打开任何一个 YouTube 视频观看页面。
    -   您应该会在视频标题下方或操作按钮区域看到一个 "推送到Y2A-Auto" 的按钮。
    -   点击该按钮，视频链接将自动发送到 Y2A-Auto 后台进行处理。
    -   页面会显示推送状态的通知。

## 目录结构简述

```
Y2A-Auto/
├── .venv/                  # Python虚拟环境 (本地开发)
├── acfunid/                # AcFun分区ID映射文件
│   └── id_mapping.json
├── config/                 # 应用配置文件目录 (如 config.json, .env)
├── db/                     # 数据库文件目录 (如 tasks.json)
├── downloads/              # 下载的视频和素材
├── logs/                   # 应用日志
├── modules/                # Python模块
│   ├── __init__.py
│   ├── config_manager.py
│   ├── task_manager.py
│   ├── utils.py
│   └── youtube_handler.py
├── static/                 # Web静态资源 (CSS, JS, 图片)
│   ├── covers/             # 上传视频的封面
│   ├── css/
│   ├── img/
│   └── js/
├── temp/                   # 临时文件目录
├── templates/              # Flask HTML模板
├── .dockerignore           # Docker构建时忽略的文件
├── .gitignore              # Git忽略的文件
├── app.py                  # Flask应用入口
├── docker-compose.yml      # Docker Compose配置文件
├── Dockerfile              # Docker镜像构建文件
├── LICENSE.txt             # 项目许可证
├── PushToY2AAuto.user.js   # Tampermonkey用户脚本
├── README.md               # 本文档
└── requirements.txt        # Python依赖列表
```

## 注意事项

-   确保所有依赖的 API 密钥和访问凭证都已正确配置，并且账户有足够的配额。
-   网络连接对于视频下载和 API 调用至关重要。
-   部分视频由于版权或地区限制可能无法下载。
-   AcFun的上传策略和限制可能会影响发布结果。

## 许可证

本项目使用 **GNU General Public License v3.0 (GPLv3)** 协议。