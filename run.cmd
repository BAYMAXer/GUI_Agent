@echo off
setlocal
rem Use Windows PowerShell modules even when launched from PowerShell 7.
set "PSModulePath=%SystemRoot%\System32\WindowsPowerShell\v1.0\Modules"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0script\run-windows.ps1" %*
set "AGENT_EXIT=%ERRORLEVEL%"
if not "%AGENT_EXIT%"=="0" if "%OSWORLD_PAUSE_ON_ERROR%"=="1" pause
exit /b %AGENT_EXIT%
