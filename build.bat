@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -m pip install --upgrade -r requirements.txt || goto :err
py make_icon.py || goto :err
py -m PyInstaller --noconfirm --onefile --windowed --name MouseBattery ^
  --icon icon.ico --add-data "icon.ico;." main.pyw || goto :err
copy /y dist\MouseBattery.exe . >nul
rmdir /s /q build dist
del /q MouseBattery.spec
echo.
echo Готово: %~dp0MouseBattery.exe
pause
exit /b 0
:err
echo.
echo Сборка упала, скинь вывод выше.
pause
exit /b 1
