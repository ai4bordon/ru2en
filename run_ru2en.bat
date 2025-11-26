@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%" >nul 2>&1

set "PYTHON_EXE="
set "VENV_PY=%SCRIPT_DIR%venv\Scripts\python.exe"

if exist "%VENV_PY%" (
    set "PYTHON_EXE=%VENV_PY%"
    echo Using virtual environment interpreter: "%PYTHON_EXE%"
) else (
    for %%P in (python.exe python3.exe) do (
        if not defined PYTHON_EXE (
            where %%P >nul 2>&1
            if not errorlevel 1 (
                set "PYTHON_EXE=%%P"
            )
        )
    )
    if not defined PYTHON_EXE (
        echo [ERROR] Python interpreter not found. Install Python or create the venv.
        set "APP_EXIT=1"
        goto :fail
    )
    echo Using system interpreter: "%PYTHON_EXE%"
)

echo Launching RU2EN...
"%PYTHON_EXE%" "%SCRIPT_DIR%ru2en.py"
set "APP_EXIT=%ERRORLEVEL%"

if %APP_EXIT% neq 0 (
    echo.
    echo [ERROR] ru2en.py exited with code %APP_EXIT%.
    goto :fail
)

:success
popd >nul 2>&1
exit /b 0

:fail
popd >nul 2>&1
pause
exit /b %APP_EXIT%
