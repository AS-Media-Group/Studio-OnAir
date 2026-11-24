@echo off
setlocal
cd /d "%~dp0"
rem Builds dist\OnAirBridge.exe. Needs Python 3.12 (3.11 also fine, not 3.13+). Run on any Windows PC; the .exe it makes runs on any other.
py -3.12 -V >nul 2>nul || py -3.11 -V >nul 2>nul || (echo Install Python 3.12 from python.org first, then run this again. & pause & exit /b 1)
set PYV=3.12
py -3.12 -V >nul 2>nul || set PYV=3.11
if not exist buildenv py -%PYV% -m venv buildenv
call buildenv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller || (echo Install failed. & pause & exit /b 1)
python -m unittest || (echo Tests failed, not building. & pause & exit /b 1)
pyinstaller --noconfirm --onefile --windowed --name OnAirBridge --hidden-import mido.backends.rtmidi --hidden-import rtmidi --hidden-import serial.tools.list_ports_windows --hidden-import pystray._win32 onair_gui.py || (echo Build failed. & pause & exit /b 1)
echo.
echo Done: dist\OnAirBridge.exe  (copy that one file to the studio PC and double-click it)
pause
