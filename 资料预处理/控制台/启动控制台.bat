@echo off
chcp 936 >nul

rem 知识库 · 启动控制台（隐藏黑窗）＋ 一键安装（并入本文件）
rem 入口：双击项目根目录 启动控制台.vbs（隐藏窗口调本文件 hidden 自检）
rem 缺依赖时自动弹可见窗口安装（install 分支），装完自动启动控制台
rem 排查时可直接执行： python "%~dp0app.py" 观察真实报错
set "ROOT=%~dp0..\.."

if /i "%~1"=="install" goto :install

if /i not "%~1"=="hidden" (
    start "" wscript //nologo "%ROOT%\启动控制台.vbs"
    exit /b
)

title 知识库 · 预处理控制台
cd /d "%ROOT%"
set "KB_ERR=%TEMP%\知识库_启动错误.txt"
if exist "%KB_ERR%" del "%KB_ERR%" >nul 2>nul

where python >nul 2>nul
if errorlevel 1 (
    >"%KB_ERR%" echo 未找到 Python：请先安装 Python 3.12+（安装时务必勾选 Add to PATH），再重新双击 启动控制台.vbs。
    exit /b 1
)

python -c "import fitz, docx, openpyxl, httpx, opencc" >nul 2>nul
if errorlevel 1 (
    start "" "%~f0" install
    exit /b 0
)

where pythonw >nul 2>nul
if errorlevel 1 (
    >"%KB_ERR%" echo 未找到 pythonw：随 Python 一同安装，请重新安装 Python 3.12+ 并勾选 Add to PATH。
    exit /b 1
)

start "" pythonw "%~dp0app.py"
exit /b 0

:install
title 知识库 · 一键安装
echo ============================================
echo   知识库 · 一键安装依赖
echo ============================================
cd /d "%ROOT%"

where python >nul 2>nul
if errorlevel 1 (
    echo [失败] 未找到 Python，请先安装 Python 3.12+（勾选 Add to PATH）
    pause
    exit /b 1
)

echo [1/3] 升级 pip 工具...
python -m pip install --upgrade pip >nul 2>nul

echo [2/3] 安装依赖（pymupdf / python-docx / opencc / httpx / openpyxl）...
python -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo [提示] 标准源安装失败，尝试镜像源...
    python -m pip install -r "%~dp0requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
)

echo [3/3] 导入自检...
python -c "import fitz, docx, openpyxl, httpx, opencc; print('导入自检通过')"
if errorlevel 1 (
    echo [失败] 导入自检未通过，请查看上方报错
    pause
    exit /b 1
)

echo.
echo ============================================
echo   安装完成 [OK]，正在启动控制台...
echo   若要 OCR 扫描版 PDF，需配置 MinerU Token：
echo   在控制台「设置」页填写即可
echo   （不配 OCR 不影响纯文本通道）
echo ============================================
timeout /t 3 >nul
start "" wscript //nologo "%ROOT%\启动控制台.vbs"
exit /b 0
