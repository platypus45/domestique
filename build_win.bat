@echo off
REM Build Domestique for Windows
REM Output: dist\Domestique\Domestique.exe

cd /d "%~dp0"

echo === Domestique Windows Build ===

REM 1. Create virtual environment if needed
if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)
call venv\Scripts\activate.bat

REM 2. Install dependencies
echo Installing dependencies...
pip install -r requirements.txt pyinstaller

REM 3. Create assets dir if missing
if not exist assets mkdir assets

REM 4. Build with PyInstaller
echo Building...
pyinstaller domestique.spec --clean --noconfirm

echo.
echo === Build complete ===
echo Executable: dist\Domestique\Domestique.exe
echo.
echo To create an installer, use Inno Setup with the generated files.
echo To run: dist\Domestique\Domestique.exe
pause
