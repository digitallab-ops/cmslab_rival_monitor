@echo off
cd /d "c:\loki_project\cmslab_rival\CellFusionC_intel"
REM stdout/stderr는 별도 파일로 — cli.py run 내부(runner.py)가 scheduler.log를
REM FileHandler로 직접 열기 때문에 같은 파일에 리다이렉트하면 잠금 충돌 발생.
REM -X utf8: stdout/stderr를 UTF-8로 강제 (한글 로그 cp1252 인코딩 에러 방지)
"C:\Program Files\Python312\python.exe" -X utf8 cli.py run >> logs\scheduler_console.log 2>&1
