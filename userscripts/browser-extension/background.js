// Y2A-Auto Assistant - Background Service Worker
// 处理Cookie同步和扩展核心功能

class Y2AAutoBackground {
    constructor() {
        this.config = {
            serverUrl: 'http://localhost:5000',
            syncInterval: 5 * 60 * 1000, // 5分钟
            autoSyncEnabled: true
        };
        
        this.syncTimer = null;
        this.lastSyncTime = 0;
        this.lastCookieHash = '';
        
        this.init();
    }
    
    async init() {
        console.log('Y2A-Auto Assistant 后台服务启动');
        
        await this.loadConfig();
        this.setupPeriodicSync();
        this.setupEventListeners();
        
        // 延迟执行初始同步
        setTimeout(() => this.checkAndSyncCookies(), 5000);
    }
    
    async loadConfig() {
        try {
            const stored = await chrome.storage.sync.get(['y2a_config']);
            if (stored.y2a_config) {
                this.config = { ...this.config, ...stored.y2a_config };
            }
            
            const syncData = await chrome.storage.local.get(['lastSyncTime', 'lastCookieHash']);
            this.lastSyncTime = syncData.lastSyncTime || 0;
            this.lastCookieHash = syncData.lastCookieHash || '';
        } catch (error) {
            console.error('加载配置失败:', error);
        }
    }
    
    async saveConfig() {
        try {
            await chrome.storage.sync.set({ y2a_config: this.config });
        } catch (error) {
            console.error('保存配置失败:', error);
        }
    }
    
