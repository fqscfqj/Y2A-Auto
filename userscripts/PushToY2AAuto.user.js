// ==UserScript==
// @name         推送到Y2A-Auto
// @namespace    http://tampermonkey.net/
// @version      1.0
// @description  将YouTube视频发送到Y2A-Auto进行处理
// @author       Y2A-Auto用户
// @match        *://www.youtube.com/watch?v=*
// @match        *://youtube.com/watch?v=*
// @grant        GM_xmlhttpRequest
// @grant        GM_notification
// @connect      localhost
// @connect      127.0.0.1
// @connect      your-y2a-auto-server.com
// ==/UserScript==

(function() {
    'use strict';

    // Y2A-Auto服务器地址配置
    // 请根据您的实际部署情况修改以下地址
    const Y2A_AUTO_SERVER = 'http://localhost:5000';
    const API_ENDPOINT = `${Y2A_AUTO_SERVER}/tasks/add_via_extension`;
    
    // 调试模式开关（生产环境请设置为false）
    const DEBUG_MODE = false;
    
    // 调试日志函数
    function debugLog(message, ...args) {
        if (DEBUG_MODE) {
            console.log(`[Y2A-Auto Script] ${message}`, ...args);
        }
    }
    
    // 样式定义
    const BUTTON_STYLE = `
        background-color: #ff4757;
        color: white;
        border: none;
        border-radius: 6px;
        padding: 8px 16px;
        font-size: 14px;
        font-weight: 600;
        cursor: pointer;
        margin-left: 10px;
        transition: all 0.3s ease;
        display: inline-flex;
        align-items: center;
        gap: 6px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    `;

    // 创建按钮
    function createButton() {
        const button = document.createElement('button');
        button.innerHTML = '📤 推送到Y2A-Auto';
        button.id = 'push-to-y2a-button';
        button.setAttribute('style', BUTTON_STYLE);
        button.title = '将当前视频推送到Y2A-Auto进行自动处理';
        
        // 鼠标悬停效果
        button.addEventListener('mouseover', function() {
            this.style.backgroundColor = '#ff3742';
            this.style.transform = 'translateY(-1px)';
            this.style.boxShadow = '0 4px 8px rgba(0,0,0,0.15)';
        });
        
        button.addEventListener('mouseout', function() {
            this.style.backgroundColor = '#ff4757';
            this.style.transform = 'translateY(0)';
            this.style.boxShadow = '0 2px 4px rgba(0,0,0,0.1)';
        });
        
        // 点击事件
        button.addEventListener('click', function(event) {
            event.preventDefault();
            event.stopPropagation();
            debugLog('Push button clicked');
            sendToY2AAuto(this);
        });
        
        return button;
    }

    // 将按钮添加到YouTube界面
    function addButtonToPage() {
        // 检查是否已存在按钮
        if (document.getElementById('push-to-y2a-button')) {
            return true;
        }
        
        // 优先尝试新版YouTube布局
        const actionBar = document.querySelector('#top-level-buttons-computed');
        if (actionBar) {
            const button = createButton();
            actionBar.appendChild(button);
            debugLog('Button added to top-level-buttons-computed');
            return true;
        }
        
        // 尝试其他可能的位置
        const alternatives = [
            '#above-the-fold',
            '.ytd-video-primary-info-renderer',
            '#info-contents'
        ];
        
        for (const selector of alternatives) {
            const container = document.querySelector(selector);
            if (container) {
                const button = createButton();
                container.appendChild(button);
                debugLog(`Button added to ${selector}`);
                return true;
            }
        }
        
        return false;
    }

    // 发送视频数据到Y2A-Auto服务器
    function sendToY2AAuto(clickedButton) {
        if (!clickedButton) {
            showNotification('错误', '按钮元素无效', 'error');
            return;
        }

        const videoUrl = window.location.href;
        debugLog('Sending video URL:', videoUrl);
        
        // 验证URL格式
        if (!videoUrl.includes('youtube.com/watch?v=')) {
            showNotification('错误', '当前页面不是有效的YouTube视频页面', 'error');
            return;
        }
        
        // 显示加载状态
        const originalHTML = clickedButton.innerHTML;
        clickedButton.innerHTML = '⏳ 发送中...';
        clickedButton.disabled = true;
        clickedButton.style.opacity = '0.7';
        
        // 显示发送通知
        showNotification('推送状态', '正在发送请求到Y2A-Auto服务器...', 'info');
        
        // 发送请求
        GM_xmlhttpRequest({
            method: 'POST',
            url: API_ENDPOINT,
            headers: {
                'Content-Type': 'application/json'
            },
            data: JSON.stringify({
                youtube_url: videoUrl
            }),
            timeout: 10000, // 10秒超时
            onload: function(response) {
                resetButton(clickedButton, originalHTML);
                
                try {
                    const result = JSON.parse(response.responseText);
                    debugLog('Server response:', result);
                    
                    if (response.status === 200 && result.success) {
                        const taskId = result.task_id ? ` (任务ID: ${result.task_id.substring(0, 8)}...)` : '';
                        showNotification('✅ 推送成功', `${result.message}${taskId}`, 'success');
                        
                        // 可选：在按钮上显示成功状态
                        clickedButton.innerHTML = '✅ 已推送';
                        setTimeout(() => {
                            clickedButton.innerHTML = originalHTML;
                        }, 3000);
                    } else {
                        showNotification('❌ 推送失败', result.message || '服务器返回错误', 'error');
                    }
                } catch (e) {
                    debugLog('JSON parse error:', e);
                    showNotification('❌ 解析错误', '无法解析服务器响应', 'error');
                }
            },
            onerror: function(error) {
                debugLog('Request error:', error);
                resetButton(clickedButton, originalHTML);
                showNotification('❌ 连接失败', `无法连接到Y2A-Auto服务器 (${Y2A_AUTO_SERVER})。请确认：\n1. 服务器是否运行\n2. 服务器地址是否正确\n3. 防火墙/网络设置`, 'error');
            },
            ontimeout: function() {
                debugLog('Request timeout');
                resetButton(clickedButton, originalHTML);
                showNotification('⏰ 连接超时', '连接Y2A-Auto服务器超时，请稍后重试', 'error');
            }
        });
    }
    
    // 重置按钮状态
    function resetButton(button, originalHTML) {
        button.innerHTML = originalHTML;
        button.disabled = false;
        button.style.opacity = '1';
    }

    // 显示页面内通知
    function showNotification(title, message, type) {
        debugLog(`Showing notification: ${title} - ${message} (${type})`);

        // 移除现有通知
        const existingBanner = document.getElementById('y2a-auto-notification');
        if (existingBanner) {
            existingBanner.remove();
        }

        const banner = document.createElement('div');
        banner.id = 'y2a-auto-notification';

        // 根据类型设置颜色和图标
        let backgroundColor, icon;
        switch (type) {
            case 'success':
                backgroundColor = '#2ecc71';
                icon = '✅';
                break;
            case 'error':
                backgroundColor = '#e74c3c';
                icon = '❌';
                break;
            case 'info':
            default:
                backgroundColor = '#3498db';
                icon = 'ℹ️';
                break;
        }

        // 设置通知内容
        banner.innerHTML = `
            <div style="display: flex; align-items: center; gap: 8px;">
                <span style="font-size: 16px;">${icon}</span>
                <div>
                    <strong>${title}</strong>
                    <div style="font-size: 13px; margin-top: 2px; white-space: pre-line;">${message}</div>
                </div>
            </div>
        `;

        // 设置样式
        banner.setAttribute('style', `
            position: fixed;
            top: -100px;
            left: 50%;
            transform: translateX(-50%);
            padding: 16px 24px;
            border-radius: 8px;
            color: white;
            background-color: ${backgroundColor};
            z-index: 2147483647;
            opacity: 0;
            transition: all 0.4s cubic-bezier(0.25, 0.8, 0.25, 1);
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            font-size: 14px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.3);
            backdrop-filter: blur(10px);
            max-width: 400px;
            text-align: left;
            cursor: pointer;
        `);

        document.body.appendChild(banner);

        // 动画显示
        requestAnimationFrame(() => {
            banner.style.opacity = '1';
            banner.style.top = '20px';
        });

        // 点击关闭
        banner.addEventListener('click', () => {
            hideNotification(banner);
        });

        // 自动隐藏
        const displayDuration = type === 'error' ? 8000 : 5000;
        setTimeout(() => {
            hideNotification(banner);
        }, displayDuration);
    }
    
    // 隐藏通知
    function hideNotification(banner) {
        if (banner && banner.parentNode) {
            banner.style.opacity = '0';
            banner.style.top = '-100px';
            setTimeout(() => {
                if (banner.parentNode) {
                    banner.remove();
                }
            }, 400);
        }
    }

    // 监听页面变化，适应YouTube的SPA导航
    function setupObserver() {
        const observer = new MutationObserver(function(mutations) {
            // 检查URL是否变化（YouTube SPA导航）
            if (window.location.href.includes('/watch?v=')) {
                setTimeout(() => {
                    if (!document.getElementById('push-to-y2a-button')) {
                        if (addButtonToPage()) {
                            debugLog('Button added via observer');
                        }
                    }
                }, 1000); // 延迟以确保页面元素加载完成
            }
        });
        
        observer.observe(document.body, { 
            childList: true, 
            subtree: true 
        });
        
        // 监听URL变化
        let lastUrl = location.href;
        new MutationObserver(() => {
            const url = location.href;
            if (url !== lastUrl) {
                lastUrl = url;
                if (url.includes('/watch?v=')) {
                    setTimeout(() => {
                        if (!document.getElementById('push-to-y2a-button')) {
                            addButtonToPage();
                        }
                    }, 1500);
                }
            }
        }).observe(document, { subtree: true, childList: true });
    }

    // 初始化
    function init() {
        debugLog('Y2A-Auto script initializing...');
        
        // 确保在YouTube视频页面
        if (!window.location.href.includes('/watch?v=')) {
            debugLog('Not on a YouTube video page, skipping initialization');
            setupObserver(); // 仍然设置观察器，以便在导航到视频页面时添加按钮
            return;
        }
        
        // 等待页面加载完成
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', function() {
                setTimeout(() => {
                    if (addButtonToPage()) {
                        debugLog('Button added on DOMContentLoaded');
                    } else {
                        debugLog('Failed to add button on DOMContentLoaded, setting up observer');
                        setupObserver();
                    }
                }, 1000);
            });
        } else {
            // 页面已加载
            setTimeout(() => {
                if (addButtonToPage()) {
                    debugLog('Button added immediately');
                } else {
                    debugLog('Failed to add button immediately, setting up observer');
                    setupObserver();
                }
            }, 1000);
        }
        
        // 无论如何都设置观察器以处理SPA导航
        setupObserver();
    }

    // 运行初始化
    init();
})(); 