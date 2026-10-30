@echo off
echo auto type.....
color 0A
cd /d "%~dp0"
py -m pip install -r requirements.txt
py -m pip install pynput pygments
py text_typer.py
pause