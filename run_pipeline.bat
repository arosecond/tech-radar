@echo off
setlocal

set WORKDIR=C:\Projects\tech-radar
set VENV_PYTHON=%WORKDIR%\.venv\Scripts\python.exe
set LOGDIR=%WORKDIR%\data\logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

:: Force UTF-8 for stdout/stderr so non-ASCII (math symbols in arXiv titles, etc.)
:: don't crash Rich/log writes via the default Windows cp932.
set PYTHONIOENCODING=utf-8

set LOGFILE=%LOGDIR%\pipeline_%date:~0,4%%date:~5,2%%date:~8,2%.log

echo [%date% %time%] START >> "%LOGFILE%"

:: Docker Engine start
echo [%date% %time%] Starting Docker Engine... >> "%LOGFILE%"
net start com.docker.service >> "%LOGFILE%" 2>&1
start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"

:: Wait until Docker daemon is ready (up to 300 sec)
set /a WAIT=0
:waitdocker
docker ps >nul 2>&1
if %errorlevel% == 0 goto dockerready
set /a WAIT+=1
if %WAIT% geq 60 (
    echo [%date% %time%] ERROR: Docker not ready after 300s >> "%LOGFILE%"
    "%VENV_PYTHON%" -m tech_radar.cli notify-docker-fail --log "%LOGFILE%" >> "%LOGFILE%" 2>&1
    goto end
)
timeout /t 5 /nobreak >nul
goto waitdocker

:dockerready
echo [%date% %time%] Docker ready >> "%LOGFILE%"

:: Run tech-radar pipeline
cd /d "%WORKDIR%"
"%VENV_PYTHON%" -m tech_radar.cli run >> "%LOGFILE%" 2>&1

:end
echo [%date% %time%] END >> "%LOGFILE%"
endlocal
