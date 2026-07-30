@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

:: Verify venv
if not exist ".venv\Scripts\python.exe" (
    echo Error: Virtual environment missing. Run install.cmd first.
    pause
    exit /b 1
)

:: Parse host and port from .env if present
set "BIND=0.0.0.0"
set "PORT=8742"
if exist ".env" (
    for /f "usebackq tokens=1,2 delims==" %%i in (".env") do (
        set "key=%%i"
        set "val=%%j"
        :: Remove spaces
        for /f "tokens=*" %%a in ("!key!") do set "key=%%a"
        for /f "tokens=*" %%a in ("!val!") do set "val=%%a"
        if "!key!"=="ALPHADESK_HOST" set "BIND=!val!"
        if "!key!"=="ALPHADESK_PORT" set "PORT=!val!"
    )
)

:: Set protocol based on SSL keys
set "PROTOCOL=http"
if exist "key.pem" (
    if exist "cert.pem" (
        set "PROTOCOL=https"
    )
)

set "OPENHOST=%BIND%"
if "%BIND%"=="0.0.0.0" set "OPENHOST=127.0.0.1"

echo Starting frontend dev server...
start /b "" cmd /c "npm --prefix frontend run dev" >nul 2>&1

echo ==================================================
echo AlphaDesk starting on %PROTOCOL%://%BIND%:%PORT%
echo Opening UI dashboard on %PROTOCOL%://%OPENHOST%:%PORT%
echo ==================================================

timeout /t 2 >nul
start "" "%PROTOCOL%://%OPENHOST%:%PORT%"

:: Run backend in foreground
".venv\Scripts\python.exe" backend\run_server.py

pause
