@echo off
setlocal

cd /d "%~dp0"
echo === VE2RBX OSS Build ===
echo Working directory: %CD%

for %%f in (app\launcher\local_launcher.py app\server\main.py app\server\jobs.py app\server\paths.py app\static\index.html app\static\app.js app\static\styles.css converter\modular_runner.py converter\dumper.py converter\vxm_to_vox.py converter\vox2obj.py converter\OBJ2FBX.py converter\OBJ2FBXanimation.py converter\config.json converter\version.json converter\VE2RBXicon.ico) do (
    if not exist "%%f" (
        echo Error: Missing required file: %%f
        exit /b 1
    )
)

python -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo Error: PyInstaller is not installed. Install it with: python -m pip install pyinstaller
  exit /b 1
)

if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name VE2RBX ^
  --icon "converter\VE2RBXicon.ico" ^
  --add-data "app\static;app\static" ^
  --add-data "converter;converter" ^
  --hidden-import glob ^
  --hidden-import hashlib ^
  --hidden-import datetime ^
  --hidden-import re ^
  --hidden-import struct ^
  --hidden-import math ^
  --hidden-import io ^
  --hidden-import argparse ^
  --hidden-import logging ^
  --hidden-import ctypes ^
  --hidden-import app.server.main ^
  --hidden-import app.server.jobs ^
  --hidden-import app.server.paths ^
  "app\launcher\local_launcher.py"

if errorlevel 1 exit /b 1

echo === Build Complete ===
echo Output: %CD%\dist\VE2RBX.exe
endlocal
