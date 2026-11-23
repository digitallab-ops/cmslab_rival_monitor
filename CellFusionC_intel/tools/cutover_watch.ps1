# 컷오버 뒤 "아직 안 살아났으면" 확인 — 11/29·11/30 09:00 에 바깥에서 돈다.
#
# 왜 필요한가:
#   11/28 알림은 전부 그날 하루짜리다. 전환을 11/29 로 미루면 아무것도 안 울린다.
#   그런데 cutover_stop.ps1 이 작업을 *비활성화* 해놓기 때문에 증상이 에러가 아니라
#   **침묵**이다. 파수꾼(watchdog)은 스케줄러 안에서 도니 같이 꺼져 있어서
#   자기가 꺼진 걸 자기가 알릴 수 없다. 그래서 밖에서 본다.
#
# 이미 살아났으면 슬랙을 울리지 않고 조용히 끝낸다.

$ErrorActionPreference = 'Stop'
$proj  = 'c:\loki_project\cmslab_rival\CellFusionC_intel'
$py    = 'C:\Program Files\Python312\python.exe'
$tasks = @('CMSLab_RivalScheduler', 'CmsLab_RivalMonitor')
$log   = Join-Path $proj 'logs\cutover\watch.log'

New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
function Say($m) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m"
    Write-Output $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

Set-Location $proj
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$down = @()
foreach ($t in $tasks) {
    $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    if (-not $task) {
        # 작업 자체가 없으면 꺼진 것보다 나쁘다 — 알린다.
        Say "작업 없음 : $t"
        $down += $t
    } elseif ($task.State -eq 'Disabled') {
        Say "비활성화 : $t"
        $down += $t
    } else {
        Say "정상 : $t ($($task.State))"
    }
}

if ($down.Count -eq 0) {
    Say '수집이 살아 있습니다 — 알림 없이 종료'
    exit 0
}

Say ("아직 멈춰 있음 : " + ($down -join ', ') + " — 슬랙 알림")
& $py -X utf8 (Join-Path $proj 'tools\cutover.py') notify --step still_down 2>&1 |
    ForEach-Object { Say "  $_" }
