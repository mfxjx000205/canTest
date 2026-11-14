@echo off
setlocal enabledelayedexpansion
title CANtest Control Plane

rem ============================================================
rem  CANtest launcher  (repo root; double-click to run)
rem
rem    start-cantest.bat          -> free port auto-pick, open UI
rem    start-cantest.bat 8710     -> fixed port
rem    start-cantest.bat probe    -> hardware/backend self-check only
rem    start-cantest.bat reset    -> delete and rebuild .venv
rem
rem  First run builds .venv and installs deps. Keep this file ASCII
rem  only and CRLF only: cmd.exe misparses non-ASCII comments under a
rem  non-UTF8 code page, and LF-only .bat breaks parenthesised blocks
rem  and goto labels. .gitattributes pins the eol for this reason.
rem ============================================================

rem  %~dp0 already ends with a backslash; trim it so the paths below
rem  print without a doubled separator.
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
if not exist "%ROOT%\pyproject.toml" (
  echo [ERROR] pyproject.toml not found next to this script.
  echo         expected: %ROOT%\pyproject.toml
  echo         this launcher must stay in the repo root.
  pause
  exit /b 1
)
cd /d "%ROOT%"
set "PYEXE=%ROOT%\.venv\Scripts\python.exe"

echo ============================================
echo   CANtest launcher
echo   dir: %ROOT%
echo ============================================
echo.

rem ---------- reset ----------
if /i "%~1"=="reset" (
  echo [reset] removing .venv ...
  if exist "%ROOT%\.venv" rmdir /s /q "%ROOT%\.venv"
  echo [reset] done. Re-run this script to rebuild.
  echo.
  pause
  exit /b 0
)

rem ---------- venv ----------
if exist "%PYEXE%" goto have_venv

echo [1/4] creating virtual env .venv ...
set "BASEPY="
where py >nul 2>nul && set "BASEPY=py -3"
if not defined BASEPY (
  where python >nul 2>nul && set "BASEPY=python"
)
if not defined BASEPY (
  echo [ERROR] Python not found. Install Python 3.9+ and tick "Add python.exe to PATH".
  pause
  exit /b 1
)
%BASEPY% -m venv "%ROOT%\.venv"
if errorlevel 1 (
  echo [ERROR] failed to create virtual env.
  pause
  exit /b 1
)

:have_venv
"%PYEXE%" -c "import sys;print(sys.version.split()[0])" > "%TEMP%\cantest_pyver.txt" 2>nul
set /p PYVER=<"%TEMP%\cantest_pyver.txt"
del "%TEMP%\cantest_pyver.txt" >nul 2>nul
echo [1/4] python ready: !PYVER!

rem ---------- deps ----------
rem  Probe by import: catches both "not installed" and "installed but
rem  binary-mismatched" (e.g. a pydantic_core built for another Python).
"%PYEXE%" -c "import fastapi, uvicorn, pydantic, can, cantest" >nul 2>nul
if not errorlevel 1 goto deps_ok

echo [2/4] installing dependencies ^(needs internet, 1-3 min^) ...
"%PYEXE%" -m pip install -e ".[dev]"
if errorlevel 1 (
  echo.
  echo [ERROR] pip install failed.
  echo         Slow network, try a mirror:
  echo    "%PYEXE%" -m pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple
  pause
  exit /b 1
)

"%PYEXE%" -c "import fastapi, uvicorn, pydantic, can, cantest" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] dependencies still broken after install.
  echo         This usually means .venv holds binaries from a different Python.
  echo         Fix it with a clean rebuild:
  echo.
  echo         start-cantest.bat reset
  echo         start-cantest.bat
  echo.
  pause
  exit /b 1
)
goto deps_done

:deps_ok
echo [2/4] dependencies ok

:deps_done

rem ---------- ws protocol guard ----------
rem  uvicorn imports websockets lazily, so a broken websockets only blows up
rem  at serve time. websockets >= 13 needs Python 3.11+ (typing.Self), which
rem  breaks 3.9/3.10 installs. Detect it here and pin a compatible version.
"%PYEXE%" -c "import uvicorn.protocols.websockets.auto" >nul 2>nul
if errorlevel 1 (
  echo [2b] websockets incompatible with this Python, pinning to 12.x ...
  "%PYEXE%" -m pip install "websockets<13" 2>nul
  "%PYEXE%" -c "import uvicorn.protocols.websockets.auto" >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] websocket protocol still unusable.
    echo         Run:  start-cantest.bat reset  then re-run this script.
    pause
    exit /b 1
  )
)

