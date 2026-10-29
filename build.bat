@echo off
rem Build dist\AzerothCoopServer.exe (single file, no Python needed to run it).
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller || goto :fail
python -m PyInstaller --noconfirm --onefile --console --name AzerothCoopServer ^
    --distpath dist --workpath build --specpath build coop_server.py || goto :fail
echo.
echo Built dist\AzerothCoopServer.exe
pause
exit /b 0
:fail
echo Build failed.
pause
exit /b 1
