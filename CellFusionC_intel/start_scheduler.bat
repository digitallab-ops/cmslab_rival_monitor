@echo off
cd /d "c:\loki_project\cmslab_rival\CellFusionC_intel"
REM 작업 스케줄러는 pythonw.exe로 직접 실행(창 없음). 이 bat은 수동 실행용 보조.
REM pythonw = 콘솔 창 없이 백그라운드. 로그는 runner.py가 logs\scheduler.log에 기록.
start "" "C:\Program Files\Python312\pythonw.exe" -X utf8 cli.py run
