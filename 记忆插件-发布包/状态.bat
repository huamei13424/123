@echo off
cd /d "%~dp0"
if exist "%~dp0..\python\python.exe" (
  "%~dp0..\python\python.exe" ״̬.py
) else (
  python ״̬.py
)
echo.
pause
