@echo off
title EE temperature runs - saving to ee_temperature
rem EE temperature series: runs go to ee_temperature
cd /d "%~dp0"
py takeoff_logger.py --out ee_temperature
pause
