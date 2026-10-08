@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0script\setup-windows.ps1" %*
set "AGENT_EXIT=%ERRORLEVEL%"
if not "%AGENT_EXIT%"=="0" pause
exit /b %AGENT_EXIT%
