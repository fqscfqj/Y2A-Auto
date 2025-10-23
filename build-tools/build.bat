@echo off
:: 设置UTF-8编码支持中文
chcp 65001 >nul

title Y2A-Auto Windows构建工具

echo ====================================
echo   Y2A-Auto Windows可执行文件构建工具
echo ====================================
echo.

:: 检查是否在正确的目录
if not exist "build_exe.py" (
    echo 错误: 请在build-tools目录中运行此脚本
    echo.
    pause
    exit /b 1
)

echo 检查Python环境...
python --version >nul 2>&1
if errorlevel 1 (
    echo 错误: 未找到Python环境
    echo 请先安装Python 3.11或更高版本
    echo.
    pause
    exit /b 1
)

echo ✓ Python环境检查通过
echo.

echo 开始构建过程...
echo 这可能需要几分钟时间，请耐心等待...
echo.

:: 运行构建脚本
python build_exe.py

if errorlevel 1 (
    echo.
    echo ❌ 构建失败！
    echo 请检查上方的错误信息
    echo.
    pause
    exit /b 1
)

echo.
echo ====================================
echo 🎉 构建完成！
echo ====================================
echo.
echo 📁 可执行文件位置: dist\Y2A-Auto\
echo 🚀 启动方式: 双击 dist\Y2A-Auto\start.bat
echo 🌐 Web界面: http://localhost:5000
echo 📖 使用说明: dist\Y2A-Auto\README.txt
echo.
echo 现在可以将整个 dist\Y2A-Auto 目录
echo 复制到任何Windows电脑上使用
echo.
pause 