@echo off
title EE runs - saving to the EE folder
rem EE: runs go to the EE folder, sorted into the temperature and altitude series
cd /d "%~dp0"
py ee_logger.py
pause
