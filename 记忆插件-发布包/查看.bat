@echo off
cd /d "%~dp0"
if exist "%~dp0..\python\python.exe" (
  "%~dp0..\python\python.exe" 查看.py %*
) else (
  python 查看.py %*
)
if "%~1"=="" pause
