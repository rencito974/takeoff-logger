@echo off
title IA runs - saving to takeoff_data
rem IA (mass vs ground roll): runs go to takeoff_data
cd /d "%~dp0"
py takeoff_logger.py
pause
