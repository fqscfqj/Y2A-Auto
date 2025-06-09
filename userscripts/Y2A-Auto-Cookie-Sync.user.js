// ==UserScript==
// @name         Y2A-Auto Cookie 自动同步
// @namespace    https://github.com/Y2A-Auto
// @version      1.0.0
// @description  自动同步YouTube cookies到Y2A-Auto程序，确保cookie及时性
// @author       Y2A-Auto
// @match        https://www.youtube.com/*
// @match        https://youtube.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_notification
// @grant        GM_registerMenuCommand
// @run-at       document-start
// ==/UserScript==

(function() {
    'use strict';

    // 配置项
    const CONFIG = {
        // Y2A-Auto服务器地址（请根据实际情况修改）
        serverUrl: 'http://localhost:5000',
        
        // 同步间隔（毫秒）- 默认30分钟
        syncInterval: 30 * 60 * 1000,
        
        // 是否启用自动同步
        autoSyncEnabled: true,
        
        // 是否显示通知
        showNotifications: true,
        
        // cookie过期检查间隔（毫秒）- 默认5分钟
        checkInterval: 5 * 60 * 1000
    };

    // 状态管理
    let syncTimer = null;
    let checkTimer = null;
    let lastSyncTime = GM_getValue('lastSyncTime', 0);
    let lastCookieHash = GM_getValue('lastCookieHash', '');

    // 日志函数
    function log(message, type = 'info') {
        const timestamp = new Date().toLocaleString();
        console.log(`[Y2A-Cookie-Sync ${timestamp}] ${type.toUpperCase()}: ${message}`);
    }

    // 显示通知
    function showNotification(title, text, type = 'info') {
        if (!CONFIG.showNotifications) return;
        
        const icons = {
            'success': '✅',
            'error': '❌',
            'warning': '⚠️',
            'info': 'ℹ️'
        };
        
        GM_notification({
            title: `${icons[type]} ${title}`,
            text: text,
            timeout: 5000,
            onclick: () => window.focus()
        });
    }

    // 获取YouTube cookies
    function getYouTubeCookies() {
        const cookies = document.cookie.split(';');
        const youtubeCookies = [];
        
        // YouTube重要的cookie名称
        const importantCookies = [
            'VISITOR_INFO1_LIVE',
            'YSC',
            'PREF',
            'CONSENT',
            'SOCS',
            '__Secure-YEC',
            'GPS',
            'VISITOR_PRIVACY_METADATA'
        ];
        
        // 获取当前域名和子域名的所有cookies
        cookies.forEach(cookie => {
            const [name, value] = cookie.trim().split('=');
            if (name && value) {
                // 添加重要的cookies或所有youtube相关的cookies
                if (importantCookies.includes(name) || 
                    name.startsWith('__Secure-') || 
                    name.includes('youtube') || 
                    name.includes('YT')) {
                    
                    youtubeCookies.push({
                        name: name.trim(),
                        value: value.trim(),
                        domain: '.youtube.com',
                        path: '/',
                        secure: true,
                        httpOnly: false,
                        sameSite: 'None'
                    });
                }
            }
        });

        // 生成Netscape格式的cookies
        let netscapeCookies = '# Netscape HTTP Cookie File\n';
        netscapeCookies += '# This is a generated file! Do not edit.\n\n';
        
        youtubeCookies.forEach(cookie => {
            // Netscape格式：domain flag path secure expiration name value
            const expiration = Math.floor(Date.now() / 1000) + (365 * 24 * 60 * 60); // 1年后过期
            netscapeCookies += `${cookie.domain}\tTRUE\t${cookie.path}\t${cookie.secure ? 'TRUE' : 'FALSE'}\t${expiration}\t${cookie.name}\t${cookie.value}\n`;
        });

        return {
            cookies: youtubeCookies,
            netscapeFormat: netscapeCookies,
            count: youtubeCookies.length
        };
    }

    // 计算cookie hash用于检测变化
    function calculateCookieHash(cookieData) {
        const str = JSON.stringify(cookieData.cookies.map(c => `${c.name}=${c.value}`).sort());
        let hash = 0;
        for (let i = 0; i < str.length; i++) {
            const char = str.charCodeAt(i);
            hash = ((hash << 5) - hash) + char;
            hash = hash & hash; // Convert to 32-bit integer
        }
        return hash.toString();
    }

    // 同步cookies到服务器
    function syncCookies(force = false) {
        try {
            const cookieData = getYouTubeCookies();
            const currentHash = calculateCookieHash(cookieData);
            
            // 检查cookies是否有变化
            if (!force && currentHash === lastCookieHash) {
                log('Cookies未发生变化，跳过同步');
                return;
            }

            log(`开始同步 ${cookieData.count} 个YouTube cookies...`);

            GM_xmlhttpRequest({
                method: 'POST',
                url: `${CONFIG.serverUrl}/api/cookies/sync`,
                headers: {
                    'Content-Type': 'application/json',
                    'User-Agent': 'Y2A-Auto-Cookie-Sync/1.0.0'
                },
                data: JSON.stringify({
                    source: 'userscript',
                    timestamp: Date.now(),
                    cookies: cookieData.netscapeFormat,
                    cookieCount: cookieData.count,
                    userAgent: navigator.userAgent,
                    url: window.location.href
                }),
                timeout: 30000,
                onload: function(response) {
                    if (response.status === 200) {
                        lastSyncTime = Date.now();
                        lastCookieHash = currentHash;
                        GM_setValue('lastSyncTime', lastSyncTime);
                        GM_setValue('lastCookieHash', lastCookieHash);
                        
                        log('Cookies同步成功', 'success');
                        showNotification(
                            'Cookie同步成功', 
                            `已同步 ${cookieData.count} 个cookies到Y2A-Auto`,
                            'success'
                        );
                        
                        // 更新状态显示
                        updateStatusDisplay('success');
                    } else {
                        log(`同步失败: HTTP ${response.status} - ${response.responseText}`, 'error');
                        showNotification(
                            'Cookie同步失败', 
                            `服务器返回错误: ${response.status}`,
                            'error'
                        );
                        updateStatusDisplay('error');
                    }
                },
                onerror: function(error) {
                    log(`同步失败: 网络错误 - ${error}`, 'error');
                    showNotification(
                        'Cookie同步失败', 
                        '无法连接到Y2A-Auto服务器',
                        'error'
                    );
                    updateStatusDisplay('error');
                },
                ontimeout: function() {
                    log('同步超时', 'warning');
                    showNotification(
                        'Cookie同步超时', 
                        '请检查Y2A-Auto服务器状态',
                        'warning'
                    );
                    updateStatusDisplay('warning');
                }
            });

        } catch (error) {
            log(`同步异常: ${error.message}`, 'error');
            showNotification(
                'Cookie同步异常', 
                error.message,
                'error'
            );
        }
    }

    // 检查cookie变化
    function checkCookieChanges() {
        try {
            const cookieData = getYouTubeCookies();
            const currentHash = calculateCookieHash(cookieData);
            
            if (currentHash !== lastCookieHash) {
                log('检测到cookie变化，触发同步');
                syncCookies();
            }
        } catch (error) {
            log(`检查cookie变化时出错: ${error.message}`, 'error');
        }
    }

    // 创建状态显示元素
    function createStatusDisplay() {
        // 避免重复创建
        if (document.getElementById('y2a-cookie-status')) return;

        const statusDiv = document.createElement('div');
        statusDiv.id = 'y2a-cookie-status';
        statusDiv.style.cssText = `
            position: fixed;
            top: 10px;
            right: 10px;
            background: rgba(0, 0, 0, 0.8);
            color: white;
            padding: 8px 12px;
            border-radius: 6px;
            font-size: 12px;
            font-family: monospace;
            z-index: 10000;
            cursor: pointer;
            transition: opacity 0.3s;
            opacity: 0.7;
        `;
        
        statusDiv.innerHTML = `
            <div>🔄 Y2A Cookie同步</div>
            <div id="y2a-status-text">初始化中...</div>
            <div id="y2a-last-sync" style="font-size: 10px; opacity: 0.8;"></div>
        `;
        
        // 点击显示详细信息
        statusDiv.addEventListener('click', () => {
            const cookieData = getYouTubeCookies();
            const lastSync = lastSyncTime ? new Date(lastSyncTime).toLocaleString() : '从未同步';
            alert(`Y2A-Auto Cookie同步状态\n\n当前cookies数量: ${cookieData.count}\n上次同步时间: ${lastSync}\n自动同步: ${CONFIG.autoSyncEnabled ? '启用' : '禁用'}\n服务器地址: ${CONFIG.serverUrl}`);
        });
        
        // 鼠标悬停显示完整信息
        statusDiv.addEventListener('mouseenter', () => {
            statusDiv.style.opacity = '1';
        });
        
        statusDiv.addEventListener('mouseleave', () => {
            statusDiv.style.opacity = '0.7';
        });

        document.body.appendChild(statusDiv);
        updateStatusDisplay('init');
    }

    // 更新状态显示
    function updateStatusDisplay(status) {
        const statusText = document.getElementById('y2a-status-text');
        const lastSyncText = document.getElementById('y2a-last-sync');
        
        if (!statusText || !lastSyncText) return;

        const statusMessages = {
            'init': '初始化中...',
            'success': '✅ 同步成功',
            'error': '❌ 同步失败',
            'warning': '⚠️ 同步超时',
            'syncing': '🔄 同步中...'
        };

        statusText.textContent = statusMessages[status] || '未知状态';
        
        if (lastSyncTime) {
            const timeAgo = Math.floor((Date.now() - lastSyncTime) / (1000 * 60));
            lastSyncText.textContent = `${timeAgo}分钟前`;
        } else {
            lastSyncText.textContent = '从未同步';
        }
    }

    // 启动定时同步
    function startAutoSync() {
        if (!CONFIG.autoSyncEnabled) return;

        // 清除现有定时器
        if (syncTimer) clearInterval(syncTimer);
        if (checkTimer) clearInterval(checkTimer);

        // 立即执行一次同步
        syncCookies(true);

        // 设置定期同步
        syncTimer = setInterval(() => {
            log('定时同步触发');
            syncCookies();
        }, CONFIG.syncInterval);

        // 设置cookie变化检查
        checkTimer = setInterval(checkCookieChanges, CONFIG.checkInterval);

        log(`自动同步已启动 - 同步间隔: ${CONFIG.syncInterval/1000/60}分钟, 检查间隔: ${CONFIG.checkInterval/1000/60}分钟`);
    }

    // 停止自动同步
    function stopAutoSync() {
        if (syncTimer) {
            clearInterval(syncTimer);
            syncTimer = null;
        }
        if (checkTimer) {
            clearInterval(checkTimer);
            checkTimer = null;
        }
        log('自动同步已停止');
    }

    // 注册菜单命令
    function registerMenuCommands() {
        GM_registerMenuCommand('🔄 立即同步Cookies', () => {
            updateStatusDisplay('syncing');
            syncCookies(true);
        });

        GM_registerMenuCommand('⚙️ 切换自动同步', () => {
            CONFIG.autoSyncEnabled = !CONFIG.autoSyncEnabled;
            if (CONFIG.autoSyncEnabled) {
                startAutoSync();
                showNotification('自动同步已启用', '将定期同步cookies到Y2A-Auto');
            } else {
                stopAutoSync();
                showNotification('自动同步已禁用', '已停止自动同步cookies');
            }
        });

        GM_registerMenuCommand('🔔 切换通知显示', () => {
            CONFIG.showNotifications = !CONFIG.showNotifications;
            showNotification(
                '通知设置已更新', 
                CONFIG.showNotifications ? '已启用通知显示' : '已禁用通知显示'
            );
        });

        GM_registerMenuCommand('📊 查看同步状态', () => {
            const cookieData = getYouTubeCookies();
            const lastSync = lastSyncTime ? new Date(lastSyncTime).toLocaleString() : '从未同步';
            const status = `Y2A-Auto Cookie同步状态

📊 统计信息:
• 当前cookies数量: ${cookieData.count}
• 上次同步时间: ${lastSync}
• 自动同步状态: ${CONFIG.autoSyncEnabled ? '✅ 启用' : '❌ 禁用'}
• 通知显示: ${CONFIG.showNotifications ? '✅ 启用' : '❌ 禁用'}

🔧 配置信息:
• 服务器地址: ${CONFIG.serverUrl}
• 同步间隔: ${CONFIG.syncInterval/1000/60}分钟
• 检查间隔: ${CONFIG.checkInterval/1000/60}分钟

💡 提示: 点击右上角状态框可查看简要信息`;
            
            alert(status);
        });
    }

    // 初始化脚本
    function init() {
        log('Y2A-Auto Cookie同步脚本启动');
        
        // 等待页面加载
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', init);
            return;
        }

        // 创建状态显示
        setTimeout(createStatusDisplay, 2000);

        // 注册菜单命令
        registerMenuCommands();

        // 启动自动同步
        if (CONFIG.autoSyncEnabled) {
            setTimeout(startAutoSync, 3000); // 延迟3秒启动，确保页面完全加载
        }

        // 监听页面可见性变化，页面重新激活时检查cookie
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden && CONFIG.autoSyncEnabled) {
                setTimeout(checkCookieChanges, 1000);
            }
        });

        log('脚本初始化完成');
        showNotification(
            'Y2A Cookie同步已启动',
            `自动同步: ${CONFIG.autoSyncEnabled ? '启用' : '禁用'}`,
            'info'
        );
    }

    // 启动脚本
    init();

})(); 