// ==UserScript==
// @name         推送到Y2A-Auto
// @namespace    http://tampermonkey.net/
// @version      0.1
// @description  将YouTube视频发送到Y2A-Auto进行处理
// @author       Y2A-Auto用户
// @match        *://www.youtube.com/watch?v=*
// @match        *://youtube.com/watch?v=*
// @grant        GM_xmlhttpRequest
// @grant        GM_notification
// @connect      localhost
// @connect      your-y2a-auto-server.com
// ==/UserScript==

(function() {
    'use strict';

    // Y2A-Auto服务器地址，可根据实际部署情况修改
    const Y2A_AUTO_SERVER = 'http://localhost:5000';
    const API_ENDPOINT = `${Y2A_AUTO_SERVER}/tasks/add_via_extension`;
    
    // 样式定义
    const BUTTON_STYLE = `
        background-color: #007bff;
        color: white;
        border: none;
        border-radius: 4px;
        padding: 8px 12px;
        font-size: 14px;
        font-weight: bold;
        cursor: pointer;
        margin-left: 10px;
        transition: background-color 0.3s;
    `;

    // 创建按钮
    function createButton() {
        const button = document.createElement('button');
        button.textContent = '推送到Y2A-Auto';
        button.id = 'push-to-y2a-button';
        button.setAttribute('style', BUTTON_STYLE);
        
        // 鼠标悬停效果
        button.addEventListener('mouseover', function() {
            this.style.backgroundColor = '#0069d9';
        });
        
        button.addEventListener('mouseout', function() {
            this.style.backgroundColor = '#007bff';
        });
        
        // 点击事件
        button.addEventListener('click', function() {
            // alert('[Y2A-Auto Script] Button click event FIRED!'); // 调试用，已确认，移除此行
            console.log('%c[Y2A-Auto Script] PUSH BUTTON CLICKED! Event listener IS FIRING.', 'color: green; font-weight: bold;');
            console.log('[Y2A-Auto Script] Push button clicked. Calling sendToY2AAuto with:', this);
            sendToY2AAuto(this);
        });
        
        return button;
    }

    // 将按钮添加到YouTube界面
    function addButtonToPage() {
        // 尝试获取视频标题下方的操作栏
        const actionBar = document.querySelector('#top-level-buttons-computed');
        
        if (actionBar) {
            // 在分享等按钮所在的位置添加我们的按钮
            const button = createButton();
            actionBar.appendChild(button);
            return true;
        }
        
        // 如果找不到标准位置，尝试插入到备选位置
        const alternativeLocation = document.querySelector('#above-the-fold');
        if (alternativeLocation) {
            const button = createButton();
            alternativeLocation.appendChild(button);
            return true;
        }
        
        return false;
    }

    // 发送视频数据到Y2A-Auto服务器
    function sendToY2AAuto(clickedButton) {
        console.log('[Y2A-Auto Script] sendToY2AAuto entered. clickedButton:', clickedButton);

        if (!clickedButton || typeof clickedButton.textContent === 'undefined') {
            console.error('[Y2A-Auto Script] Error: clickedButton is not a valid element in sendToY2AAuto.', clickedButton);
            alert('[Y2A-Auto Script] 错误：按钮元素无效，无法继续。'); // Fallback alert for critical error
            return;
        }

        const videoUrl = window.location.href;
        console.log('[Y2A-Auto Script] videoUrl:', videoUrl);
        
        // 显示加载状态
        const button = clickedButton; // 使用传入的按钮元素
        const originalText = button.textContent;
        button.textContent = '发送中...';
        button.disabled = true;
        console.log('[Y2A-Auto Script] Button text changed to "发送中..." and disabled.');
        
        // 立即显示任务已发送的通知
        console.log('[Y2A-Auto Script] About to call showNotification for "已发送请求".');
        showNotification('推送状态', '已发送请求至Y2A-Auto，请等待服务器响应...', 'info');
        
        console.log('[Y2A-Auto Script] About to make GM_xmlhttpRequest to:', API_ENDPOINT);
        GM_xmlhttpRequest({
            method: 'POST',
            url: API_ENDPOINT,
            headers: {
                'Content-Type': 'application/json'
            },
            data: JSON.stringify({
                youtube_url: videoUrl
            }),
            onload: function(response) {
                console.log('[Y2A-Auto Script] GM_xmlhttpRequest onload triggered. Response status:', response.status);
                button.textContent = originalText;
                button.disabled = false;
                
                try {
                    const result = JSON.parse(response.responseText);
                    console.log('[Y2A-Auto Script] Parsed server response:', result);
                    
                    if (result.success) {
                        // 成功处理
                        showNotification('成功', `${result.message} (任务ID: ${result.task_id})`, 'success');
                    } else {
                        // 处理失败
                        showNotification('失败', result.message || '未知错误', 'error');
                    }
                } catch (e) {
                    console.error('[Y2A-Auto Script] Error parsing server response:', e, 'Response text:', response.responseText);
                    // JSON解析错误
                    showNotification('错误', '无法解析服务器响应', 'error');
                }
            },
            onerror: function(error) {
                console.error('[Y2A-Auto Script] GM_xmlhttpRequest onerror triggered.', error);
                button.textContent = originalText;
                button.disabled = false;
                showNotification('连接错误', '无法连接到Y2A-Auto服务器，请确认服务器是否运行', 'error');
            },
            ontimeout: function() {
                console.error('[Y2A-Auto Script] GM_xmlhttpRequest ontimeout triggered.');
                button.textContent = originalText;
                button.disabled = false;
                showNotification('连接超时', '连接Y2A-Auto服务器超时', 'error');
            }
        });
    }

    // 显示通知
    function showNotification(title, message, type) {
        console.log(`[Y2A-Auto Script] In-page showNotification: title="${title}", message="${message}", type="${type}"`);

        // 移除任何现有的横幅
        const existingBanner = document.getElementById('y2a-auto-inpage-notification');
        if (existingBanner) {
            existingBanner.parentNode.removeChild(existingBanner);
        }

        const banner = document.createElement('div');
        banner.id = 'y2a-auto-inpage-notification';

        let backgroundColor;
        switch (type) {
            case 'success':
                backgroundColor = '#28a745'; // 绿色
                break;
            case 'error':
                backgroundColor = '#dc3545'; // 红色
                break;
            case 'info':
            default:
                backgroundColor = '#007bff'; // 蓝色
                break;
        }

        // 创建 strong 元素用于标题
        const titleStrong = document.createElement('strong');
        titleStrong.textContent = title + ': '; // 加个冒号和空格

        // 创建文本节点用于消息
        const messageText = document.createTextNode(message);

        // 清空 banner 并添加新的子元素
        while (banner.firstChild) {
            banner.removeChild(banner.firstChild);
        }
        banner.appendChild(titleStrong);
        banner.appendChild(messageText);

        banner.setAttribute('style', `
            position: fixed;
            top: 20px; /* 初始位置，用于滑入效果 */
            left: 50%;
            transform: translateX(-50%);
            padding: 12px 20px;
            border-radius: 8px;
            color: white;
            background-color: ${backgroundColor};
            z-index: 2147483647; /* 确保在最顶层 */
            opacity: 0;
            transition: opacity 0.5s ease-in-out, top 0.5s ease-in-out;
            font-family: Arial, sans-serif;
            font-size: 14px;
            box-shadow: 0 4px 8px rgba(0,0,0,0.2);
            text-align: center;
        `);

        document.body.appendChild(banner);

        // 动画进入 (滑下并淡入)
        setTimeout(() => {
            banner.style.opacity = '1';
            banner.style.top = '30px'; // 滑入后的最终位置
        }, 100); // 短暂延迟以确保过渡效果生效

        const displayDuration = (type === 'error' ? 7000 : 5000); // 错误信息显示时间更长

        // 淡出并移除
        setTimeout(() => {
            banner.style.opacity = '0';
            banner.style.top = '20px'; // 淡出时滑回初始位置
        }, displayDuration);

        // 动画完成后从DOM中移除
        setTimeout(() => {
            if (banner.parentNode) {
                banner.parentNode.removeChild(banner);
            }
        }, displayDuration + 500); // 500ms 用于淡出过渡
    }

    // 监听页面变化，在YouTube的SPA导航中保持按钮存在
    function setupObserver() {
        // YouTube使用动态加载内容，需要监听DOM变化
        const observer = new MutationObserver(function(mutations) {
            // 检查我们的按钮是否已存在
            if (!document.getElementById('push-to-y2a-button')) {
                // 尝试添加按钮
                if (addButtonToPage()) {
                    console.log('[Y2A-Auto Script] Y2A-Auto推送按钮已通过Observer添加到页面');
                }
            }
        });
        
        // 监视整个body元素的变化
        observer.observe(document.body, { childList: true, subtree: true });
    }

    // 初始化
    function init() {
        // 等待页面加载完成
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', function() {
                if (addButtonToPage()) {
                    console.log('[Y2A-Auto Script] Y2A-Auto推送按钮已添加到页面 (DOMContentLoaded)');
                } else {
                    console.warn('[Y2A-Auto Script] 无法找到适合的位置添加Y2A-Auto推送按钮 (DOMContentLoaded), 设置观察器');
                    setupObserver();
                }
            });
        } else {
            // 页面已加载，直接添加按钮
            if (addButtonToPage()) {
                console.log('[Y2A-Auto Script] Y2A-Auto推送按钮已添加到页面 (direct)');
            } else {
                console.warn('[Y2A-Auto Script] 无法找到适合的位置添加Y2A-Auto推送按钮 (direct), 设置观察器');
                setupObserver();
            }
        }
    }

    // 运行初始化
    init();
})(); 