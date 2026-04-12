@echo off
setlocal
REM ============================================================
REM  KoL Adaptive Lighting - Full Release Builder
REM  Builds the current app and then compiles the single installer
REM  that installs/updates both KoL and the CP210x ESP32 driver.
REM ============================================================

pushd "%~dp0"

echo === KoL Full Installer Build ===
echo.

REM --- Find Python ---
set "PYTHON_EXE="
if defined PYTHON (
    if exist "%PYTHON%" set "PYTHON_EXE=%PYTHON%"
)
if not defined PYTHON_EXE (
    where python >nul 2>&1
    if not errorlevel 1 set "PYTHON_EXE=python"
)
if not defined PYTHON_EXE (
    where py >nul 2>&1
    if not errorlevel 1 set "PYTHON_EXE=py"
)
if not defined PYTHON_EXE (
    echo ERROR: Python not found. Install Python 3.9+ or set PYTHON to python.exe.
    popd
    exit /b 1
)

REM --- Find Inno Setup compiler ---
set "ISCC_EXE="
where ISCC >nul 2>&1
if not errorlevel 1 set "ISCC_EXE=ISCC"
if not defined ISCC_EXE (
    if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
)
if not defined ISCC_EXE (
    if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles%\Inno Setup 6\ISCC.exe"
)
if not defined ISCC_EXE (
    echo ERROR: Inno Setup 6 compiler not found. Install it or add ISCC.exe to PATH.
    popd
    exit /b 1
)

if not exist "drivers\cp210x\silabser.inf" (
    echo ERROR: CP210x driver INF missing: drivers\cp210x\silabser.inf
    popd
    exit /b 1
)

REM --- Read installer version ---
set "APP_VERSION="
for /f "tokens=3" %%A in ('findstr /B /C:"#define MyAppVersion" installer.iss') do set "APP_VERSION=%%~A"
set APP_VERSION=%APP_VERSION:"=%
if not defined APP_VERSION set "APP_VERSION=unknown"

echo [1/4] Installing build dependencies...
"%PYTHON_EXE%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Dependency installation failed.
    popd
    exit /b 1
)
"%PYTHON_EXE%" -m pip install pyinstaller
if errorlevel 1 (
    echo ERROR: PyInstaller installation failed.
    popd
    exit /b 1
)

echo.
echo [2/4] Building current app into dist\KoL...
"%PYTHON_EXE%" -m PyInstaller kol.spec --noconfirm
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    popd
    exit /b 1
)

if not exist "dist\KoL\KoL.exe" (
    echo ERROR: dist\KoL\KoL.exe was not created.
    popd
    exit /b 1
)
if not exist "dist\KoL\data\telemetry" mkdir "dist\KoL\data\telemetry"
if not exist "dist\KoL\data\models" mkdir "dist\KoL\data\models"
if not exist "dist\KoL\data\profiles" mkdir "dist\KoL\data\profiles"

echo.
echo [3/4] Verifying the built app is from the current source...
"dist\KoL\KoL.exe" --help > "%TEMP%\kol-help.txt" 2>&1
findstr /C:"baseline" "%TEMP%\kol-help.txt" >nul 2>&1
if errorlevel 1 (
    type "%TEMP%\kol-help.txt"
    echo ERROR: Built KoL.exe does not expose the current baseline mode.
    echo        The installer would package an old app, so the build stopped.
    popd
    exit /b 1
)

echo.
echo [4/4] Compiling app + driver installer...
"%ISCC_EXE%" installer.iss
if errorlevel 1 (
    echo ERROR: Inno Setup build failed.
    popd
    exit /b 1
)

echo.
echo ============================================================
echo  INSTALLER READY
echo  Output\KoL-Setup-%APP_VERSION%.exe
echo.
echo  This one installer installs/updates KoL and stages the CP210x
echo  USB serial driver needed by many ESP32 boards.
echo ============================================================

popd
exit /b 0
