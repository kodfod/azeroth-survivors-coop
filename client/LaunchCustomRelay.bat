@echo off
setlocal
rem Launch Azeroth Survivors pointed at a custom co-op server.
rem Put this file in the game folder, next to AzerothSurvivors.exe.
rem Only this launch uses the custom server; starting the game normally
rem still uses the official one.

rem Default server address: one running on this PC. Guests type the host's
rem address instead (the server window prints the ones to share).
set "DEFAULT_URL=ws://127.0.0.1:8765/coop/lobby/"

if not exist "%~dp0AzerothSurvivors.exe" (
    echo AzerothSurvivors.exe was not found next to this script.
    echo Move LaunchCustomRelay.bat into the game folder and run it again.
    pause
    exit /b 1
)

set "COOP_URL="
set /p "COOP_URL=Server address [%DEFAULT_URL%]: "
if "%COOP_URL%"=="" set "COOP_URL=%DEFAULT_URL%"

set "WOW_ROGUE_COOP_URL=%COOP_URL%"
rem don't let a join code left in the environment skip the lobby
set "WOW_ROGUE_COOP_JOIN="

echo Launching with WOW_ROGUE_COOP_URL=%WOW_ROGUE_COOP_URL%

cd /d "%~dp0"
start "" "%~dp0AzerothSurvivors.exe"
endlocal
