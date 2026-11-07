@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto prepare_existing
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -X utf8 bootstrap.py
goto prepared
:use_python
python -X utf8 bootstrap.py
goto prepared
:prepare_existing
".venv\Scripts\python.exe" -X utf8 bootstrap.py
:prepared
if errorlevel 1 goto failed
:launch
if "%~1"=="" goto doctor
".venv\Scripts\python.exe" -X utf8 main.py %*
exit /b %errorlevel%
:doctor
".venv\Scripts\python.exe" -X utf8 main.py doctor
if errorlevel 1 goto failed
pause
exit /b 0
:failed
echo Setup or execution failed. See the message above.
pause
exit /b 1
