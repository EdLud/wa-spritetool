@echo off
REM Double-click this to open the spritetool window on Windows.
REM
REM Same job as spritetool.command on macOS: find a Python that can actually
REM run the window, say plainly what is missing when none can, and keep the
REM console open on a failure. A double-clicked .bat closes the instant it
REM ends, so anything printed on the way out is never read.
REM
REM The candidates are tried by calling a subroutine rather than looping.
REM Inside a FOR body every %VAR% is expanded once, before the first
REM iteration runs, so a loop that sets a variable and then tests it reads
REM the value the loop started with -- the test would never see what the
REM previous iteration set. CALL sidesteps that without needing
REM setlocal EnableDelayedExpansion.

cd /d "%~dp0"

REM `py` is the launcher a python.org install puts on PATH and is the most
REM reliable; `python` is what a Store or custom install leaves. Take the
REM first that can import both dependencies rather than the first that runs:
REM a machine with several Pythons usually has PySide6 in only one of them.
REM .venv first: PySide6 is ~400 MB and belongs to the machine rather than
REM the repo, so the usual way to have it here is a virtualenv beside this
REM file. Anything on PATH would otherwise win and report the dependency
REM missing while it sits installed a directory away.
set "PYTHON="
call :try_full ".venv\Scripts\python.exe"
call :try_full py -3
call :try_full python
call :try_full python3
if defined PYTHON goto :run

REM Nothing complete. Find any Python at all, so the message can name what is
REM actually absent instead of guessing.
set "FALLBACK="
call :try_any py -3
call :try_any python
call :try_any python3
if not defined FALLBACK goto :no_python

set "MISSING="
%FALLBACK% -c "import PySide6" >nul 2>&1 || set "MISSING= PySide6"
%FALLBACK% -c "import PIL" >nul 2>&1 || set "MISSING=%MISSING% Pillow"
echo.
echo spritetool needs%MISSING%, which is not installed.
echo.
echo Install it with:
echo.
echo     %FALLBACK% -m pip install%MISSING%
echo.
echo then double-click this file again.
echo.
pause
exit /b 1

:no_python
echo.
echo spritetool needs Python 3.
echo.
echo Install it from https://www.python.org/downloads/ -- tick
echo "Add Python to PATH" in the installer -- then double-click
echo this file again.
echo.
pause
exit /b 1

:run
%PYTHON% -m gui %*
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo spritetool stopped with an error.
echo.
pause
exit /b 1

REM --- subroutines -------------------------------------------------------
REM %* is the whole candidate ("py -3" arrives as two arguments), so both
REM the test and the stored value keep the interpreter's own flags.

:try_full
if defined PYTHON exit /b 0
%* -c "import PySide6, PIL" >nul 2>&1 && set "PYTHON=%*"
exit /b 0

:try_any
if defined FALLBACK exit /b 0
%* -c "" >nul 2>&1 && set "FALLBACK=%*"
exit /b 0
