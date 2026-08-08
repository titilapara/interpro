@echo off
REM ================================================================
REM  Sayvo - Build the INSTANT-LAUNCH folder app from the clean venv
REM  Usage:  build_exe.bat
REM  Produces dist\Sayvo\Sayvo.exe (+ _internal\).  --onedir means the
REM  app launches instantly (no 62MB unpack every start like --onefile).
REM  The version is tracked by __version__ inside the app (shown in the
REM  header pill / Settings), not in the exe name.
REM ================================================================
echo Building instant-launch Sayvo (onedir) from clean build_env...
build_env\Scripts\pyinstaller ^
  --noconfirm ^
  --onedir ^
  --windowed ^
  --name "Sayvo" ^
  --icon="sayvo_icon.ico" ^
  --add-data "sayvo_icon.ico;." ^
  --hidden-import customtkinter ^
  --hidden-import pyaudiowpatch ^
  --hidden-import keyboard ^
  --collect-all customtkinter ^
  interpro_final.py

echo.
if exist dist\Sayvo\Sayvo.exe (
    echo  SUCCESS! Instant-launch app: dist\Sayvo\Sayvo.exe
) else (
    echo  BUILD FAILED - check the output above for errors.
)
