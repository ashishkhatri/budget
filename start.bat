@echo off
setlocal
title Budget
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto deps

echo First run: creating a private Python environment in .venv ...
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m venv .venv
) else (
  python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" goto nopython

:deps
rem Install/update packages only when requirements.txt has changed.
fc /b requirements.txt ".venv\installed-requirements.txt" >nul 2>nul
if %errorlevel%==0 goto run
echo Installing packages ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto pipfail
copy /y requirements.txt ".venv\installed-requirements.txt" >nul

:run
".venv\Scripts\python.exe" -m budget %*
echo.
echo Budget has stopped.
pause
exit /b 0

:nopython
echo.
echo Could not create a Python environment.
echo Make sure Python 3.9 or newer is installed from python.org
echo and that "Add python.exe to PATH" was checked during install.
pause
exit /b 1

:pipfail
echo.
echo Installing packages failed. Check your internet connection and run start.bat again.
pause
exit /b 1
