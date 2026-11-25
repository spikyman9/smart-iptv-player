@echo off
title Build Smart IPTV Player Executable
cd /d "%~dp0"
echo ====================================================
echo   Building Smart IPTV Player (.exe) with PyInstaller
echo ====================================================
echo.

pyinstaller --noconfirm SmartIPTVPlayer.spec

echo.
echo ====================================================
echo   Build finished! Look in 'dist\SmartIPTVPlayer' for SmartIPTVPlayer.exe.
echo ====================================================
pause
