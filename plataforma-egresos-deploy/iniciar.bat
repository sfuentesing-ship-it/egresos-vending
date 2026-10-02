@echo off
cd /d "%~dp0"
echo Iniciando plataforma de egresos en http://127.0.0.1:5000 ...
start "" http://127.0.0.1:5000
.venv\Scripts\python.exe app.py
pause
