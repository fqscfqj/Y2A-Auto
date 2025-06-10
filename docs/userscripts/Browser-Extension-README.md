# Y2A-Auto 浏览器扩展使用指南

Y2A-Auto官方浏览器扩展，提供Cookie同步和视频推送的一体化解决方案。

## 概述

该扩展完全替代了原有的用户脚本，提供更稳定、更强大的功能。最重要的是，它可以访问包括HttpOnly在内的所有YouTube认证Cookie，这是用户脚本无法实现的关键功能。

## 主要功能

### 🍪 自动Cookie同步
- **HttpOnly Cookie访问** - 可获取用户脚本无法访问的关键认证Cookie
- **后台自动同步** - 定时自动同步，无需用户干预
- **变化检测** - 智能检测Cookie变化并及时同步
- **错误重试** - 同步失败时自动重试机制

### 📤 一键视频推送
- **页面集成** - 直接在YouTube页面添加推送按钮
- **实时反馈** - 推送状态实时显示
- **智能检测** - 自动适配YouTube界面变化
- **元数据提取** - 自动提取视频标题、描述等信息

### 📊 状态指示器
- **实时状态** - 可视化显示同步状态
- **操作反馈** - 用户操作后的即时反馈
- **错误提示** - 清晰的错误信息显示

## 安装配置

### 系统要求

- **浏览器支持**：Chrome 88+、Firefox 86+、Edge 88+
- **Y2A-Auto服务器**：确保服务器正常运行
- **网络连接**：扩展需要与服务器通信

### 安装步骤

#### 1. 获取扩展文件

确保你有完整的扩展目录：
```
userscripts/browser-extension/
├── manifest.json    # 扩展清单文件
├── background.js    # 后台服务脚本
└── content.js       # 内容脚本
```

#### 2. 加载到浏览器

##### Chrome/Edge

1. 打开浏览器，输入 `chrome://extensions/` 或 `edge://extensions/`
2. 打开右上角的"开发者模式"开关
3. 点击"加载已解压的扩展程序"
4. 选择 `userscripts/browser-extension/` 目录
5. 确认扩展已成功加载

##### Firefox

1. 打开浏览器，输入 `about:debugging`
2. 点击"此Firefox"
3. 点击"临时载入附加组件"
4. 选择 `browser-extension/manifest.json` 文件
5. 确认扩展已成功加载

#### 3. 配置服务器地址

编辑 `background.js` 文件，修改服务器配置：

```javascript
// 服务器配置
const Y2A_AUTO_SERVER = 'http://localhost:5000'; // 修改为你的服务器地址

// 可选：同步间隔配置（分钟）
const SYNC_INTERVAL_MINUTES = 5; // 默认5分钟同步一次
```

#### 4. 重新加载扩展

修改配置后，在扩展管理页面点击"重新加载"按钮。

## 使用方法

### Cookie同步

#### 自动同步

扩展安装成功后会自动开始Cookie同步：

1. **初次同步**：扩展启动后立即执行第一次同步
2. **定时同步**：按配置的间隔（默认5分钟）自动同步
3. **变化同步**：检测到Cookie变化时触发同步

#### 查看同步状态

同步状态会在YouTube页面显示：

| 状态指示 | 含义 | 说明 |
|----------|------|------|
| 🟢 已同步 | Cookie已成功同步 | 认证状态正常 |
| 🔵 同步中 | 正在执行同步操作 | 请稍等同步完成 |
| 🟡 待同步 | 检测到变化等待同步 | 即将开始同步 |
| 🔴 同步失败 | 同步过程出现错误 | 需要检查配置或网络 |

### 视频推送

#### 推送按钮位置

扩展会在YouTube页面的多个位置添加推送按钮：

1. **视频标题下方** - 主要推送按钮位置
2. **视频描述区域** - 备用按钮位置
3. **浮动按钮** - 当标准位置不可用时显示

#### 推送操作

1. 打开YouTube视频页面
2. 找到"推送到Y2A-Auto"按钮
3. 点击按钮执行推送
4. 查看推送状态反馈

#### 推送反馈

- ✅ **推送成功** - 视频已添加到处理队列
- ❌ **推送失败** - 检查服务器连接或视频URL
- ⏳ **推送中** - 正在发送请求到服务器

## 技术优势

### 与用户脚本对比

| 功能特性 | 浏览器扩展 | 用户脚本 |
|----------|------------|----------|
| HttpOnly Cookie访问 | ✅ 支持 | ❌ 不支持 |
| 后台运行稳定性 | ✅ 高 | ⚠️ 中等 |
| 页面集成能力 | ✅ 强 | ⚠️ 一般 |
| 安装复杂度 | ⚠️ 中等 | ✅ 简单 |
| 更新维护 | ✅ 方便 | ⚠️ 需要手动 |

### 关键技术实现

#### Cookie访问机制

