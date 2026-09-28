@echo off
REM Double-click to start the YouTube Trending Analyzer.
REM First run creates a virtual environment in .venv and installs the requirements.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.10+ from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv || goto :error
)

call ".venv\Scripts\activate.bat"

REM Reinstall only when requirements.txt changed.
fc /b requirements.txt ".venv\installed.txt" >nul 2>nul
if errorlevel 1 (
    echo Installing requirements...
    python -m pip install --upgrade pip >nul
    python -m pip install -r requirements.txt || goto :error
    copy /y requirements.txt ".venv\installed.txt" >nul
)

REM Optional: put your keys in a file called keys.bat next to this one, e.g.
REM   set YOUTUBE_API_KEY=AIza...
REM   set ANTHROPIC_API_KEY=sk-ant-...
if exist keys.bat call keys.bat

REM Skip Streamlit's one-time "enter your email" prompt.
if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
    mkdir "%USERPROFILE%\.streamlit" 2>nul
    > "%USERPROFILE%\.streamlit\credentials.toml" echo [general]
    >> "%USERPROFILE%\.streamlit\credentials.toml" echo email = ""
)

python -m streamlit run app.py
goto :eof

:error
echo.
echo Something went wrong - see the messages above.
pause
exit /b 1
