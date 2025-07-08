// Y2A-Auto Browser Extension Popup Script

document.addEventListener('DOMContentLoaded', async () => {
  // Initialize popup
  await initializePopup();
  
  // Set up event listeners
  setupEventListeners();
  
  // Start status updates
  startStatusUpdates();
  
  // Check authentication status
  await checkAuthStatus();
});

async function initializePopup() {
  try {
    // Get server configuration from background script
    const response = await chrome.runtime.sendMessage({ type: 'GET_CONFIG' });
    
    if (response && response.serverUrl) {
      document.getElementById('serverAddress').textContent = response.serverUrl;
    }
    
    // Get initial status
    await updateStatus();
  } catch (error) {
    console.error('Failed to initialize popup:', error);
  }
}

async function checkAuthStatus() {
  try {
    const response = await chrome.runtime.sendMessage({ type: 'CHECK_AUTH' });
    
    if (response && response.needsAuth) {
      showLoginSection();
    } else {
      showMainContent();
    }
  } catch (error) {
    console.error('Failed to check auth status:', error);
    // Assume no auth needed if check fails
    showMainContent();
  }
}

function showLoginSection() {
  document.getElementById('loginSection').classList.add('show');
  document.getElementById('mainContent').classList.add('hidden');
  // Focus on password input
  setTimeout(() => {
    document.getElementById('passwordInput').focus();
  }, 100);
}

function showMainContent() {
  document.getElementById('loginSection').classList.remove('show');
  document.getElementById('mainContent').classList.remove('hidden');
}

function setupEventListeners() {
  // Login button
  document.getElementById('loginBtn').addEventListener('click', async () => {
    await handleLogin();
  });

  // Enter key in password field
  document.getElementById('passwordInput').addEventListener('keypress', async (e) => {
    if (e.key === 'Enter') {
      await handleLogin();
    }
  });

  // Web login button
  document.getElementById('openWebLoginBtn').addEventListener('click', async () => {
    await chrome.runtime.sendMessage({ action: 'openLoginPage' });
    window.close();
  });

  // Sync now button - 合并了原来的立即同步和紧急修复功能
  document.getElementById('syncNowBtn').addEventListener('click', async () => {
    const button = document.getElementById('syncNowBtn');
    const originalText = button.textContent;
    button.textContent = '🔄 同步中...';
    button.disabled = true;
    
    try {
      // 使用强制同步，应对YouTube严格检测
      const response = await chrome.runtime.sendMessage({ 
        type: 'SYNC_COOKIES', 
        force: true 
      });
      
      if (response && response.success) {
        updateCookieStatus('success', '同步成功');
        
        // 显示成功状态
        button.textContent = '✅ 同步完成';
        setTimeout(() => {
          button.textContent = originalText;
        }, 2000);
      } else {
        updateCookieStatus('error', '同步失败');
        button.textContent = '❌ 同步失败';
        setTimeout(() => {
          button.textContent = originalText;
        }, 2000);
      }
    } catch (error) {
      console.error('Sync failed:', error);
      updateCookieStatus('error', '同步失败');
      button.textContent = '❌ 同步失败';
      setTimeout(() => {
        button.textContent = originalText;
      }, 2000);
    } finally {
      button.disabled = false;
    }
  });

  // Open settings button
  document.getElementById('openSettingsBtn').addEventListener('click', async () => {
    try {
      // 打开扩展设置页面
      chrome.runtime.openOptionsPage();
    } catch (error) {
      console.error('Failed to open options page:', error);
      // 备用方案：尝试直接打开Y2A-Auto设置页面
      try {
        const response = await chrome.runtime.sendMessage({ type: 'GET_CONFIG' });
        
        if (response && response.serverUrl) {
          chrome.tabs.create({ url: `${response.serverUrl}/settings` });
        } else {
          chrome.tabs.create({ url: 'http://localhost:5000/settings' });
        }
      } catch (error2) {
        console.error('Failed to open Y2A-Auto settings:', error2);
      }
    }
  });
}

async function handleLogin() {
  const loginBtn = document.getElementById('loginBtn');
  const passwordInput = document.getElementById('passwordInput');
  const password = passwordInput.value.trim();

  if (!password) {
    passwordInput.focus();
    return;
  }

  const originalText = loginBtn.textContent;
  loginBtn.textContent = '登录中...';
  loginBtn.disabled = true;

  try {
    const response = await chrome.runtime.sendMessage({
      action: 'login',
      password: password
    });

    if (response && response.success) {
      // Login successful
      passwordInput.value = '';
      showMainContent();
      await updateStatus(); // Refresh status after login
    } else {
      // Login failed
      const errorMsg = response ? response.error : '登录失败';
      alert(errorMsg);
      passwordInput.select();
    }
  } catch (error) {
    console.error('Login failed:', error);
    alert('登录失败: ' + error.message);
    passwordInput.select();
  } finally {
    loginBtn.textContent = originalText;
    loginBtn.disabled = false;
  }
}

function startStatusUpdates() {
  // Update status every 10 seconds
  setInterval(updateStatus, 10000);
}

async function updateStatus() {
  try {
    // Get status from background script
    const response = await chrome.runtime.sendMessage({ type: 'GET_STATUS' });
    
    if (response) {
      updateCookieStatus(response.cookieStatus, response.cookieMessage);
      updateServerStatus(response.serverStatus, response.serverMessage);
    }
  } catch (error) {
    console.error('Failed to update status:', error);
    updateCookieStatus('error', '状态获取失败');
    updateServerStatus('error', '状态获取失败');
  }
}

function updateCookieStatus(status, message) {
  const dot = document.getElementById('cookieStatusDot');
  const text = document.getElementById('cookieStatusText');
  
  // Remove all status classes
  dot.className = 'status-dot';
  
  // Add new status class
  dot.classList.add(status);
  
  // Update text
  text.textContent = message;
}

function updateServerStatus(status, message) {
  const dot = document.getElementById('serverStatusDot');
  const text = document.getElementById('serverStatusText');
  
  // Remove all status classes
  dot.className = 'status-dot';
  
  // Add new status class
  dot.classList.add(status);
  
  // Update text
  text.textContent = message;
} 