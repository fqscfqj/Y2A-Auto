// Y2A-Auto Assistant 设置页面脚本

document.addEventListener('DOMContentLoaded', async () => {
  await loadCurrentSettings();
  await updateStatus();
  setupEventListeners();
});

async function loadCurrentSettings() {
  try {
    const response = await chrome.runtime.sendMessage({ type: 'GET_CONFIG' });
    
    if (response && response.success) {
      const config = response.config;
      
      document.getElementById('serverUrl').value = config.serverUrl || 'http://localhost:5000';
      document.getElementById('autoSyncEnabled').value = config.autoSyncEnabled ? 'true' : 'false';
      document.getElementById('syncInterval').value = config.syncInterval || 300000;
      
      console.log('Settings loaded:', config);
    } else {
      showMessage('加载设置失败', 'error');
    }
  } catch (error) {
    console.error('Failed to load settings:', error);
    showMessage('加载设置失败: ' + error.message, 'error');
  }
}

function setupEventListeners() {
  document.getElementById('saveBtn').addEventListener('click', saveSettings);
  document.getElementById('testConnectionBtn').addEventListener('click', testConnection);
  document.getElementById('resetBtn').addEventListener('click', resetSettings);
  document.getElementById('syncNowBtn').addEventListener('click', syncNow);
  document.getElementById('encodeUrlBtn').addEventListener('click', encodeUrl);
  
  // 服务器地址输入框回车事件
  document.getElementById('serverUrl').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
      testConnection();
    }
  });

  // 编码URL输入框回车事件
  document.getElementById('urlToEncode').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
      encodeUrl();
    }
  });
}

async function encodeUrl() {
  const urlToEncodeInput = document.getElementById('urlToEncode');
  const encodeResultDiv = document.getElementById('encodeResult');
  const encodedUrlDisplay = document.getElementById('encodedUrlDisplay');
  
  encodeResultDiv.classList.add('hidden');
  encodedUrlDisplay.textContent = '';

  const url = urlToEncodeInput.value.trim();

  if (!url) {
    encodedUrlDisplay.textContent = '请输入要编码的URL或字符串';
    encodeResultDiv.className = 'test-result error';
    encodeResultDiv.classList.remove('hidden');
    return;
  }

  try {
    // 编码整个字符串，因为URL中的某些部分（如密码）可能包含特殊字符
    const encoded = encodeURIComponent(url);
    encodedUrlDisplay.textContent = encoded;
    encodeResultDiv.className = 'test-result success';
  } catch (e) {
    encodedUrlDisplay.textContent = `编码失败: ${e.message}`;
    encodeResultDiv.className = 'test-result error';
  } finally {
    encodeResultDiv.classList.remove('hidden');
  }
}

async function saveSettings() {
  const saveBtn = document.getElementById('saveBtn');
  const originalText = saveBtn.textContent;
  
  try {
    saveBtn.textContent = '保存中...';
    saveBtn.disabled = true;
    
    const serverUrl = document.getElementById('serverUrl').value.trim();
    const autoSyncEnabled = document.getElementById('autoSyncEnabled').value === 'true';
    const syncInterval = parseInt(document.getElementById('syncInterval').value);
    
    // 基本验证
    if (!serverUrl) {
      throw new Error('请输入服务器地址');
    }
    
    if (!serverUrl.startsWith('http://') && !serverUrl.startsWith('https://')) {
      throw new Error('服务器地址必须以 http:// 或 https:// 开头');
    }
    
    const config = {
      serverUrl: serverUrl.endsWith('/') ? serverUrl.slice(0, -1) : serverUrl,
      autoSyncEnabled,
      syncInterval
    };
    
    const response = await chrome.runtime.sendMessage({
      type: 'UPDATE_CONFIG',
      config
    });
    
    if (response && response.success) {
      showMessage('设置保存成功！', 'success');
      await updateStatus();
    } else {
      throw new Error(response ? response.error : '保存失败');
    }
  } catch (error) {
    console.error('Save settings failed:', error);
    showMessage('保存失败: ' + error.message, 'error');
  } finally {
    saveBtn.textContent = originalText;
    saveBtn.disabled = false;
  }
}

