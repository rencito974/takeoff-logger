@echo off
title EE auto runs - saving to the EE folder
rem EE automatic runs: back to the start point, full throttle to 60 KIAS, 3 runs in a row
cd /d "%~dp0"
py ee_auto.py
pause
