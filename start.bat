@echo off
python "%~dp0periph_eyes.py"
if errorlevel 1 (
    echo.
    echo Python не найден или произошла ошибка.
    echo Установи Python 3.8+ c python.org
    pause
)
