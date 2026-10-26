@echo off
REM Launched by Windows Task Scheduler (Mon-Fri 09:05). Runs today's session and exits after 15:40.
cd /d "%~dp0"
python run_agent.py %*
