@echo off
setlocal
cd /d "%~dp0"

if not exist "main.py" (
    echo main.py was not found. Keep this build file in the project folder.
    goto failed
)
if not exist "dashboard.html" (
    echo dashboard.html was not found. Copy it into the project folder.
    goto failed
)
if not exist "autosns-mark.svg" (
    echo autosns-mark.svg was not found. Copy it into the project folder.
    goto failed
)
if not exist "assets\sidebar-icons\dashboard.png" (
    echo Sidebar icon assets were not found. Copy the assets folder into the project.
    goto failed
)
if not exist "autosns_icon.png" (
    echo autosns_icon.png was not found. Copy the app icon into the project folder.
    goto failed
)
if not exist "prepare_icon.py" (
    echo prepare_icon.py was not found. Extract the complete build ZIP first.
    goto failed
)
if not exist "cloud_settings.json" (
    echo cloud_settings.json was not found. Extract the complete build ZIP first.
    goto failed
)

set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 --version >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)
if not defined PYTHON_CMD (
    where python >nul 2>nul
    if not errorlevel 1 (
        python --version >nul 2>nul
        if not errorlevel 1 set "PYTHON_CMD=python"
    )
)
if not defined PYTHON_CMD (
    echo Python 3 was not found. Install Python 3.12 from https://www.python.org/downloads/windows/ and run this file again.
    echo During installation, enable the option to add Python to PATH.
    pause
    exit /b 1
)

if not exist ".venv-build\Scripts\python.exe" (
    %PYTHON_CMD% -m venv .venv-build
    if errorlevel 1 goto failed
)

call ".venv-build\Scripts\activate.bat"
python -m pip install --upgrade pip
if errorlevel 1 goto failed
python -m pip install "playwright>=1.50,<2" "openai>=1.50,<4" "pyinstaller>=6.10,<7" "Pillow>=10,<13" "keyring>=25,<27"
if errorlevel 1 goto failed

python validate_cloud_settings.py
if errorlevel 1 goto failed

python prepare_icon.py
if errorlevel 1 goto failed

if exist "dist\AutoSNS" rmdir /s /q "dist\AutoSNS"

python -m PyInstaller --noconfirm --clean --onefile --console ^
  --name AutoSNS ^
  --icon autosns_icon.ico ^
  --add-data "dashboard.html;." ^
  --add-data "autosns-mark.svg;." ^
  --add-data "assets\sidebar-icons;assets\sidebar-icons" ^
  --add-data "cloud_settings.json;." ^
  --collect-all playwright ^
  --collect-all openai ^
  --collect-all keyring ^
  main.py
if errorlevel 1 goto failed

echo.
echo Build complete: dist\AutoSNS.exe
echo Package it with installer\AutoSNS.iss to create the user installer.
if defined CI exit /b 0
pause
exit /b 0

:failed
echo.
echo Build failed. Review the error above.
if defined CI exit /b 1
pause
exit /b 1
