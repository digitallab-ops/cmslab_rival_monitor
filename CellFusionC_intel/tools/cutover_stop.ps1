# 컷오버 — 수집 정지 + 대사 기준값 저장 + 슬랙 보고
#
# 🔴 프로세스만 죽이면 안 된다.
#    CMSLab_RivalScheduler 는 10분마다 반복 트리거(PT10M, 무기한)가 있고
#    실패 시 5회 재시작(PT2M)도 걸려 있다. 작업을 먼저 '비활성화' 해야
#    되살아나지 않는다. CmsLab_RivalMonitor 는 부팅 트리거라 컷오버 중
#    재부팅이 나면 조용히 수집을 되살린다 — 그래서 둘 다 내린다.

$ErrorActionPreference = 'Stop'
$proj  = 'c:\loki_project\cmslab_rival\CellFusionC_intel'
$py    = 'C:\Program Files\Python312\python.exe'
$tasks = @('CMSLab_RivalScheduler', 'CmsLab_RivalMonitor')
$log   = Join-Path $proj 'logs\cutover\stop.log'

New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
function Say($m) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m"
    Write-Output $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

Set-Location $proj
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

Say '=== 컷오버 정지 시작 ==='

# ① 작업 비활성화 — 프로세스를 죽이기 전에 해야 한다
foreach ($t in $tasks) {
    try {
        Disable-ScheduledTask -TaskName $t -ErrorAction Stop | Out-Null
        Say "작업 비활성화 : $t"
    } catch {
        Say "작업 비활성화 실패 : $t — $($_.Exception.Message)"
    }
}

# ② 돌고 있는 잡이 끝나기를 기다린다 (최대 20분)
#    로그 마지막 줄이 2분 안에 갱신되면 아직 일하는 중으로 본다.
$sched = Join-Path $proj 'logs\scheduler.log'
$deadline = (Get-Date).AddMinutes(20)
while ((Get-Date) -lt $deadline) {
    if (-not (Test-Path $sched)) { break }
    $age = (Get-Date) - (Get-Item $sched).LastWriteTime
    if ($age.TotalMinutes -ge 2) { break }
    Say ("진행 중인 잡이 있는 듯 — 대기 (로그 갱신 {0:N0}초 전)" -f $age.TotalSeconds)
    Start-Sleep -Seconds 60
}

# ③ 프로세스 종료
$procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
         Where-Object { $_.CommandLine -like '*cli.py run*' }
if ($procs) {
    foreach ($p in $procs) {
        Say "프로세스 종료 : PID $($p.ProcessId)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 5
} else {
    Say '실행 중인 스케줄러 프로세스가 없습니다'
}

$left = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*cli.py run*' }
if ($left) {
    Say '🔴 아직 살아 있는 프로세스가 있습니다 — 수동 확인 필요'
} else {
    Say '정지 확인 완료'
}

# ④ 대사 기준값 — 수집이 멈춘 뒤에 떠야 값이 고정된다
Say '대사 기준값 저장 중...'
& $py -X utf8 (Join-Path $proj 'tools\cutover.py') snapshot 2>&1 | ForEach-Object { Say "  $_" }

# ⑤ 권한·소유자 기록 — Supabase 쓰기 권한을 회수하기 **전에** 떠야 한다.
#    무엇을 회수했는지 모르면 롤백할 때 복구가 추측이 된다.
Say '권한·소유자 기록 중...'
& $py -X utf8 (Join-Path $proj 'tools\cutover.py') grants 2>&1 | ForEach-Object { Say "  $_" }

# ⑥ 슬랙 보고
& $py -X utf8 (Join-Path $proj 'tools\cutover.py') notify --step stopped 2>&1 | ForEach-Object { Say "  $_" }

Say '=== 컷오버 정지 완료 ==='
