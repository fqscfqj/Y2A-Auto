# 浏览器扩展脚本

本目录包含Y2A-Auto的浏览器扩展脚本，用于增强YouTube使用体验。

## 脚本列表

| 脚本 | 功能 | 状态 |
|------|------|------|
| [Y2A-Auto-Cookie-Sync.user.js](Y2A-Auto-Cookie-Sync.user.js) | 自动同步YouTube cookies | ✅ 可用 |
| [PushToY2AAuto.user.js](PushToY2AAuto.user.js) | 一键推送YouTube视频 | ✅ 可用 |

## 快速开始

### 安装Tampermonkey

选择适合的浏览器扩展：

- [Chrome](https://chrome.google.com/webstore/detail/tampermonkey/dhdgffkkebhmkfjojejmpbldmpobfkfo)
- [Firefox](https://addons.mozilla.org/firefox/addon/tampermonkey/)
- [Edge](https://microsoftedge.microsoft.com/addons/detail/tampermonkey/iikmkjmpaadaobahmlepeloendndfphd)

### 安装脚本

1. 复制脚本文件内容
2. 在Tampermonkey中创建新脚本
3. 粘贴内容并保存

### 配置服务器

在脚本中修改服务器地址：

```javascript
const Y2A_AUTO_SERVER = 'http://localhost:5000';
```

## 脚本说明

### Y2A-Auto-Cookie-Sync.user.js

自动Cookie同步脚本

- 功能：自动同步YouTube cookies到Y2A-Auto服务器
- 用途：保持认证状态，避免下载失败
- 文档：[详细说明](../docs/userscripts/Cookie-Sync-README.md)

### PushToY2AAuto.user.js

视频推送脚本

- 功能：在YouTube页面添加推送按钮
- 用途：一键将视频发送到处理队列
- 文档：[详细说明](../docs/userscripts/PushTo-README.md)

## 故障排除

### 常见问题

#### 脚本未启用

- 检查Tampermonkey图标是否为绿色
- 刷新YouTube页面

#### 连接失败

- 确认Y2A-Auto服务运行正常
- 检查脚本中的服务器地址配置

#### 功能异常

- 查看浏览器控制台错误信息
- 检查脚本权限设置

### 调试方法

1. 打开浏览器开发工具（F12）
2. 查看Console标签页
3. 寻找脚本相关的错误信息

## 更新

脚本更新请重新下载并替换原文件。

## 许可证

遵循Y2A-Auto项目许可证。
