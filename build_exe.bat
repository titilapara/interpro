@echo off
REM ================================================================
REM  Sayvo - Build the INSTANT-LAUNCH folder app from the clean venv
REM  Usage:  build_exe.bat 42
REM  Produces dist\v42\Sayvo\Sayvo.exe (+ _internal\).  Each version gets
REM  its own folder so building never touches a copy that's running.
REM  --onedir means instant launch (no 62MB unpack every start).
REM ================================================================
setlocal
set VER=%~1
if "%VER%"=="" (
    echo Usage: build_exe.bat ^<version number^>   e.g.  build_exe.bat 42
    exit /b 1
)
cd /d "%~dp0"
echo Building Sayvo v%VER% (onedir) from clean build_env...
build_env\Scripts\pyinstaller ^
  --noconfirm ^
  --onedir ^
  --windowed ^
  --name "Sayvo" ^
  --distpath "dist\v%VER%" ^
  --workpath "build\v%VER%" ^
  --icon="sayvo_icon.ico" ^
  --add-data "sayvo_icon.ico;." ^
  --add-data "silero_vad_v6.onnx;." ^
  --hidden-import customtkinter ^
  --hidden-import pyaudiowpatch ^
  --hidden-import keyboard ^
  --hidden-import onnxruntime ^
  --exclude-module scipy ^
  --collect-all customtkinter ^
  interpro_final.py

echo.
if exist "dist\v%VER%\Sayvo\Sayvo.exe" (
    echo  SUCCESS! dist\v%VER%\Sayvo\Sayvo.exe
) else (
    echo  BUILD FAILED - check the output above for errors.
    exit /b 1
)
