@echo off
title Sim connection check
rem Live readout to test the connection - Ctrl+C to stop
cd /d "%~dp0"
py takeoff_logger.py --check
pause
