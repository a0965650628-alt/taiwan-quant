"""
setup_scheduler.py
自動設定 Windows 工作排程器 (採用 XML 導入模式，支援中文路徑、桌面彈出視窗與筆電電池執行)
"""
import os
import subprocess
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

def build_task_xml(task_name: str, description: str, bat_name: str, start_time: str) -> str:
    proj_dir = os.path.abspath(".")
    bat_path = os.path.join(proj_dir, bat_name)
    
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Date>2026-10-08T08:50:00</Date>
    <Author>TaiwanQuant</Author>
    <Description>{description}</Description>
    <URI>\\{task_name}</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>4</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-10-08T{start_time}:00</StartBoundary>
      <ScheduleByWeek>
        <WeeksInterval>1</WeeksInterval>
        <DaysOfWeek>
          <Monday />
          <Tuesday />
          <Wednesday />
          <Thursday />
          <Friday />
        </DaysOfWeek>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>/c "{bat_name}"</Arguments>
      <WorkingDirectory>{proj_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""
    return xml

def setup():
    proj_dir = os.path.abspath(".")
    task1_name = "TaiwanQuant_Live_0850"
    task2_name = "TaiwanQuant_Settle_1430"

    print("========================================================")
    print(" 正在為您設定 Windows 工作排程器 (XML 模式，支援中文路徑與桌面視窗)...")
    print(f" 專案路徑: {proj_dir}")
    print("========================================================")

    # 任務 1: 08:50 盤中監控
    xml1 = build_task_xml(task1_name, "台股量化交易系統 - 盤中即時高頻監控", "start_market_monitor.bat", "08:50")
    xml1_file = os.path.join(proj_dir, "task_live.xml")
    with open(xml1_file, "w", encoding="utf-16") as f:
        f.write(xml1)
    
    res1 = subprocess.run(["schtasks", "/create", "/tn", task1_name, "/xml", xml1_file, "/f"], capture_output=True)
    if res1.returncode == 0:
        print(f"✅ 任務 1 設定成功：每週一至週五 08:50 自動啟動盤中監控視窗 ({task1_name})")
    else:
        print(f"⚠️ 任務 1 建立提示: {res1.stderr.decode('cp950', errors='ignore') or res1.stdout.decode('cp950', errors='ignore')}")

    # 任務 2: 14:30 盤後結算
    xml2 = build_task_xml(task2_name, "台股量化交易系統 - 14:30 盤後三大法人籌碼結算", "run_settle_1430.bat", "14:30")
    xml2_file = os.path.join(proj_dir, "task_settle.xml")
    with open(xml2_file, "w", encoding="utf-16") as f:
        f.write(xml2)
    
    res2 = subprocess.run(["schtasks", "/create", "/tn", task2_name, "/xml", xml2_file, "/f"], capture_output=True)
    if res2.returncode == 0:
        print(f"✅ 任務 2 設定成功：每週一至週五 14:30 自動發送法人籌碼日報 ({task2_name})")
    else:
        print(f"⚠️ 任務 2 建立提示: {res2.stderr.decode('cp950', errors='ignore') or res2.stdout.decode('cp950', errors='ignore')}")

    print("\n排程登錄作業已完成！")

if __name__ == "__main__":
    setup()
