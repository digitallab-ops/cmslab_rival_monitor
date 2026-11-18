# 컷오버 — 수집 재가동 (cutover_stop.ps1 의 짝)
#
# 정지 때 비활성화한 작업 두 개를 되살리고 스케줄러를 띄운다.
# .env 교체와 검증이 끝난 뒤에 부른다.

$ErrorActionPreference = 'Stop'
$proj  = 'c:\loki_project\cmslab_rival\CellFusionC_intel'
$tasks = @('CMSLab_RivalScheduler', 'CmsLab_RivalMonitor')
$log   = Join-Path $proj 'logs\cutover\stop.log'

New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
function Say($m) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m"
    Write-Output $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

Set-Location $proj
Say '=== 컷오버 재가동 시작 ==='

foreach ($t in $tasks) {
    try {
        Enable-ScheduledTask -TaskName $t -ErrorAction Stop | Out-Null
        Say "작업 재활성화 : $t"
    } catch {
        Say "작업 재활성화 실패 : $t — $($_.Exception.Message)"
    }
}

Start-ScheduledTask -TaskName 'CMSLab_RivalScheduler'
Start-Sleep -Seconds 20

$p = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
     Where-Object { $_.CommandLine -like '*cli.py run*' }
if ($p) {
    Say "스케줄러 기동 확인 : PID $($p.ProcessId)"
} else {
    Say '🔴 스케줄러가 안 올라왔습니다 — logs\scheduler.log 확인 필요'
}

Say '=== 컷오버 재가동 완료 ==='
