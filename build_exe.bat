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
pip install -r requirements.txt
pip install pyinstaller

REM --- Build ---
echo.
echo [2/3] Running PyInstaller...
pyinstaller kol.spec --noconfirm

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
