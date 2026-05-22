@echo off
echo.
echo  ================================================
echo   InterPro - Building Windows Executable
echo  ================================================
echo.

pip install pyinstaller --quiet

echo Building InterPro.exe...
pyinstaller --onefile --windowed --name "InterPro" --icon="interpro_icon.ico" interpro_final.py

echo.
echo Done! Your app is at: dist\InterPro.exe
echo You can move it to your Desktop and double-click it anytime.
echo.
pause
