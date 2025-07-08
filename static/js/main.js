// 主JavaScript文件
document.addEventListener('DOMContentLoaded', function() {
    console.log('Y2A-Auto 已加载');

    // --- 设置页面的密码保护逻辑 ---
    const settingsForm = document.querySelector('form[method="post"][enctype="multipart/form-data"]');
    if (settingsForm) {
        const newPassword = document.getElementById('new_password');
        const confirmPassword = document.getElementById('confirm_password');
        const passwordError = document.getElementById('password-match-error');
        const passwordProtectionEnabled = document.getElementById('password_protection_enabled');
        const passwordFields = document.getElementById('password-fields');

        if (passwordProtectionEnabled) {
            function togglePasswordFields() {
                if (passwordProtectionEnabled.checked) {
                    passwordFields.style.display = 'block';
                } else {
                    passwordFields.style.display = 'none';
                }
            }

            // Initial state
            togglePasswordFields();
            passwordProtectionEnabled.addEventListener('change', togglePasswordFields);
        }

        settingsForm.addEventListener('submit', function(event) {
            if (newPassword && confirmPassword && newPassword.value !== confirmPassword.value) {
                event.preventDefault(); // 阻止表单提交
                if (passwordError) {
                    passwordError.style.display = 'block';
                }
            } else {
                if (passwordError) {
                    passwordError.style.display = 'none';
                }
            }
        });
    }


    // --- 设置页面的日志清理按钮逻辑 ---
    // 绑定手动日志清理按钮
    const manualCleanupBtn = document.getElementById('manual-cleanup-btn');
    if(manualCleanupBtn) {
        manualCleanupBtn.addEventListener('click', function() {
            if (confirm('确定要手动清理旧日志吗？此操作将根据当前设置保留最近的日志文件。')) {
                document.getElementById('cleanup-form').submit();
            }
        });
    }

    // 绑定立即清空日志按钮
    const clearLogsBtn = document.getElementById('clear-logs-btn');
    const confirmClearBtn = document.getElementById('confirm-clear-btn');
    const cancelClearBtn = document.getElementById('cancel-clear-btn');
    const clearWarning = document.getElementById('clear-warning');

    if (clearLogsBtn) {
        clearLogsBtn.addEventListener('click', function() {
            clearLogsBtn.classList.add('d-none');
            confirmClearBtn.classList.remove('d-none');
            cancelClearBtn.classList.remove('d-none');
            clearWarning.classList.remove('d-none');
        });
    }

    if (cancelClearBtn) {
        cancelClearBtn.addEventListener('click', function() {
            clearLogsBtn.classList.remove('d-none');
            confirmClearBtn.classList.add('d-none');
            cancelClearBtn.classList.add('d-none');
            clearWarning.classList.add('d-none');
        });
    }
    
    if (confirmClearBtn) {
        confirmClearBtn.addEventListener('click', function() {
             document.getElementById('clear-form').submit();
        });
    }
}); 