@echo off
REM TTC Positions Report - Windows Release Script
REM Creates a new release build and optionally creates installer
REM
REM Usage:
REM   release.bat              - Build and create installer
REM   release.bat --build-only - Just build, don't create installer
REM   release.bat 2.0.1        - Build with specific version
REM
REM Requirements:
REM   - PyInstaller (pip install pyinstaller)
REM   - Inno Setup (for installer): https://jrsoftware.org/isinfo.php

setlocal enabledelayedexpansion

echo ========================================
echo   TTC Positions Report - Release Tool
echo ========================================
echo.

set "APP_NAME=TTC Positions Report"
set "SCRIPT_DIR=%~dp0"
set "DIST_DIR=%SCRIPT_DIR%dist"
set "BUILD_DIR=%SCRIPT_DIR%build"
set "BUILD_ONLY=0"

REM Parse arguments
if "%~1"=="--build-only" (
    set "BUILD_ONLY=1"
    set "VERSION="
) else (
    set "VERSION=%~1"
)

REM Get version from Python file if not specified
if "%VERSION%"=="" (
    for /f "tokens=2 delims='=" %%a in ('findstr "APP_VERSION = " ttc_positions_app.py') do (
        set "VERSION=%%a"
        set "VERSION=!VERSION:"=!"
        set "VERSION=!VERSION: =!"
    )
)

echo Version: %VERSION%
echo.

REM Check for virtual environment
if not exist "venv\Scripts\activate.bat" (
    echo Error: Virtual environment not found.
    echo Run: python -m venv venv
    exit /b 1
)

REM Activate virtual environment
echo Activating virtual environment...
call venv\Scripts\activate.bat

REM Check for PyInstaller
where pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller
)

REM Clean previous builds
echo Cleaning previous builds...
if exist "%DIST_DIR%" rmdir /s /q "%DIST_DIR%"
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"

REM Build the app
echo Building application...
pyinstaller ^
    --name "%APP_NAME%" ^
    --onefile ^
    --windowed ^
    --add-data "resources;resources" ^
    --hidden-import=ib_async ^
    --hidden-import=webview ^
    --hidden-import=webview.platforms.winforms ^
    ttc_positions_app.py

if errorlevel 1 (
    echo Build failed!
    exit /b 1
)

REM Add icon if exists
if exist "installer\icon.ico" (
    echo Adding icon...
    REM Icon is embedded by PyInstaller if specified in spec file
)

REM Create resources folder alongside the exe
echo Creating external resources folder...
mkdir "%DIST_DIR%\resources\templates" 2>nul
mkdir "%DIST_DIR%\resources\static\css" 2>nul
mkdir "%DIST_DIR%\resources\static\js" 2>nul

REM Copy resources if they exist
if exist "resources" (
    xcopy /s /y "resources\*" "%DIST_DIR%\resources\"
)

echo.
echo ========================================
echo   Build Complete!
echo ========================================
echo.
echo   Exe: %DIST_DIR%\%APP_NAME%.exe
echo.

if "%BUILD_ONLY%"=="1" (
    echo Build only mode - skipping installer.
    goto :end
)

REM Check for Inno Setup
set "ISCC="
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" (
    set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
) else if exist "C:\Program Files\Inno Setup 6\ISCC.exe" (
    set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
)

if "%ISCC%"=="" (
    echo Inno Setup not found. 
    echo Install from: https://jrsoftware.org/isinfo.php
    echo.
    echo To create installer manually:
    echo   1. Install Inno Setup
    echo   2. Open installer\ttc_setup.iss
    echo   3. Click Build ^> Compile
    goto :end
)

REM Update version in Inno Setup script
echo Updating installer version to %VERSION%...
powershell -Command "(Get-Content 'installer\ttc_setup.iss') -replace '#define MyAppVersion \"[^\"]*\"', '#define MyAppVersion \"%VERSION%\"' | Set-Content 'installer\ttc_setup.iss'"

REM Create installer
echo Creating installer...
"%ISCC%" installer\ttc_setup.iss

if errorlevel 1 (
    echo Installer creation failed!
    goto :end
)

echo.
echo ========================================
echo   Installer Created!
echo ========================================
echo.
echo   Installer: Output\TTC_Positions_Setup_%VERSION%.exe
echo.

REM Ask about GitHub release
set /p "RELEASE=Would you like to create a GitHub release? (y/n): "
if /i not "%RELEASE%"=="y" goto :end

REM Check for GitHub CLI
where gh >nul 2>&1
if errorlevel 1 (
    echo.
    echo GitHub CLI not found. 
    echo Install from: https://cli.github.com/
    echo Then run: gh auth login
    echo.
    echo Manual release: https://github.com/YOUR_USERNAME/YOUR_REPO/releases/new
    echo Upload: Output\TTC_Positions_Setup_%VERSION%.exe
    goto :end
)

REM Create release
echo.
echo Enter release notes (end with an empty line):
set "NOTES="
:readnotes
set /p "LINE="
if not "%LINE%"=="" (
    set "NOTES=%NOTES%%LINE% "
    goto :readnotes
)

echo Creating GitHub release v%VERSION%...
gh release create "v%VERSION%" --title "v%VERSION%" --notes "%NOTES%" "Output\TTC_Positions_Setup_%VERSION%.exe"

if errorlevel 0 (
    echo.
    echo Release v%VERSION% created successfully!
    gh release view "v%VERSION%" --web
)

:end
echo.
echo Done!
pause

