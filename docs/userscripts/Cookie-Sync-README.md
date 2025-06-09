# Cookie同步脚本使用指南

自动从YouTube获取并同步cookies到Y2A-Auto服务器的浏览器扩展脚本。

## 概述

该脚本自动检测YouTube登录状态变化，将有效的cookies同步到Y2A-Auto服务器，确保下载功能正常运行。

## 主要功能

- 自动检测cookie变化
- 定时同步到服务器
- 可视化状态显示
- 自动重试机制
- 数据备份保护

## 安装配置

### 前置要求

- Tampermonkey浏览器扩展
- Y2A-Auto服务器运行中

### 安装步骤

1. 安装Tampermonkey
   - [Chrome扩展商店](https://chrome.google.com/webstore/detail/tampermonkey/dhdgffkkebhmkfjojejmpbldmpobfkfo)
   - [Firefox附加组件](https://addons.mozilla.org/firefox/addon/tampermonkey/)

2. 添加脚本

   ```text
   复制 userscripts/Y2A-Auto-Cookie-Sync.user.js 内容
   → Tampermonkey管理面板
   → 添加新脚本
   → 粘贴并保存
   ```

3. 配置服务器地址

   ```javascript
   const Y2A_AUTO_SERVER = 'http://localhost:5000';  // 修改为实际地址
   ```

4. 配置连接权限

   确保脚本头部包含正确的连接权限：

   ```javascript
   // @connect      localhost
   // @connect      127.0.0.1
   // @connect      your-server.com  // 添加你的服务器域名
   ```

## 使用方法

### 基本操作

1. 登录YouTube

   使用有效的YouTube账户登录

2. 启用脚本

   确保Tampermonkey图标为绿色状态

3. 查看状态

   页面右上角显示同步状态指示器

### 状态指示器

| 状态 | 含义 | 颜色 |
|------|------|------|
| ✅ 已同步 | cookies已成功同步 | 绿色 |
| 🔄 同步中 | 正在同步到服务器 | 蓝色 |
| ⚠️ 待同步 | 检测到变化，等待同步 | 橙色 |
| ❌ 同步失败 | 连接服务器失败 | 红色 |

### 手动控制

通过Tampermonkey菜单可进行手动操作：

- 立即同步：强制执行一次同步
- 清除本地：清除本地存储的cookie数据
- 查看状态：显示详细的同步信息

## 工作原理

### 同步机制

1. 变化检测：监控指定cookie字段的变化
2. 数据提取：提取必要的认证cookie
3. 服务器通信：通过API发送到Y2A-Auto
4. 状态更新：更新本地存储和显示状态

### 监控字段

脚本监控以下关键cookie：

- `__Secure-1PSID`
- `__Secure-3PSID`
- `HSID`
- `SSID`
- `SIDCC`

## API接口

### 同步端点

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
    "SIDCC": "value"
  }
}
```

### 响应格式

```json
{
  "success": true,
  "message": "Cookies同步成功",
  "timestamp": "2024-12-09T12:00:00Z"
}
```

## 故障排除

### 常见问题

#### 状态指示器不显示

检查脚本是否启用，刷新页面重试

#### 同步失败（红色状态）

1. 确认Y2A-Auto服务运行状态
2. 检查服务器地址配置
3. 查看浏览器控制台错误信息

#### Cookie未生效

1. 确认YouTube登录状态
2. 检查cookie内容是否完整
3. 尝试重新登录YouTube

### 调试模式

启用调试输出：

```javascript
const DEBUG_MODE = true;  // 在脚本中修改此值
```

### 日志查看

1. 打开开发者工具（F12）
2. 切换到Console标签
3. 查看`[Y2A-Cookie-Sync]`开头的日志

### 网络问题

检查网络连接：

```javascript
// 测试连接
fetch('http://your-server:5000/api/health')
  .then(r => console.log('服务器可达'))
  .catch(e => console.log('连接失败:', e));
```

## 配置选项

### 基本配置

```javascript
// 服务器配置
const Y2A_AUTO_SERVER = 'http://localhost:5000';

// 同步间隔（毫秒）
const SYNC_INTERVAL = 60000;  // 1分钟

// 重试配置
const MAX_RETRIES = 3;
const RETRY_DELAY = 5000;  // 5秒
```

### 高级配置

```javascript
// 监控字段（可自定义）
const MONITORED_COOKIES = [
  '__Secure-1PSID',
  '__Secure-3PSID',
  'HSID',
  'SSID',
  'SIDCC'
];

// 状态显示位置
const INDICATOR_POSITION = 'top-right';
```

## 安全说明

- 脚本仅在YouTube域名运行
- 只提取必要的认证cookie
- 数据仅发送到配置的Y2A-Auto服务器
- 本地数据加密存储

## 兼容性

### 浏览器支持

- Chrome 88+
- Firefox 86+
- Edge 88+

### YouTube版本

- 支持当前版本YouTube
- 自动适配界面变化

## 技术支持

遇到问题时，请提供：

1. 浏览器版本信息
2. 脚本版本号
3. 控制台错误日志
4. Y2A-Auto服务器日志

## 更新日志

### v1.0

- 初始版本发布
- 基础cookie同步功能
- 状态指示器
- 自动重试机制
