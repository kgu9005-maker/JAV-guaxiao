@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    echo [ERROR] Python not found. Install Python 3.8+ and check "Add Python to PATH".
    pause
    exit /b 1
)

echo ============================================
echo  Video Organizer
echo  Folder: %cd%
echo ============================================
echo.

rem ===== 并发路数 =====
rem 4 = 默认，稳定够快
rem 6 = 明显更快，javbus 一般还扛得住
rem 8 = 激进，容易被反爬，出现大批失败就退回 4~6
rem 想临时试别的值：直接改下面这个 4；或者命令行传第二个参数
if not defined JAV_WORKERS set "JAV_WORKERS=4"
echo [workers] %JAV_WORKERS% lanes
echo.

rem ===== 命名规则：番号后追加分辨率 =====
rem 1 = 开启（默认）：ABP-123 1080p / ABP-123 2K / ABP-123 4K
rem 0 = 关闭：恢复纯番号命名 ABP-123
rem 依赖 ffprobe 读取真实分辨率；没有 ffprobe 时自动从文件名识别 4K/1080p 等标签
if not defined JAV_APPEND_RES set "JAV_APPEND_RES=1"
echo [naming] resolution suffix: %JAV_APPEND_RES%
echo.

rem ===== Proxy for FC2 (fc2ppvdb / fc2 official are blocked in CN) =====
rem Auto-detect: checks common local ports, uses the first one listening.
rem To force a specific proxy, uncomment the next line and edit the port:
rem set "JAV_PROXY=http://127.0.0.1:7897"

if not defined JAV_PROXY (
    for %%P in (7897 7890 7891 10809 10808 1080 8888) do (
        if not defined JAV_PROXY (
            netstat -ano | findstr /r /c:"127.0.0.1:%%P .*LISTENING" >nul 2>nul && (
                set "JAV_PROXY=http://127.0.0.1:%%P"
            )
        )
    )
)
if defined JAV_PROXY (
    echo [proxy] %JAV_PROXY%
) else (
    echo [proxy] not detected - FC2 may fail, JAV still works
)
echo.

rem ===== Target folder: drag-and-drop argument, or the bat's own folder =====
set "TARGET=%~1"
if not defined TARGET set "TARGET=%~dp0."
set "TARGET=%TARGET:"=%"
if "%TARGET:~-1%"=="\" set "TARGET=%TARGET:~0,-1%"
if not exist "%TARGET%\" (
    echo [ERROR] Target folder not found: %TARGET%
    pause
    exit /b 1
)
echo Target: %TARGET%
echo.

rem ===== Locate jav_organizer.py: next to bat first, else scan Y:/ =====
set "SCRIPT="
if exist "%~dp0jav_organizer.py" set "SCRIPT=%~dp0jav_organizer.py"
if not defined SCRIPT (
    for /d %%D in ("Y:/*") do (
        if not defined SCRIPT if exist "%%D\jav_organizer.py" set "SCRIPT=%%D\jav_organizer.py"
    )
)
if not defined SCRIPT (
    for /d %%D in ("Y:/*") do (
        if not defined SCRIPT if exist "%%D\*\jav_organizer.py" set "SCRIPT=%%D\*\jav_organizer.py"
    )
)
if not defined SCRIPT (
    echo [ERROR] jav_organizer.py not found. Put this bat next to it.
    pause
    exit /b 1
)

%PY% -c "import PIL" >nul 2>nul
if errorlevel 1 (
    echo Installing Pillow for portrait poster ...
    %PY% -m pip install pillow -q -i https://pypi.tuna.tsinghua.edu.cn/simple
)

%PY% "%SCRIPT%" "%TARGET%" %JAV_WORKERS%
echo.
echo Finished. You can close this window.
pause