    setupEventListeners() {
        // 统一消息处理
        chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
            this.handleMessage(message, sender, sendResponse);
            return true;
        });
        
        // 扩展安装事件
        chrome.runtime.onInstalled.addListener((details) => {
            if (details.reason === 'install') {
                this.showWelcomeNotification();
            }
        });
        
        // YouTube页面监控
        chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
            if (changeInfo.status === 'complete' && 
                tab.url && tab.url.includes('youtube.com/watch')) {
                this.checkAndSyncCookies();
            }
        });
    }
    
    async handleMessage(message, sender, sendResponse) {
        try {
            const action = message.action || message.type;
            
            switch (action) {
                case 'getConfig':
                case 'GET_CONFIG':
                    sendResponse({ success: true, config: this.config });
                    break;
                    
                case 'updateConfig':
                case 'UPDATE_CONFIG':
                    this.config = { ...this.config, ...message.config };
                    await this.saveConfig();
                    this.setupPeriodicSync();
                    sendResponse({ success: true });
                    break;
                    
                case 'syncCookies':
                case 'SYNC_COOKIES':
                    const result = await this.syncCookies(message.force || false);
                    sendResponse(result);
                    break;
                    
                case 'addVideoTask':
                    const taskResult = await this.addVideoTask(message.videoUrl);
                    sendResponse(taskResult);
                    break;
                    
                case 'getSyncStatus':
                case 'GET_STATUS':
                    const status = await this.getExtensionStatus();
                    sendResponse({ success: true, ...status });
                    break;
                    
                case 'openLoginPage':
                    chrome.tabs.create({ url: `${this.config.serverUrl}/login` });
                    sendResponse({success: true});
                    break;
                    
                case 'CHECK_AUTH':
                    const authStatus = await this.checkAuthenticationStatus();
                    sendResponse(authStatus);
                    break;
                    
                case 'login':
                    const loginResult = await this.performLogin(message.password);
                    sendResponse(loginResult);
                    break;
                    
                default:
                    sendResponse({ success: false, error: '未知操作' });
            }
        } catch (error) {
            console.error('处理消息失败:', error);
            sendResponse({ success: false, error: error.message });
        }
    }
    
    setupPeriodicSync() {
        if (this.syncTimer) {
            clearInterval(this.syncTimer);
        }
        
        if (!this.config.autoSyncEnabled) return;
        
        this.syncTimer = setInterval(() => {
            this.checkAndSyncCookies();
        }, this.config.syncInterval);
        
        console.log(`自动同步已启动，间隔: ${this.config.syncInterval / 60000} 分钟`);
    }
    
    async getAllYouTubeCookies() {
        try {
            const allCookies = await chrome.cookies.getAll({ domain: '.youtube.com' });
            
            // 关键Cookie列表
            const importantNames = new Set([
                'LOGIN_INFO', 'VISITOR_INFO1_LIVE', 'VISITOR_PRIVACY_METADATA',
                'YSC', 'PREF', 'DEVICE_INFO', 'HSID', 'SSID', 'APISID', 
                'SAPISID', 'SID', 'SIDCC', '__Secure-1PAPISID', 
                '__Secure-3PAPISID', '__Secure-1PSID', '__Secure-3PSID',
                '__Secure-1PSIDCC', '__Secure-3PSIDCC', '__Secure-1PSIDTS',
                '__Secure-3PSIDTS', '__Secure-ROLLOUT_TOKEN', '__Secure-YT_TVFAS'
            ]);
            
            const importantCookies = allCookies.filter(cookie => 
                cookie.domain === '.youtube.com' && importantNames.has(cookie.name)
            );
            
            console.log(`获取到 ${importantCookies.length} 个YouTube cookies`);
            return importantCookies;
        } catch (error) {
            console.error('获取Cookies失败:', error);
            return [];
        }
    }
    
    generateNetscapeCookies(cookies) {
        const lines = ['# Netscape HTTP Cookie File'];
        
        for (const cookie of cookies) {
            const line = [
                cookie.domain,
                'TRUE',
                cookie.path,
                cookie.secure ? 'TRUE' : 'FALSE',
                Math.floor(cookie.expirationDate || 0),
                cookie.name,
                cookie.value
            ].join('\t');
            lines.push(line);
        }
        
        return lines.join('\n');
    }
    
    calculateCookieHash(cookies) {
        const cookieString = cookies
            .map(c => `${c.name}=${c.value}`)
            .sort()
            .join('|');
        
        // 简单哈希计算
        let hash = 0;
        for (let i = 0; i < cookieString.length; i++) {
            const char = cookieString.charCodeAt(i);
            hash = ((hash << 5) - hash) + char;
            hash = hash & hash;
        }
        return hash.toString(36);
    }
    
    async checkAndSyncCookies() {
        if (!this.config.autoSyncEnabled) return;
        
        const now = Date.now();
        if (now - this.lastSyncTime < 60000) return; // 防止频繁同步
        
        const cookies = await this.getAllYouTubeCookies();
        const currentHash = this.calculateCookieHash(cookies);
        
        if (currentHash !== this.lastCookieHash) {
            console.log('检测到Cookie变化，开始同步');
            await this.syncCookies();
        }
    }
    
    // 解析服务器URL，提取认证信息
    parseServerUrl(url) {
        try {
            const urlObj = new URL(url);
            const credentials = {
                username: urlObj.username,
                password: urlObj.password
            };
            
            // 移除URL中的认证信息
            urlObj.username = '';
            urlObj.password = '';
            const cleanUrl = urlObj.toString();
            
            return { cleanUrl, credentials };
        } catch (error) {
            console.error('解析服务器URL失败:', error);
            return { cleanUrl: url, credentials: { username: '', password: '' } };
        }
    }
    
    // 创建带认证的fetch选项
    createFetchOptions(method = 'GET', body = null) {
        const { cleanUrl, credentials } = this.parseServerUrl(this.config.serverUrl);
        const headers = { 'Content-Type': 'application/json' };
        
        // 如果有认证信息，添加Authorization头
        if (credentials.username && credentials.password) {
            const auth = btoa(`${credentials.username}:${credentials.password}`);
            headers['Authorization'] = `Basic ${auth}`;
        }
        
        const options = { method, headers };
        if (body) {
            options.body = typeof body === 'string' ? body : JSON.stringify(body);
        }
        
        return { url: cleanUrl, options };
    }

    async syncCookies(force = false) {
        try {
            const cookies = await this.getAllYouTubeCookies();
            
            if (!force && cookies.length === 0) {
                return { success: false, error: '未找到有效的YouTube Cookie' };
            }
            
            const cookieData = this.generateNetscapeCookies(cookies);
            const currentHash = this.calculateCookieHash(cookies);
            
            // 发送到服务器
            const { url, options } = this.createFetchOptions('POST', {
                cookies: cookieData,
                source: 'extension',
                timestamp: Date.now(),
                cookieCount: cookies.length
            });
            
            const response = await fetch(`${url}/api/cookies/sync`, options);
            
            if (!response.ok) {
                throw new Error(`服务器返回 ${response.status}`);
            }
            
            const result = await response.json();
            
            if (result.success) {
                this.lastSyncTime = Date.now();
                this.lastCookieHash = currentHash;
                
                await chrome.storage.local.set({
                    lastSyncTime: this.lastSyncTime,
                    lastCookieHash: this.lastCookieHash
                });
                
                console.log('Cookie同步成功');
                return { success: true, cookieCount: cookies.length };
            } else {
                throw new Error(result.error || '同步失败');
            }
            
        } catch (error) {
            console.error('Cookie同步失败:', error);
            return { success: false, error: error.message };
        }
    }
    
    async addVideoTask(videoUrl) {
        try {
            const { url, options } = this.createFetchOptions('POST', {
                youtube_url: videoUrl
            });
            
            const response = await fetch(`${this.config.serverUrl}/tasks/add_via_extension`, {
                ...options,
                credentials: 'include' // Include session cookies
            });
            
            if (!response.ok) {
                if (response.status === 401) {
                    const data = await response.json();
                    const error = new Error(data.message || '需要登录');
                    error.action = data.action || 'login_required';
                    throw error;
                }
                throw new Error(`服务器返回 ${response.status}`);
            }
            
            return await response.json();
        } catch (error) {
            console.error('添加视频任务失败:', error);
            if (error.action) {
                return { success: false, error: error.message, action: error.action };
            }
            return { success: false, error: error.message };
        }
    }
    
    showNotification(title, message) {
        try {
            chrome.notifications.create({
                type: 'basic',
                iconUrl: 'icons/icon48.png',
                title: title,
                message: message
            });
        } catch (error) {
            console.error('显示通知失败:', error);
        }
    }
    
    showWelcomeNotification() {
        this.showNotification(
            'Y2A-Auto Assistant 已安装',
            '感谢安装！请在设置页面配置服务器地址。'
        );
    }
    
    async getExtensionStatus() {
        try {
            // 检查服务器连接
            const { url, options } = this.createFetchOptions('GET');
            const response = await fetch(`${url}/system_health`, {
                ...options,
                signal: AbortSignal.timeout(5000)
            });
            
            const serverStatus = response.ok ? 'success' : 'error';
            const serverMessage = response.ok ? '服务器连接正常' : `连接失败 (${response.status})`;
            
            // 检查Cookie状态
            const cookies = await this.getAllYouTubeCookies();
            const cookieStatus = cookies.length > 0 ? 'success' : 'warning';
            const cookieMessage = cookies.length > 0 
                ? `已获取 ${cookies.length} 个Cookie` 
                : '未检测到有效Cookie';
            
            return {
                serverStatus,
                serverMessage,
                cookieStatus,
                cookieMessage,
                lastSyncTime: this.lastSyncTime,
                config: this.config
            };
            
        } catch (error) {
            return {
                serverStatus: 'error',
                serverMessage: `连接失败: ${error.message}`,
                cookieStatus: 'error',
                cookieMessage: '状态检查失败',
                lastSyncTime: this.lastSyncTime,
                config: this.config
            };
        }
    }

    async checkAuthenticationStatus() {
        try {
            // Try to access a protected endpoint to check auth status
            const { url, options } = this.createFetchOptions('GET');
            const response = await fetch(`${this.config.serverUrl}/tasks`, {
                ...options,
                credentials: 'include',
                signal: AbortSignal.timeout(5000)
            });
            
            if (response.status === 401) {
                return { success: true, needsAuth: true };
            }
            
            return { success: true, needsAuth: false };
        } catch (error) {
            console.error('检查认证状态失败:', error);
            return { success: true, needsAuth: false }; // Assume no auth needed if check fails
        }
    }

    async performLogin(password) {
        try {
            // Use form data for login
            const formData = new FormData();
            formData.append('password', password);
            
            const response = await fetch(`${this.config.serverUrl}/login`, {
                method: 'POST',
                body: formData,
                credentials: 'include', // Important for session cookies
                signal: AbortSignal.timeout(10000)
            });
            
            if (response.ok) {
                // Successful login usually redirects, so check if we got redirected away from login
                if (response.redirected && !response.url.includes('/login')) {
                    return { success: true };
                }
                
                // Check if the response contains success indicators
                const text = await response.text();
                if (text.includes('登录成功') || text.includes('首页') || !text.includes('密码错误')) {
                    return { success: true };
                }
                
                return { success: false, error: '密码错误' };
            } else {
                return { success: false, error: '登录失败' };
            }
        } catch (error) {
            console.error('登录失败:', error);
            return { success: false, error: error.message };
        }
    }
}

// 启动后台服务
const y2aBackground = new Y2AAutoBackground(); 