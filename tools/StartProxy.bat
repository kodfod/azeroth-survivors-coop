@echo off
rem Developer tool: run the logging proxy between the game and the official
rem co-op servers. Launch the game with LaunchCustomRelay.bat and the default
rem address. Captures land in tools\logs\. Leave this window open while
rem playing; close it (or Ctrl+C) when done.
cd /d "%~dp0"
python relay_proxy.py %*
pause
