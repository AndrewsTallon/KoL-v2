@echo off
REM ============================================================
REM  KoL Adaptive Lighting - Windows Build Script
REM  Run this from the repository root (where kol.spec lives).
REM ============================================================

echo === KoL Build Script ===
echo.

REM --- Check Python ---
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.9+ and add to PATH.
    pause
    exit /b 1
)

REM --- Install / upgrade dependencies ---
echo [1/3] Installing dependencies...
python -m pip install -r requirements.txt
python -m pip install pyinstaller

REM --- Build ---
echo.
echo [2/3] Running PyInstaller...
python -m PyInstaller kol.spec --noconfirm

if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller build failed. Check output above.
    pause
    exit /b 1
)

REM --- Create data directory in output ---
echo.
echo [3/3] Preparing output...
if not exist "dist\KoL\data" mkdir "dist\KoL\data"
if not exist "dist\KoL\data\telemetry" mkdir "dist\KoL\data\telemetry"
if not exist "dist\KoL\data\models" mkdir "dist\KoL\data\models"
if not exist "dist\KoL\data\profiles" mkdir "dist\KoL\data\profiles"

REM --- CP210x driver sanity check (for installer build only) ---
echo.
if exist "drivers\cp210x\silabser.inf" (
    echo [OK] CP210x driver found at drivers\cp210x\silabser.inf
    echo      The installer will register it with pnputil during setup.
) else (
    echo [WARN] drivers\cp210x\silabser.inf NOT found.
    echo        The Inno Setup installer will still compile, but it
    echo        will NOT install the CP210x USB-to-UART driver, and
    echo        end-user PCs without this driver will not see the
    echo        ESP32 sensor as a COM port ^(Device Manager "Code 28"^).
    echo.
    echo        Download the "CP210x Universal Windows Driver" ZIP
    echo        from Silicon Labs and extract it so that
    echo            drivers\cp210x\silabser.inf
    echo        exists, then re-run this script.
    echo        See README-build.md for details.
)

echo.
echo ============================================================
echo  BUILD COMPLETE
echo  Output: dist\KoL\KoL.exe
echo.
echo  Test with:
echo    cd dist\KoL
echo    KoL.exe --dry-run
echo ============================================================
pause
