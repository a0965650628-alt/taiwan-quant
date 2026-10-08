# setup_scheduler.ps1
# 自動將台股量化系統註冊進 Windows 工作排程器
$ErrorActionPreference = "Stop"

$currentDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$liveBat = Join-Path $currentDir "start_market_monitor.bat"
$settleBat = Join-Path $currentDir "run_settle_1430.bat"

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " 正在設定 Windows 工作排程器 (每週一至週五自動執行)..." -ForegroundColor Cyan
Write-Host " 專案路徑: $currentDir" -ForegroundColor Gray
Write-Host "========================================================" -ForegroundColor Cyan

# 任務 1: 08:50 開盤前自動啟動即時監控
$action1 = New-ScheduledTaskAction -Execute $liveBat -WorkingDirectory $currentDir
$trigger1 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "08:50"
Register-ScheduledTask -TaskName "台股量化_0850開盤監控" -Action $action1 -Trigger $trigger1 -Description "台股量化交易系統 - 盤中即時監控" -Force
Write-Host "✅ 任務 1 設定成功：每週一至週五 08:50 自動啟動盤中監控" -ForegroundColor Green

# 任務 2: 14:30 自動發送盤後三大法人交易金額與策略結算
$action2 = New-ScheduledTaskAction -Execute $settleBat -WorkingDirectory $currentDir
$trigger2 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "14:30"
Register-ScheduledTask -TaskName "台股量化_1430法人籌碼結算" -Action $action2 -Trigger $trigger2 -Description "台股量化交易系統 - 14:30 盤後三大法人交易金額與策略日報" -Force
Write-Host "✅ 任務 2 設定成功：每週一至週五 14:30 自動發送法人籌碼日報" -ForegroundColor Green

Write-Host "`n全部排程已登錄完成！您可以在 Windows『工作排程器』中隨時檢視或管理。" -ForegroundColor Yellow
