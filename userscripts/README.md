# 浏览器扩展和脚本

本目录包含Y2A-Auto的浏览器扩展和用户脚本，用于增强YouTube使用体验。

## 扩展和脚本列表

| 类型 | 名称 | 功能 | 状态 |
|------|------|------|------|
| 🔥 浏览器扩展 | [browser-extension/](browser-extension/) | Cookie同步 + 视频推送一体化 | ✅ 推荐 |
| 📤 用户脚本 | [PushToY2AAuto.user.js](PushToY2AAuto.user.js) | 一键推送YouTube视频 | ✅ 可用 |

## 快速开始

### 方式一：浏览器扩展（推荐）

**优势**：可访问HttpOnly Cookie，更稳定的运行机制

1. **加载扩展到浏览器**
   - 打开浏览器扩展管理页面
   - 启用"开发者模式"
   - 点击"加载已解压的扩展程序"
   - 选择 `browser-extension/` 目录

2. **配置服务器地址**
   - 编辑 `browser-extension/background.js`
   ```javascript
   const Y2A_AUTO_SERVER = 'http://localhost:5000'; // 修改为实际地址
   ```

### 方式二：用户脚本

**要求**：需要安装Tampermonkey扩展

1. **安装Tampermonkey**

   选择适合的浏览器扩展：

   - [Chrome](https://chrome.google.com/webstore/detail/tampermonkey/dhdgffkkebhmkfjojejmpbldmpobfkfo)
   - [Firefox](https://addons.mozilla.org/firefox/addon/tampermonkey/)
   - [Edge](https://microsoftedge.microsoft.com/addons/detail/tampermonkey/iikmkjmpaadaobahmlepeloendndfphd)

2. **安装脚本**
   - 复制 `PushToY2AAuto.user.js` 文件内容
   - 在Tampermonkey中创建新脚本
   - 粘贴内容并保存

3. **配置服务器地址**

   ```javascript
   const Y2A_AUTO_SERVER = 'http://localhost:5000'; // 修改为实际地址
   ```

## 功能说明

### 🔥 Y2A-Auto 浏览器扩展（推荐）

**完整的一体化解决方案**

**主要功能**：
- 🍪 **自动Cookie同步** - 可访问包括HttpOnly在内的所有认证Cookie
- 📤 **一键视频推送** - 在YouTube页面直接添加视频到处理队列  
- 🔄 **后台自动同步** - 定时同步Cookie，确保认证状态有效
- 📊 **实时状态显示** - 可视化同步状态和操作反馈
- ⚙️ **灵活配置** - 支持自定义服务器地址和同步间隔

**技术优势**：
- ✅ 可访问HttpOnly Cookie（用户脚本无法实现）
- ✅ 更稳定的后台运行机制
- ✅ 更好的YouTube页面集成
- ✅ 无需安装额外的脚本管理器

### 📤 PushToY2AAuto.user.js

**轻量级视频推送脚本**

- **功能**：在YouTube页面添加推送按钮
- **用途**：一键将视频发送到处理队列
- **文档**：[详细说明](../docs/userscripts/PushTo-README.md)

## 故障排除

### 浏览器扩展问题

#### 扩展未生效

- 确认扩展已正确加载并启用
- 检查扩展图标是否显示在工具栏中
- 刷新YouTube页面

#### Cookie同步失败

- 确认Y2A-Auto服务运行正常
- 检查 `background.js` 中的服务器地址配置
- 查看扩展的控制台错误信息

### 用户脚本问题

#### 脚本未启用

- 检查Tampermonkey图标是否为绿色
- 刷新YouTube页面

#### 连接失败

- 确认Y2A-Auto服务运行正常
- 检查脚本中的服务器地址配置

### 调试方法

#### 浏览器扩展调试

1. 打开扩展管理页面
2. 点击扩展的"详细信息"
3. 点击"检查视图"下的背景页面
4. 查看Console错误信息

#### 用户脚本调试

1. 打开浏览器开发工具（F12）
2. 查看Console标签页
3. 寻找脚本相关的错误信息

## 更新维护

### 扩展更新

直接替换 `browser-extension/` 目录中的文件即可。

### 脚本更新

脚本更新请重新下载并替换原文件。

## 许可证

遵循Y2A-Auto项目许可证。
