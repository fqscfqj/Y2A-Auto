// Y2A-Auto Assistant - Content Script
// 在YouTube页面中添加快速操作按钮

class Y2AAutoContent {
    constructor() {
        this.currentVideoUrl = '';
        this.currentVideoData = null;
        this.buttonContainer = null;
        
        this.init();
    }
    
    init() {
        console.log('Y2A-Auto Assistant Content Script 启动');
        
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => this.setupPage());
        } else {
            this.setupPage();
        }
        
        this.observePageChanges();
    }
    
    setupPage() {
        if (this.isVideoPage()) {
            this.createUI();
            this.extractVideoData();
        }
    }
    
    isVideoPage() {
        return window.location.pathname === '/watch';
    }
    
    observePageChanges() {
        let lastUrl = location.href;
        
        new MutationObserver(() => {
            const url = location.href;
            if (url !== lastUrl) {
                lastUrl = url;
                setTimeout(() => {
                    if (this.isVideoPage()) {
                        this.updateUI();
                        this.extractVideoData();
                    } else {
                        this.removeUI();
                    }
                }, 1000);
            }
        }).observe(document, { subtree: true, childList: true });
    }
    
    createUI() {
        this.removeUI();
        
        const targetContainer = this.findButtonContainer();
        if (!targetContainer) return;
        
        this.buttonContainer = document.createElement('div');
        this.buttonContainer.id = 'y2a-action-container';
        this.buttonContainer.innerHTML = `
            <button id="y2a-add-task-btn" class="y2a-action-btn" title="添加到Y2A-Auto">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/>
                </svg>
                <span>添加任务</span>
            </button>
            <button id="y2a-sync-btn" class="y2a-action-btn secondary" title="立即同步Cookie">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46C19.54 15.03 20 13.57 20 12c0-4.42-3.58-8-8-8zm0 14c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8L5.24 7.74C4.46 8.97 4 10.43 4 12c0 4.42 3.58 8 8 8v3l4-4-4-4v3z"/>
                </svg>
            </button>
        `;
        
        targetContainer.appendChild(this.buttonContainer);
        this.bindButtonEvents();
        this.injectStyles();
    }
    
    findButtonContainer() {
        const selectors = [
            '#actions .ytd-menu-renderer',
            '#menu-container #top-level-buttons-computed',
            '#actions-inner'
        ];
        
        for (const selector of selectors) {
            const element = document.querySelector(selector);
            if (element) return element;
        }
        
        return this.createFloatingContainer();
    }
    
    createFloatingContainer() {
        const container = document.createElement('div');
        container.id = 'y2a-floating-container';
        container.style.cssText = `
            position: fixed;
            top: 100px;
            right: 20px;
            z-index: 9999;
            background: rgba(0, 0, 0, 0.8);
            border-radius: 8px;
            padding: 10px;
        `;
        document.body.appendChild(container);
        return container;
    }
    
    bindButtonEvents() {
        const addTaskBtn = document.getElementById('y2a-add-task-btn');
        const syncBtn = document.getElementById('y2a-sync-btn');
        
        if (addTaskBtn) {
            addTaskBtn.addEventListener('click', () => this.addVideoTask());
        }
        
        if (syncBtn) {
            syncBtn.addEventListener('click', () => this.syncCookies());
        }
    }
    
    extractVideoData() {
        try {
            const url = window.location.href;
            const videoId = new URLSearchParams(window.location.search).get('v');
            
            if (!videoId) return;
            
            this.currentVideoUrl = url;
            
            const titleElement = document.querySelector('h1.ytd-video-primary-info-renderer yt-formatted-string') ||
                               document.querySelector('#container h1 yt-formatted-string');
            
            const channelElement = document.querySelector('#text a') ||
                                 document.querySelector('.ytd-channel-name a');
            
            this.currentVideoData = {
                id: videoId,
                title: titleElement ? titleElement.textContent.trim() : '',
                channel: channelElement ? channelElement.textContent.trim() : '',
                url: url
            };
            
            console.log('视频数据已提取:', this.currentVideoData);
            this.updateButtonState();
            
        } catch (error) {
            console.error('提取视频数据失败:', error);
        }
    }
    
    updateButtonState() {
        const addTaskBtn = document.getElementById('y2a-add-task-btn');
        if (addTaskBtn && this.currentVideoData) {
            addTaskBtn.disabled = false;
            addTaskBtn.title = `添加《${this.currentVideoData.title}》到Y2A-Auto`;
        }
    }
    
    async addVideoTask() {
        const addTaskBtn = document.getElementById('y2a-add-task-btn');
        if (!addTaskBtn || !this.currentVideoData) return;
        
        const originalText = addTaskBtn.innerHTML;
        
        try {
            addTaskBtn.innerHTML = '<span>添加中...</span>';
            addTaskBtn.disabled = true;
            
            const response = await chrome.runtime.sendMessage({
                action: 'addVideoTask',
                videoUrl: this.currentVideoUrl,
                videoData: this.currentVideoData
            });
            
            if (response && response.success) {
                this.showMessage('视频已添加到Y2A-Auto处理队列', 'success');
                addTaskBtn.innerHTML = '<span>✓ 已添加</span>';
                
                setTimeout(() => {
                    addTaskBtn.innerHTML = originalText;
                    addTaskBtn.disabled = false;
                }, 2000);
            } else {
                throw new Error(response ? response.error : '添加失败');
            }
            
        } catch (error) {
            console.error('添加视频任务失败:', error);
            this.showMessage('添加失败: ' + error.message, 'error');
            
            addTaskBtn.innerHTML = originalText;
            addTaskBtn.disabled = false;
        }
    }
    
    async syncCookies() {
        const syncBtn = document.getElementById('y2a-sync-btn');
        if (!syncBtn) return;
        
        const originalTitle = syncBtn.title;
        
        try {
            syncBtn.title = '同步中...';
            syncBtn.disabled = true;
            
            const response = await chrome.runtime.sendMessage({
                action: 'syncCookies'
            });
            
            if (response && response.success) {
                this.showMessage('Cookie同步成功', 'success');
                syncBtn.title = '✓ 同步完成';
                
                setTimeout(() => {
                    syncBtn.title = originalTitle;
                    syncBtn.disabled = false;
                }, 2000);
            } else {
                throw new Error(response ? response.error : '同步失败');
            }
            
        } catch (error) {
            console.error('Cookie同步失败:', error);
            this.showMessage('同步失败: ' + error.message, 'error');
            
            syncBtn.title = originalTitle;
            syncBtn.disabled = false;
        }
    }
    
    showMessage(message, type = 'info') {
        const messageDiv = document.createElement('div');
        messageDiv.className = `y2a-message y2a-message-${type}`;
        messageDiv.textContent = message;
        
        document.body.appendChild(messageDiv);
        
        setTimeout(() => {
            messageDiv.style.opacity = '0';
            setTimeout(() => {
                if (messageDiv.parentNode) {
                    messageDiv.parentNode.removeChild(messageDiv);
                }
            }, 300);
        }, 3000);
    }
    
    updateUI() {
        if (this.isVideoPage() && !document.getElementById('y2a-action-container')) {
            this.createUI();
        }
    }
    
    removeUI() {
        const containers = [
            'y2a-action-container',
            'y2a-floating-container'
        ];
        
        containers.forEach(id => {
            const element = document.getElementById(id);
            if (element && element.parentNode) {
                element.parentNode.removeChild(element);
            }
        });
        
        this.buttonContainer = null;
    }
    
    injectStyles() {
        if (document.getElementById('y2a-styles')) return;
        
        const style = document.createElement('style');
        style.id = 'y2a-styles';
        style.textContent = `
            #y2a-action-container {
                display: flex;
                gap: 8px;
                margin-left: 8px;
            }
            
            .y2a-action-btn {
                display: flex;
                align-items: center;
                gap: 6px;
                padding: 8px 12px;
                border: none;
                border-radius: 18px;
                background: #ff0000;
                color: white;
                cursor: pointer;
                font-size: 14px;
                font-weight: 500;
                transition: all 0.2s;
            }
            
            .y2a-action-btn:hover {
                background: #cc0000;
                transform: translateY(-1px);
            }
            
            .y2a-action-btn.secondary {
                background: #606060;
                padding: 8px;
            }
            
            .y2a-action-btn.secondary:hover {
                background: #404040;
            }
            
            .y2a-action-btn:disabled {
                background: #ccc;
                cursor: not-allowed;
                transform: none;
            }
            
            .y2a-message {
                position: fixed;
                top: 80px;
                right: 20px;
                padding: 12px 16px;
                border-radius: 4px;
                color: white;
                font-weight: 500;
                z-index: 10000;
                opacity: 1;
                transition: opacity 0.3s;
            }
            
            .y2a-message-success {
                background: #4caf50;
            }
            
            .y2a-message-error {
                background: #f44336;
            }
            
            .y2a-message-info {
                background: #2196f3;
            }
        `;
        
        document.head.appendChild(style);
    }
}

// 启动Content Script
new Y2AAutoContent(); 