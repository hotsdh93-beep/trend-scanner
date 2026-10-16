@echo off
cd /d "%~dp0"
set "PYL="
py -3 -c "import sys" >nul 2>&1 && set "PYL=py -3"
if not defined PYL (python -c "import sys" >nul 2>&1 && set "PYL=python")
if not defined PYL (py -c "import sys" >nul 2>&1 && set "PYL=py")
if not defined PYL (echo Python not found. Run the setup file first. & pause & exit /b 1)
%PYL% "%~dp0trend_scanner.py" all --open
pause
