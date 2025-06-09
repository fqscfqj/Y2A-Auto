# YouTube视频推送脚本使用指南

在YouTube视频页面添加推送按钮，一键将视频发送到Y2A-Auto处理队列。

## 概述

该脚本在YouTube视频页面添加推送按钮，让用户可以一键将当前观看的视频发送到Y2A-Auto程序进行自动处理。

## 功能特性

- 一键推送：在YouTube视频页面添加醒目的推送按钮
- 智能集成：自动适配YouTube页面布局变化
- 实时反馈：显示推送状态和结果通知
- 错误处理：完善的网络错误处理和超时机制
- SPA支持：支持YouTube的单页应用导航
- 美观界面：现代化的按钮设计和交互效果

## 安装步骤

### 安装Tampermonkey

- **Chrome**: 安装 [Tampermonkey](https://chrome.google.com/webstore/detail/tampermonkey/dhdgffkkebhmkfjojejmpbldmpobfkfo)
- **Firefox**: 安装 [Tampermonkey](https://addons.mozilla.org/firefox/addon/tampermonkey/)
- **Edge**: 安装 [Tampermonkey](https://microsoftedge.microsoft.com/addons/detail/tampermonkey/iikmkjmpaadaobahmlepeloendndfphd)

### 安装脚本

1. 点击 Tampermonkey 图标
2. 选择 "添加新脚本"
3. 复制 `PushToY2AAuto.user.js` 文件内容并粘贴
4. 保存脚本（Ctrl+S）

### 配置服务器地址

在脚本开头找到配置部分：

```javascript
// Y2A-Auto服务器地址配置
// 请根据您的实际部署情况修改以下地址
const Y2A_AUTO_SERVER = 'http://localhost:5000';
```

根据你的Y2A-Auto部署情况修改 `Y2A_AUTO_SERVER`：

- **本地部署**: `http://localhost:5000`
- **Docker部署**: `http://localhost:5000` 或实际端口
- **远程服务器**: `http://你的服务器IP:端口`

### 配置连接权限

确保Tampermonkey允许连接到你的服务器：

```javascript
// @connect      localhost
// @connect      127.0.0.1
// @connect      your-y2a-auto-server.com
```

如果使用远程服务器，需要添加对应的域名到 `@connect` 列表。

## 使用方法

### 基本操作

1. 在YouTube上打开任何视频页面
2. 页面加载完成后，会在视频控制区域出现 "📤 推送到Y2A-Auto" 按钮
3. 点击按钮即可将当前视频发送到Y2A-Auto处理队列
4. 系统会显示推送状态通知

### 按钮位置

脚本会尝试将按钮添加到以下位置（按优先级）：

1. `#top-level-buttons-computed` - 新版YouTube的操作按钮区域
2. `#above-the-fold` - 视频信息区域上方
3. `.ytd-video-primary-info-renderer` - 视频主要信息区域
4. `#info-contents` - 信息内容区域

### 状态反馈

- **⏳ 发送中...**: 正在向服务器发送请求
- **✅ 已推送**: 推送成功，显示任务ID
- **❌ 推送失败**: 推送失败，显示错误信息

## 通知系统

### 通知类型

- **📤 推送状态**: 显示推送进度
- **✅ 推送成功**: 显示成功信息和任务ID
- **❌ 推送失败**: 显示具体错误原因
- **⏰ 连接超时**: 网络连接超时提示

### 通知交互

- 通知会自动显示在页面顶部
- 点击通知可以立即关闭
- 成功通知5秒后自动消失
- 错误通知8秒后自动消失

## 工作原理

1. **页面检测**: 脚本检测当前是否为YouTube视频页面
2. **按钮注入**: 在合适位置添加推送按钮
3. **URL提取**: 获取当前视频的完整URL
4. **数据发送**: 通过POST请求发送到Y2A-Auto API
5. **结果处理**: 解析服务器响应并显示结果

## API接口

脚本调用Y2A-Auto的以下API端点：

```http
POST /tasks/add_via_extension
Content-Type: application/json

{
    "youtube_url": "https://www.youtube.com/watch?v=VIDEO_ID"
}
```

响应格式：

```json
{
    "success": true,
    "message": "任务已添加到队列",
    "task_id": "task_uuid"
}
```

## 故障排除

### 常见问题

#### 按钮没有出现怎么办

1. 确保脚本已启用（Tampermonkey图标显示绿色）
2. 刷新YouTube页面
3. 检查浏览器控制台是否有错误信息
4. 确认当前页面是视频页面（包含 `/watch?v=`）

#### 点击按钮没有反应

1. 检查浏览器是否阻止了弹出窗口或通知
2. 查看浏览器控制台的错误信息
3. 确认Y2A-Auto服务器地址配置正确
4. 测试Y2A-Auto服务是否正常运行

#### 推送失败怎么办

1. 连接失败: 检查网络连接和服务器状态
2. 权限错误: 确认Tampermonkey的 `@connect` 配置
3. 服务器错误: 查看Y2A-Auto日志中的错误信息
4. URL格式错误: 确认当前页面是有效的YouTube视频

#### 通知显示异常

1. 检查浏览器是否允许通知权限
2. 确认页面没有被其他扩展干扰
3. 尝试禁用广告拦截器
4. 清除浏览器缓存和Cookie

### 调试模式

启用详细日志输出：

1. 打开脚本编辑器
2. 找到 `DEBUG_MODE` 设置
3. 将其改为 `true`：

```javascript
const DEBUG_MODE = true;
```

### 日志查看

1. 打开浏览器开发者工具（F12）
2. 切换到Console标签
3. 查看以 `[Y2A-Auto Script]` 开头的日志信息

## 自定义配置

### 按钮样式

可以修改按钮的外观：

```javascript
const BUTTON_STYLE = `
    background-color: #ff4757;  /* 背景色 */
    color: white;               /* 文字色 */
    border-radius: 6px;         /* 圆角 */
    padding: 8px 16px;          /* 内边距 */
    font-size: 14px;            /* 字体大小 */
    /* 其他样式... */
`;
```

### 超时设置

修改网络请求超时时间：

```javascript
timeout: 10000, // 10秒超时，可根据需要调整
```

### 按钮文本

自定义按钮显示文本：

```javascript
button.innerHTML = '📤 推送到Y2A-Auto'; // 可修改为其他文本
```

## 兼容性说明

### 浏览器支持

- Chrome 88+
- Firefox 86+
- Edge 88+
- Safari 14+

### YouTube版本

- 支持当前版本的YouTube桌面网站
- 自动适配YouTube界面更新
- 支持新旧版本的页面布局

### Y2A-Auto版本

- 需要Y2A-Auto v1.0+
- 兼容所有包含 `/tasks/add_via_extension` API的版本

## 安全说明

- 脚本只在YouTube域名下运行
- 只发送视频URL到指定的Y2A-Auto服务器
- 不收集或传输用户个人信息
- 不会访问YouTube账户数据
- 遵循同源策略和CORS规范

## 更新日志

### v1.0

- 初始版本发布
- 支持一键推送YouTube视频
- 添加实时状态通知
- 实现自适应页面布局
- 完善错误处理机制

## 技术支持

如遇到问题，请：

1. 查看浏览器控制台日志
2. 检查Y2A-Auto服务器日志
3. 确认网络连接和防火墙设置
4. 提供详细的错误信息和操作步骤

## 许可证

本脚本遵循与Y2A-Auto项目相同的许可证。
