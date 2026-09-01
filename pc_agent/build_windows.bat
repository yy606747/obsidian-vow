@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  set "PY=py -3"
) else (
  set "PY=python"
)

%PY% -c "import sysconfig; raise SystemExit(0 if 'mingw' in sysconfig.get_platform().lower() else 1)"
if errorlevel 1 goto pip_deps

set "PACMAN=pacman"
where pacman >nul 2>nul
if not errorlevel 1 goto pacman_ready
if exist "%~d0\msys64\usr\bin\pacman.exe" set "PACMAN=%~d0\msys64\usr\bin\pacman.exe"
:pacman_ready
"%PACMAN%" -S --noconfirm --needed ^
  mingw-w64-ucrt-x86_64-pyside6 ^
  mingw-w64-ucrt-x86_64-pyinstaller ^
  mingw-w64-ucrt-x86_64-python-pillow ^
  mingw-w64-ucrt-x86_64-python-certifi || exit /b 1
goto deps_ready

:pip_deps
%PY% -m pip install --upgrade pip || exit /b 1
%PY% -m pip install -r requirements-windows.txt || exit /b 1

:deps_ready
%PY% -c "from PySide6.QtCore import QTimer; from PySide6.QtGui import QImage; from PySide6.QtWidgets import QApplication" || exit /b 1
for /f "delims=" %%I in ('%PY% -c "import certifi; print(certifi.where())"') do set "CA_BUNDLE=%%I"
if not defined CA_BUNDLE (
  echo Could not locate the certifi CA bundle
  exit /b 1
)
%PY% -m PyInstaller --clean --onefile --noconsole ^
  --name ObsidianVowPcAgent ^
  --collect-all PySide6 ^
  agent.py || exit /b 1

copy /Y config.example.json dist\config.example.json >nul
copy /Y "%CA_BUNDLE%" dist\cacert.pem >nul || exit /b 1
copy /Y run_windows.bat dist\run_windows.bat >nul || exit /b 1
copy /Y start_hidden.vbs dist\start_hidden.vbs >nul || exit /b 1
if exist dist\ObsidianVowPcAgent.exe (
  echo Built dist\ObsidianVowPcAgent.exe
) else (
  echo Build failed: dist\ObsidianVowPcAgent.exe was not created
  exit /b 1
)
%PY% -c "from PyInstaller.archive.readers import CArchiveReader; names=[str(name).lower() for name in CArchiveReader(r'dist\ObsidianVowPcAgent.exe').toc]; bad=[name for name in names if name.endswith('tcl86.dll') or name.endswith('tk86.dll')]; print('GUI archive check:', 'clean' if not bad else bad); raise SystemExit(bool(bad))" || exit /b 1
echo Create dist\config.json, then launch dist\run_windows.bat or dist\start_hidden.vbs.
