@echo off
setlocal EnableExtensions
cd /d "%~dp0"
rem ===== UTF-8 console: Chinese messages from the python script stay readable =====
chcp 65001 >nul 2>nul

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
echo  Bat folder: %cd%
echo ============================================
echo.

rem ===== 并发路数 =====
rem 4 = 默认，稳定够快
rem 6 = 明显更快，javbus 一般还扛得住
rem 8 = 激进，容易被反爬，出现大批失败就退回 4~6
rem 想临时试别的值：直接改下面这个 4；或者命令行传第二个参数
if not defined JAV_WORKERS set "JAV_WORKERS=4"
if not "%~2"=="" set "JAV_WORKERS=%~2"
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

rem ===== Target folder =====
rem 1) drag a folder onto this bat / pass it as the 1st CLI argument -> use it directly
rem 2) just double-click (no argument) -> open a folder picker dialog, then run
rem    (remembers the last selected folder, so the picker starts there next time)
set "TARGET=%~1"
if defined TARGET goto RUN

where powershell >nul 2>nul
if errorlevel 1 (
    echo [ERROR] PowerShell not found. Drag a folder onto this bat instead.
    pause
    exit /b 1
)
echo.
echo No folder given - opening the folder picker ...
echo Select the folder that contains the videos, then click OK.
echo.

set "JAV_START=%~dp0"
set "JAV_SCRIPT=%SCRIPT%"
set "JAV_PY_EXE=python"
set "JAV_PY_PRE="
if "%PY%"=="py -3" (
    set "JAV_PY_EXE=py"
    set "JAV_PY_PRE=-3"
)

rem The one-liner below: show a WinForms folder dialog (no cmd codepage issues,
rem all non-ASCII paths travel through env vars / Unicode argv), then launch the
rem organizer with the chosen folder. Cancel => exit code 2, nothing is touched.
powershell.exe -NoProfile -STA -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description='Select the folder containing the videos'; $d.ShowNewFolderButton=$false; $last=Join-Path $env:TEMP 'jav_organizer_last_target.txt'; $prev=''; if(Test-Path $last){ $prev=([IO.File]::ReadAllText($last,[Text.Encoding]::UTF8)).Trim() }; if($prev -and (Test-Path -LiteralPath $prev)){ $d.SelectedPath=$prev } else { $d.SelectedPath=$env:JAV_START }; if($d.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK){ Write-Host 'Cancelled - nothing was changed.'; exit 2 }; $p=$d.SelectedPath; [IO.File]::WriteAllText($last,$p,[Text.Encoding]::UTF8); Write-Host ('Target: ' + $p); if($env:JAV_PY_PRE){ & $env:JAV_PY_EXE $env:JAV_PY_PRE $env:JAV_SCRIPT $p $env:JAV_WORKERS } else { & $env:JAV_PY_EXE $env:JAV_SCRIPT $p $env:JAV_WORKERS }; exit $LASTEXITCODE"
if errorlevel 2 (
    echo.
    echo Cancelled. No files were changed.
    goto END
)
if errorlevel 1 (
    echo.
    echo [ERROR] Organizer exited with an error. See messages above.
)
goto END

:RUN
set "TARGET=%TARGET:"=%"
if "%TARGET:~-1%"=="\" set "TARGET=%TARGET:~0,-1%"
if not exist "%TARGET%\" (
    echo [ERROR] Target folder not found: %TARGET%
    pause
    exit /b 1
)
echo Target: %TARGET%
echo.
%PY% "%SCRIPT%" "%TARGET%" %JAV_WORKERS%

:END
echo.
echo Finished. You can close this window.
pause
