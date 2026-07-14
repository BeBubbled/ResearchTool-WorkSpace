@echo off
rem Starts the signed-policy-safe PowerShell launcher without changing policy.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_web_panel.ps1" %*
exit /b %ERRORLEVEL%
