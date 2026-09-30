@echo off
rem ============================================================
rem  Webbdammsugare Pro - startskript (Windows)
rem
rem  Dubbelklicka for att starta GUI:t.
rem  Forsta gangen skapas en egen Python-miljo (.venv) och alla
rem  beroenden installeras - det tar nagra minuter.
rem
rem  Andra sätt att starta:
rem    starta.bat test                  kor testerna
rem    starta.bat --config sites.json   serverlage (utan GUI)
rem ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

rem --- Hitta Python 3.11+ ---
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [FEL] Python hittades inte. Installera Python 3.11 eller nyare fran https://www.python.org/downloads/
    echo       och kryssa i "Add python.exe to PATH" under installationen.
    goto :fail
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [FEL] Python 3.11 eller nyare kravs. Din version:
    %PY% --version
    goto :fail
)

rem --- Skapa virtuell miljo vid forsta korningen ---
if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Skapar Python-miljo i .venv ...
    %PY% -m venv .venv
    if errorlevel 1 goto :fail
)
set "VPY=.venv\Scripts\python.exe"

rem --- Installera beroenden om requirements.txt andrats sedan sist ---
set "STAMP=.venv\requirements.installed"
fc /b requirements.txt "%STAMP%" >nul 2>nul
if errorlevel 1 (
    echo [2/3] Installerar beroenden ^(bara forsta gangen eller nar requirements.txt andrats^) ...
    "%VPY%" -m pip install --upgrade pip >nul
    "%VPY%" -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
    echo [3/3] Installerar webblasaren for JavaScript-sidor ^(Playwright Chromium, ca 150 MB^) ...
    "%VPY%" -m playwright install chromium
    if errorlevel 1 (
        echo [VARNING] Kunde inte installera Chromium. Crawlern fungerar, men sidor som kraver JavaScript hamtas inte.
    )
    copy /y requirements.txt "%STAMP%" >nul
)

rem --- Starta ---
if /i "%~1"=="test" (
    "%VPY%" -m pip install pytest >nul
    "%VPY%" -m pytest tests -q
    goto :done
)
if "%~1"=="" (
    "%VPY%" ultimate-web-crawler.py
) else (
    "%VPY%" ultimate-web-crawler.py %*
)
goto :done

:fail
echo.
echo Nagot gick fel - se meddelandet ovan.
pause
exit /b 1

:done
if errorlevel 1 (
    echo.
    echo Programmet avslutades med fel ^(kod %errorlevel%^).
    pause
)
endlocal
