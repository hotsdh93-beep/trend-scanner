@echo off
cd /d "%~dp0"
set "PYL="
py -3 -c "import sys" >nul 2>&1 && set "PYL=py -3"
if not defined PYL (python -c "import sys" >nul 2>&1 && set "PYL=python")
if not defined PYL (py -c "import sys" >nul 2>&1 && set "PYL=py")
if not defined PYL (python3 -c "import sys" >nul 2>&1 && set "PYL=python3")
if not defined PYL goto nopython
%PYL% "%~dp0install.py"
pause
exit /b

:nopython
echo.
echo  Python could not be started from this window.
echo  Checked: py -3, python, py, python3
echo.
echo  --- where py ---
where py
echo  --- where python ---
where python
echo.
echo  Please send a screenshot of this window to Claude.
pause
exit /b 1
