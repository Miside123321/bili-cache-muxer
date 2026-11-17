@echo off
chcp 65001 >nul
title B站缓存合并工具
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "B站缓存合并工具.pyw"
    goto :eof
)

where python >nul 2>nul
if %errorlevel%==0 (
    python "B站缓存合并工具.pyw"
    goto :eof
)

echo 未检测到 Python。
echo 请先安装 Python 3（安装时勾选 Add Python to PATH）：
echo https://www.python.org/downloads/
pause
