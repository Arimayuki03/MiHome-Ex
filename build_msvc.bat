@echo off
:: ============================================
:: MiHome-Ex one-click build entry (based on MiHome-Windows) (double-click friendly)
:: All build logic lives in build.ps1 to keep Nuitka
:: arguments in a single place.
:: ============================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
pause