// 解析服务器URL，提取认证信息
function parseServerUrl(url) {
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
function createFetchOptions(url, method = 'GET', body = null) {
  const { cleanUrl, credentials } = parseServerUrl(url);
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

async function testConnection() {
  const testBtn = document.getElementById('testConnectionBtn');
  const testResult = document.getElementById('testResult');
  const originalText = testBtn.textContent;
  
      try {
      testBtn.textContent = '测试中...';
      testBtn.disabled = true;
      testResult.classList.add('hidden');
      
      const serverUrl = document.getElementById('serverUrl').value.trim();
    
    if (!serverUrl) {
      throw new Error('请先输入服务器地址');
    }
    
    const cleanServerUrl = serverUrl.endsWith('/') ? serverUrl.slice(0, -1) : serverUrl;
    const { url, options } = createFetchOptions(cleanServerUrl, 'GET');
    
    const response = await fetch(`${url}/system_health`, {
      ...options,
      signal: AbortSignal.timeout(10000)
    });
    
    testResult.classList.remove('hidden');
    
    if (response.ok) {
      const data = await response.json();
      testResult.className = 'test-result success';
      
      let serverInfo = '';
      if (data && data.version) {
        serverInfo = ` (Y2A-Auto ${data.version})`;
      }
      
      testResult.textContent = `✅ 连接成功！服务器运行正常${serverInfo}`;
    } else {
      testResult.className = 'test-result error';
      
      let errorMsg = `❌ 连接失败：服务器返回 ${response.status}`;
      
      if (response.status === 401) {
        errorMsg += ' - 认证失败';
      } else if (response.status === 403) {
        errorMsg += ' - 访问被拒绝';
      } else if (response.status === 404) {
        errorMsg += ' - 路径不存在';
      } else if (response.status >= 500) {
        errorMsg += ' - 服务器内部错误';
      }
      
      testResult.textContent = errorMsg;
    }
  } catch (error) {
    testResult.classList.remove('hidden');
    testResult.className = 'test-result error';
    
    let errorMsg = `❌ 连接失败：${error.message}`;
    
    if (error.message.includes('Failed to fetch')) {
      errorMsg = '❌ 连接失败：无法访问服务器\n请检查地址和网络连接';
    } else if (error.message.includes('timeout')) {
      errorMsg = '❌ 连接超时：请检查网络连接';
    }
    
    testResult.innerHTML = errorMsg.replace(/\n/g, '<br>');
  } finally {
    testBtn.textContent = originalText;
    testBtn.disabled = false;
  }
}

async function resetSettings() {
  if (confirm('确定要重置所有设置吗？')) {
    document.getElementById('serverUrl').value = 'http://localhost:5000';
    document.getElementById('autoSyncEnabled').value = 'true';
    document.getElementById('syncInterval').value = '300000';
    showMessage('设置已重置', 'info');
  }
}

async function syncNow() {
  const syncBtn = document.getElementById('syncNowBtn');
  const originalText = syncBtn.textContent;
  
  try {
    syncBtn.textContent = '同步中...';
    syncBtn.disabled = true;
    
    const response = await chrome.runtime.sendMessage({ type: 'SYNC_COOKIES' });
    
    if (response && response.success) {
      showMessage('Cookie同步成功！', 'success');
      await updateStatus();
    } else {
      showMessage('Cookie同步失败: ' + (response ? response.error : '未知错误'), 'error');
    }
  } catch (error) {
    console.error('Sync failed:', error);
    showMessage('Cookie同步失败: ' + error.message, 'error');
  } finally {
    syncBtn.textContent = originalText;
    syncBtn.disabled = false;
  }
}

async function updateStatus() {
  try {
    const serverUrl = await getCurrentServerUrl();
    const autoSyncStatus = await getAutoSyncStatus();
    const cookieCount = await getCookieCount();
    
    document.getElementById('currentServerUrl').textContent = serverUrl;
    document.getElementById('autoSyncStatus').textContent = autoSyncStatus;
    document.getElementById('currentCookieCount').textContent = cookieCount;
    
    // 更新最后同步时间
    const response = await chrome.runtime.sendMessage({ type: 'GET_STATUS' });
    if (response && response.lastSyncTime) {
      document.getElementById('lastSyncTime').textContent = formatTime(response.lastSyncTime);
    } else {
      document.getElementById('lastSyncTime').textContent = '从未同步';
    }
  } catch (error) {
    console.error('Update status failed:', error);
    // 显示错误信息而不是"加载中..."
    document.getElementById('currentServerUrl').textContent = '获取失败';
    document.getElementById('autoSyncStatus').textContent = '获取失败';
    document.getElementById('currentCookieCount').textContent = '获取失败';
    document.getElementById('lastSyncTime').textContent = '获取失败';
  }
}

async function getCurrentServerUrl() {
  try {
    const response = await chrome.runtime.sendMessage({ type: 'GET_CONFIG' });
    return response && response.config ? response.config.serverUrl : '未设置';
  } catch (error) {
    return '获取失败';
  }
}

async function getAutoSyncStatus() {
  try {
    const response = await chrome.runtime.sendMessage({ type: 'GET_CONFIG' });
    return response && response.config ? (response.config.autoSyncEnabled ? '已启用' : '已禁用') : '未设置';
  } catch (error) {
    return '获取失败';
  }
}

async function getCookieCount() {
  try {
    const cookies = await chrome.cookies.getAll({ domain: '.youtube.com' });
    return cookies.length.toString();
  } catch (error) {
    return '获取失败';
  }
}

function formatTime(timestamp) {
  if (!timestamp) return '从未同步';
  
  const date = new Date(timestamp);
  const now = new Date();
  const diff = now - date;

  const seconds = Math.floor(diff / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);

  if (seconds < 60) {
    return `${seconds} 秒前`;
  } else if (minutes < 60) {
    return `${minutes} 分钟前`;
  } else if (hours < 24) {
    return `${hours} 小时前`;
  } else if (days < 30) {
    return `${days} 天前`;
  } else {
    return date.toLocaleString();
  }
}

function showMessage(message, type) {
  const statusMessageDiv = document.getElementById('statusMessage');
  statusMessageDiv.textContent = message;
  statusMessageDiv.className = `status-message ${type}`;
  statusMessageDiv.classList.remove('hidden');
  
  setTimeout(() => {
    statusMessageDiv.classList.add('hidden');
  }, 5000);
} 