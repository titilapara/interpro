@echo off
REM ================================================================
REM  Sayvo - Build a versioned Windows exe from the CLEAN venv
REM  Usage:  build_exe.bat 24        -> dist\Sayvo_v24.exe
REM  The build_env venv contains ONLY Sayvo's dependencies, so the
REM  exe stays small (system Python has torch/paddle/cv2 which used
REM  to bloat builds to 450MB).
REM ================================================================
if "%~1"=="" (
    echo Usage: build_exe.bat VERSION_NUMBER   e.g.  build_exe.bat 24
    exit /b 1
)
set NAME=Sayvo_v%~1

echo Building %NAME%.exe from clean build_env...
build_env\Scripts\pyinstaller ^
  --noconfirm ^
  --onefile ^
  --windowed ^
  --name "%NAME%" ^
  --icon="sayvo_icon.ico" ^
  --add-data "sayvo_icon.ico;." ^
  --hidden-import customtkinter ^
  --hidden-import pyaudiowpatch ^
  --hidden-import keyboard ^
  --collect-all customtkinter ^
  interpro_final.py

echo.
if exist dist\%NAME%.exe (
    echo  SUCCESS! Your app is at: dist\%NAME%.exe
) else (
    echo  BUILD FAILED - check the output above for errors.
)