rem ---------- clock resolution check ----------
rem  Frame timestamps go through cantest.clock, which samples perf_counter
rem  and anchors it to the wall clock. That gives ~100 ns steps even on
rem  Python 3.10/Windows, where time.time() alone is 15.6 ms. So a coarse
rem  system clock is informational here, not a problem: ask the library
rem  what it actually achieves instead of guessing from the Python version.
set "CLOCKCHK=%TEMP%\cantest_clock.py"
> "%CLOCKCHK%" echo from cantest import clock
>>"%CLOCKCHK%" echo d = clock.describe()
>>"%CLOCKCHK%" echo print(d["effective_resolution_ns"], "COARSE" if d["coarse"] else "FINE")
"%PYEXE%" "%CLOCKCHK%" > "%CLOCKCHK%.out" 2>nul
set "CLOCKLINE="
set /p CLOCKLINE=<"%CLOCKCHK%.out"
del "%CLOCKCHK%" "%CLOCKCHK%.out" >nul 2>nul
rem  Split "100 COARSE" by plain string substitution - no for /f, which is
rem  unreliable here (non-UTF8 console code page).
set "CLOCKNS=%CLOCKLINE:COARSE=%"
set "CLOCKNS=%CLOCKNS:FINE=%"
set "CLOCKNS=%CLOCKNS: =%"
set "COARSECHK=%CLOCKLINE:COARSE=%"
if not "%COARSECHK%"=="%CLOCKLINE%" (
  echo.
  echo [i] frame timestamps: %CLOCKNS% ns  ^(high-precision clock^)
  echo     system wall clock is coarse ^(~15.6 ms^), expected on Python
  echo     3.10/Windows. Frame intervals and TX/RX ordering are unaffected.
  echo     Python 3.11+ would make the wall clock itself precise too, but
  echo     it is not required.
  echo.
)

rem ---------- probe ----------
if /i "%~1"=="probe" (
  echo.
  "%PYEXE%" -m cantest probe 2>nul
  echo.
  pause
  exit /b 0
)

rem ---------- port ----------
rem  Port picking is delegated to Python: netstat/findstr parsing is
rem  unreliable on non-UTF8 console code pages, and Python is a hard
rem  requirement here anyway. Helper is written once, then discarded.
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8700"
set "WANTPORT=%PORT%"
set "PICK=%TEMP%\cantest_pickport.py"

> "%PICK%" echo import socket, sys
>>"%PICK%" echo p = int(sys.argv[1])
>>"%PICK%" echo for _ in range(20):
>>"%PICK%" echo     s = socket.socket()
>>"%PICK%" echo     s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
>>"%PICK%" echo     try:
>>"%PICK%" echo         s.bind(("127.0.0.1", p)); s.close(); print(p); break
>>"%PICK%" echo     except OSError:
>>"%PICK%" echo         s.close(); p += 1
>>"%PICK%" echo else:
>>"%PICK%" echo     print(-1)

rem  NOTE: for /f refuses to capture output here, so route through a file.
"%PYEXE%" "%PICK%" %PORT% > "%PICK%.out" 2>nul
set /p PICKED=<"%PICK%.out"
del "%PICK%" "%PICK%.out" >nul 2>nul
if not "%PICKED%"=="" set "PORT=%PICKED%"

if "%PORT%"=="-1" (
  echo [ERROR] ports %WANTPORT% and the next 19 are all busy.
  echo         Pass one manually, e.g.:  start-cantest.bat 9000
  pause
  exit /b 1
)

if not "!PORT!"=="%WANTPORT%" (
  echo [3/4] port %WANTPORT% busy, using !PORT! instead
) else (
  echo [3/4] port !PORT! free
)

rem ---------- run ----------
echo [4/4] starting service ...
echo.
echo    console : http://127.0.0.1:!PORT!/ui
echo    docs    : http://127.0.0.1:!PORT!/docs
echo.
echo    stop : Ctrl+C here, or just close this window
echo    keep this window open ^- it IS the server
echo ============================================
echo.

rem  Wait ~3 s for uvicorn to bind, then open the browser.
rem  ping instead of timeout: timeout refuses to run when stdin is redirected.
start "" /min cmd /c "ping -n 4 127.0.0.1 >nul & start "" http://127.0.0.1:!PORT!/ui"

"%PYEXE%" -m cantest serve --host 127.0.0.1 --port !PORT!

echo.
echo service stopped.
pause
