@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
  python "%~dp0compress.py" --gui
  exit /b %ERRORLEVEL%
)

REM Drag-and-drop: compress to 512 MB by default
python "%~dp0compress.py" "%~1" -s 512 -f
echo.
pause
