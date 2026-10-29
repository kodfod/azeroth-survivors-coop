@echo off
rem Run the co-op server from source (needs Python and requirements.txt).
rem Leave this window open while playing; it prints the addresses to share.
cd /d "%~dp0"
python coop_server.py %*
pause
