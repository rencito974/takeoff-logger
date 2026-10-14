@echo off
title IA runs - saving to the IA folder
rem IA (mass vs ground roll): runs go to the IA folder
cd /d "%~dp0"
py takeoff_logger.py
pause