```javascript
// 扩展可以访问所有Cookie类型
chrome.cookies.getAll({
  domain: '.youtube.com'
}, (cookies) => {
  // 包括HttpOnly Cookie在内的所有Cookie
  cookies.forEach(cookie => {
    if (isAuthCookie(cookie.name)) {
      // 处理认证Cookie
    }
  });
});
```

#### 后台服务机制

- **Service Worker** - 持续运行的后台脚本
- **消息传递** - 后台脚本与内容脚本的通信
- **定时任务** - 可靠的定时同步机制

## API接口

### Cookie同步接口

扩展使用以下API与服务器通信：

```http
POST /api/cookies/sync
Content-Type: application/json

{
  "source": "youtube",
  "cookies": {
    "__Secure-1PSID": "value",
    "__Secure-3PSID": "value",
    "HSID": "value",
    "SSID": "value",
    "SIDCC": "value",
    "__Secure-1PSIDCC": "value",
    "__Secure-3PSIDCC": "value"
  }
}
```

### 视频推送接口

```http
POST /tasks/add_via_extension
Content-Type: application/json

{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "title": "视频标题",
  "channel": "频道名称",
  "duration": "视频时长",
  "source": "extension"
}
```

## 故障排除

### 常见问题

#### Q: 扩展图标不显示

**解决方案**：
1. 检查扩展是否正确加载
2. 确认扩展已启用
3. 尝试重新加载扩展

#### Q: Cookie同步失败

**排查步骤**：
1. 确认Y2A-Auto服务器运行正常
2. 检查 `background.js` 中的服务器地址
3. 查看扩展控制台错误信息
4. 确认网络连接正常

#### Q: 推送按钮不显示

**解决方案**：
1. 刷新YouTube页面
2. 检查YouTube页面是否为视频页
3. 查看浏览器控制台错误信息

#### Q: 服务器连接超时

**检查清单**：
1. 服务器地址配置是否正确
2. 服务器是否正常运行
3. 防火墙是否阻止连接
4. 网络代理设置是否影响

### 调试方法

#### 查看扩展日志

1. 打开 `chrome://extensions/` 或相应浏览器的扩展页面
2. 找到Y2A-Auto扩展，点击"详细信息"
3. 点击"检查视图"中的"Service Worker"或"背景页"
4. 在控制台查看日志信息

#### 查看内容脚本日志

1. 在YouTube页面按F12打开开发者工具
2. 切换到Console标签
3. 查找 `[Y2A-Extension]` 开头的日志

#### 网络请求调试

1. 在开发者工具中切换到Network标签
2. 执行同步或推送操作
3. 查看相关API请求的状态和响应

### 配置调试

#### 启用调试模式

在 `background.js` 中启用详细日志：

```javascript
const DEBUG_MODE = true; // 启用调试模式

function debugLog(message, data = null) {
  if (DEBUG_MODE) {
    console.log(`[Y2A-Extension] ${message}`, data);
  }
}
```

#### 测试连接

可以使用以下代码测试服务器连接：

```javascript
// 在扩展控制台中执行
fetch('http://localhost:5000/api/health')
  .then(response => response.json())
  .then(data => console.log('服务器响应:', data))
  .catch(error => console.error('连接失败:', error));
```

## 安全说明

### 数据安全

- **Cookie加密** - 传输过程中Cookie数据使用HTTPS加密
- **本地存储** - 最小化本地数据存储，定期清理
- **权限控制** - 仅请求必要的浏览器权限

### 隐私保护

- **数据范围** - 仅处理YouTube相关Cookie和视频URL
- **传输限制** - 数据仅发送到配置的Y2A-Auto服务器
- **无第三方** - 不向任何第三方服务发送数据

## 更新维护

### 扩展更新

1. 获取最新的扩展文件
2. 替换原有的 `browser-extension/` 目录
3. 在扩展管理页面重新加载扩展

### 配置迁移

更新时注意保留你的自定义配置：

```javascript
// 备份你的配置
const Y2A_AUTO_SERVER = 'http://your-server:5000';
const SYNC_INTERVAL_MINUTES = 10;
```

### 版本兼容性

- **向前兼容** - 新版本兼容旧版本的API
- **配置检查** - 自动检查配置有效性
- **错误恢复** - 自动处理配置错误

## 技术支持

### 问题报告

遇到问题时，请提供以下信息：

1. **扩展版本** - 从manifest.json中获取
2. **浏览器信息** - 浏览器类型和版本
3. **错误日志** - 扩展控制台的错误信息
4. **网络环境** - 服务器地址和网络配置
5. **重现步骤** - 问题的具体重现方法

### 功能建议

欢迎提出功能改进建议：

- 新的Cookie同步策略
- 界面优化建议
- 性能改进想法
- 兼容性增强需求

## 许可证

本扩展遵循Y2A-Auto项目的开源许可证。 