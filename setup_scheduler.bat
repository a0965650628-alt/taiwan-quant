@echo off
chcp 65001 >nul
echo 正在為您設定 Windows 工作排程器 (每週一至週五自動執行)...
echo.

set "PROJ_DIR=%~dp0"
set "LIVE_BAT=%PROJ_DIR%start_market_monitor.bat"
set "SETTLE_BAT=%PROJ_DIR%run_settle_1430.bat"

:: 任務 1: 08:50 自動啟動盤中即時監控
schtasks /create /tn "台股量化_0850開盤監控" /tr "\"%LIVE_BAT%\"" /sc weekly /d MON,TUE,WED,THU,FRI /st 08:50 /f

:: 任務 2: 14:30 自動執行盤後法人籌碼與策略結算
schtasks /create /tn "台股量化_1430法人籌碼結算" /tr "\"%SETTLE_BAT%\"" /sc weekly /d MON,TUE,WED,THU,FRI /st 14:30 /f

echo.
echo ========================================================
echo   ✅ Windows 工作排程器已成功登錄！
echo   1. 每天 08:50 自動啟動盤中監控 (台股量化_0850開盤監控)
echo   2. 每天 14:30 自動發送法人籌碼日報 (台股量化_1430法人籌碼結算)
echo ========================================================
pause
