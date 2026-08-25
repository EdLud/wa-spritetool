@echo off
REM Double-click this to open the spritetool window on Windows.
REM
REM Same job as launcher.command on macOS: find a Python that can actually
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

REM Offer to install rather than print a pip line. A novice told to run pip
REM meets whatever their Python decides about installing into itself, and on
REM a managed one that is a refusal with --break-system-packages as the
REM suggested way round it. A virtual environment avoids the question and is
REM one folder they can delete.
echo.
echo spritetool needs two things it can install for you:
echo.
echo   Pillow      15 MB   reads and writes the pictures
echo   PySide6    364 MB   draws the window itself
echo.
echo They go into a .venv folder beside this file and touch nothing
echo else on your PC. Delete that folder to undo it.
echo.
set "ANSWER="
set /p "ANSWER=Install them now? [y/N] "
if /i not "%ANSWER:~0,1%"=="y" goto :declined

echo.
echo Creating .venv...
%FALLBACK% -m venv .venv
if errorlevel 1 (
    echo.
    echo Could not create the .venv folder. Check that you can write to:
    echo     %CD%
    echo.
    pause
    exit /b 1
)
echo Downloading ^(this takes a few minutes the first time^)...
REM PySide6-Essentials, not PySide6: the meta-package pulls in Addons too --
REM WebEngine, 3D, Multimedia, Charts -- for 1.2 GB against 364 MB, and the
REM window uses QtCore, QtGui and QtWidgets, all of which are here.
".venv\Scripts\python.exe" -m pip install --quiet --no-cache-dir pillow PySide6-Essentials
if errorlevel 1 (
    echo.
    echo The download did not finish. Try again, or do it yourself:
    echo.
    echo     .venv\Scripts\python.exe -m pip install pillow PySide6-Essentials
    echo.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -c "import PySide6, PIL" >nul 2>&1
if errorlevel 1 (
    echo.
    echo The install finished but the window still cannot start. Run this
    echo to see why:
    echo.
    echo     .venv\Scripts\python.exe -m gui
    echo.
    pause
    exit /b 1
)
set PYTHON=".venv\Scripts\python.exe"
echo Done.
echo.
goto :run

:declined
echo.
echo Nothing installed.
echo.
echo To do it yourself:
echo.
echo     %FALLBACK% -m venv .venv
echo     .venv\Scripts\python.exe -m pip install pillow PySide6-Essentials
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
