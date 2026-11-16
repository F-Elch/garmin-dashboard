@echo off
rem Doppelklick: Garmin-Daten laden und garmin-dashboard.html neu bauen
cd /d "%~dp0"
python update.py %*
echo.
pause
