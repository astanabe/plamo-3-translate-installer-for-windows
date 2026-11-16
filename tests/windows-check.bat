@echo off
rem Checks an installed PLaMo 3 Translate (see tests\windows_check.py).
rem Install with install-plamo-3-translate.bat first, close the PLaMo 3 Translate
rem windows, then double-click this file.  The result is written to
rem %USERPROFILE%\plamo-3-translate-check.log.
setlocal
rem The Python that install-plamo-3-translate.bat keeps (same version as there).
set "PY=%TEMP%\plamo-3-translate-installer\python-3.14.10\python.exe"

net session >nul 2>&1
if not errorlevel 1 goto run
if "%~1"=="--elevated" goto no_admin
echo Requesting administrator privileges...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '--elevated' -Verb RunAs"
if errorlevel 1 goto no_admin
exit /b 0

:no_admin
echo This check needs administrator privileges.
goto finish

:run
if not exist "%PY%" (
    echo %PY% was not found.
    echo Run install-plamo-3-translate.bat first.
    goto finish
)
"%PY%" "%~dp0windows_check.py"

:finish
echo.
pause
