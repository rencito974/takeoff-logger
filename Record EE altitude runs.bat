@echo off
title EE altitude runs - saving to ee_altitude
rem EE altitude series: runs go to ee_altitude
cd /d "%~dp0"
py takeoff_logger.py --out ee_altitude
pause
