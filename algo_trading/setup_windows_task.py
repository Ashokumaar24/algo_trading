"""
setup_windows_task.py
=====================
Run this ONCE to create the Windows Task Scheduler entry.
After that, your algo trading system will auto-start every
weekday at 8:55 AM — no manual action needed.

Run:  python setup_windows_task.py
"""

import os
import sys
import subprocess


def find_python_exe():
    return sys.executable


def create_task():
    project_root = os.path.dirname(os.path.abspath(__file__))
    python_exe   = find_python_exe()
    script_path  = os.path.join(project_root, "auto_start.py")

    os.makedirs(os.path.join(project_root, "logs"), exist_ok=True)

    print("=" * 60)
    print("  WINDOWS TASK SCHEDULER SETUP")
    print("=" * 60)
    print(f"\n  Project:    {project_root}")
    print(f"  Python:     {python_exe}")
    print(f"  Script:     {script_path}")
    print(f"  Schedule:   8:55 AM, Monday–Friday\n")

    if not os.path.exists(script_path):
        print(f"  ERROR: auto_start.py not found at {script_path}")
        print("  Make sure you run this from your algo_trading folder.")
        input("\nPress Enter to exit...")
        return False

    # XML task definition — most reliable method for Task Scheduler
    task_xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>AI Algo Trading System — Auto-start every weekday at 8:55 AM</Description>
    <Author>{os.environ.get('USERNAME', 'User')}</Author>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T08:55:00</StartBoundary>
      <Enabled>true</Enabled>
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
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT8H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{python_exe}</Command>
      <Arguments>"{script_path}"</Arguments>
      <WorkingDirectory>{project_root}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""

    xml_path = os.path.join(project_root, "logs", "task_temp.xml")
    with open(xml_path, 'w', encoding='utf-16') as f:
        f.write(task_xml)

    task_name = "AlgoTrading_AutoStart"

    # Delete old task if it exists
    subprocess.run(
        ["schtasks", "/Delete", "/TN", task_name, "/F"],
        capture_output=True
    )

    # Create new task from XML
    result = subprocess.run(
        ["schtasks", "/Create", "/TN", task_name, "/XML", xml_path, "/F"],
        capture_output=True, text=True
    )

    try:
        os.remove(xml_path)
    except Exception:
        pass

    if result.returncode == 0:
        print("  ✅ Task created successfully!\n")
        print(f"  Task name:  {task_name}")
        print(f"  Runs at:    8:55 AM every Monday–Friday")
        print(f"  Starts on:  Next weekday automatically\n")

        verify = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name, "/FO", "LIST"],
            capture_output=True, text=True
        )
        if verify.returncode == 0:
            for line in verify.stdout.splitlines():
                if any(k in line for k in ["Task Name", "Status", "Next Run", "Last Run"]):
                    print(f"  {line.strip()}")
        return True

    else:
        print(f"  ❌ Task creation failed.")
        print(f"  Error: {result.stderr.strip()}")
        print(f"\n  Try running as Administrator:")
        print(f"  Right-click Command Prompt → 'Run as administrator'")
        print(f"  Then run: python setup_windows_task.py")
        return False


def test_task():
    task_name = "AlgoTrading_AutoStart"
    print("\n  Running task now to test...")
    result = subprocess.run(
        ["schtasks", "/Run", "/TN", task_name],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print("  ✅ Task started — check Telegram for startup message!")
    else:
        print(f"  ⚠️  Could not start task: {result.stderr.strip()}")


def delete_task():
    task_name = "AlgoTrading_AutoStart"
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", task_name, "/F"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"  ✅ Task '{task_name}' deleted.")
    else:
        print(f"  Task not found or already deleted.")


def show_menu():
    print("\n" + "=" * 60)
    print("  ALGO TRADING — WINDOWS TASK SCHEDULER")
    print("=" * 60)
    print("\n  What do you want to do?\n")
    print("  1. Create / Update the scheduled task")
    print("  2. Test — run it right now")
    print("  3. Delete the scheduled task")
    print("  4. Exit")
    return input("\n  Enter choice (1/2/3/4): ").strip()


if __name__ == "__main__":
    while True:
        choice = show_menu()

        if choice == "1":
            success = create_task()
            if success:
                test_now = input("\n  Test it now? (y/n): ").strip().lower()
                if test_now == "y":
                    test_task()
            input("\nPress Enter to continue...")

        elif choice == "2":
            test_task()
            input("\nPress Enter to continue...")

        elif choice == "3":
            confirm = input("\n  Delete the task? (y/n): ").strip().lower()
            if confirm == "y":
                delete_task()
            input("\nPress Enter to continue...")

        elif choice == "4":
            print("\n  Done. Task is scheduled — see you at 8:55 AM! 🚀\n")
            break

        else:
            print("  Invalid choice, try again.")
