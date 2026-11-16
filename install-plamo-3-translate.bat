@echo off
rem PLaMo 3 Translate installer for Windows 11 (x64).
rem
rem   install-plamo-3-translate.bat [options]       install or update in place
rem   install-plamo-3-translate.bat --uninstall     uninstall
rem
rem Run it as Administrator.  Without options it asks for elevation itself and
rem asks the settings interactively; with options, run it from an Administrator
rem command prompt.  See README.md or --help for the options.
rem
rem This file only fetches the embeddable Python package (pinned by SHA-256)
rem and runs plamo-3-translate-installer.py with it; that file does the work.
setlocal
set "PY_VERSION=3.14.10"
set "PY_ZIP=python-%PY_VERSION%-embed-amd64.zip"
set "PY_SHA256=90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"
set "PY_URL=https://www.python.org/ftp/python/%PY_VERSION%/%PY_ZIP%"
set "WORK=%TEMP%\plamo-3-translate-installer"
set "PY_DIR=%WORK%\python-%PY_VERSION%"
set "INSTALLER=%~dp0plamo-3-translate-installer.py"
set "ELEVATED="
set "NOARGS="
if "%~1"=="--elevated" set "ELEVATED=1"
if "%~1"=="" set "NOARGS=1"
if defined ELEVATED set "NOARGS=1"

if "%~1"=="--help" goto run_setup
if "%~1"=="-h" goto run_setup

rem Administrator privileges: without arguments, relaunch elevated (a new
rem window); arguments would not survive that, so with arguments just say so.
net session >nul 2>&1
if not errorlevel 1 goto run_setup
if defined ELEVATED goto no_admin
if not defined NOARGS goto no_admin
echo Requesting administrator privileges...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '--elevated' -Verb RunAs"
if errorlevel 1 goto no_admin
exit /b 0

:no_admin
echo This installer needs administrator privileges.
echo Open "Command Prompt" with "Run as administrator" and run it from there.
set "RC=1"
goto finish

:run_setup
if not exist "%INSTALLER%" (
    echo plamo-3-translate-installer.py was not found next to this file.
    set "RC=1"
    goto finish
)
if exist "%PY_DIR%\python.exe" goto run_installer
if not exist "%WORK%" mkdir "%WORK%"
call :check_zip
if not errorlevel 1 goto extract
echo Downloading %PY_ZIP%
curl.exe -L --fail -o "%WORK%\%PY_ZIP%" "%PY_URL%"
if errorlevel 1 (
    echo Download failed: %PY_URL%
    set "RC=1"
    goto finish
)
call :check_zip
if errorlevel 1 (
    echo Checksum mismatch for %PY_ZIP%; the file was removed.  Run this file again to retry.
    del "%WORK%\%PY_ZIP%"
    set "RC=1"
    goto finish
)

:extract
echo Unpacking %PY_ZIP%
if exist "%PY_DIR%.tmp" rmdir /s /q "%PY_DIR%.tmp"
mkdir "%PY_DIR%.tmp"
tar.exe -xf "%WORK%\%PY_ZIP%" -C "%PY_DIR%.tmp"
if errorlevel 1 (
    echo Cannot unpack %PY_ZIP%
    set "RC=1"
    goto finish
)
if exist "%PY_DIR%" rmdir /s /q "%PY_DIR%"
move "%PY_DIR%.tmp" "%PY_DIR%" >nul
if errorlevel 1 (
    echo Cannot create %PY_DIR%
    set "RC=1"
    goto finish
)

:run_installer
if defined NOARGS goto run_without_args
"%PY_DIR%\python.exe" "%INSTALLER%" %*
set "RC=%ERRORLEVEL%"
goto finish

:run_without_args
"%PY_DIR%\python.exe" "%INSTALLER%"
set "RC=%ERRORLEVEL%"

:finish
rem Started without arguments (e.g. by a double click): keep the window open.
if defined NOARGS (
    echo.
    pause
)
exit /b %RC%

rem Is the archive there, with the pinned SHA-256?
:check_zip
if not exist "%WORK%\%PY_ZIP%" exit /b 1
certutil -hashfile "%WORK%\%PY_ZIP%" SHA256 | findstr /i /x "%PY_SHA256%" >nul
exit /b %ERRORLEVEL%
