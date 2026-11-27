# 컷오버 당일 알림·작업을 Windows 작업 스케줄러에 1회성으로 등록한다.
#
# 왜 APScheduler 가 아니라 여기냐: 컷오버 당일엔 APScheduler 를 멈춘다.
# 자기를 멈춘 뒤의 알림을 자기가 보낼 수는 없다.
#
# 실행 계정은 기존 스케줄러와 동일하게 SYSTEM — 로그인 상태와 무관하게 뜬다.

$ErrorActionPreference = 'Stop'
$proj = 'c:\loki_project\cmslab_rival\CellFusionC_intel'
$py   = 'C:\Program Files\Python312\python.exe'
$pwsh = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

# 이름, 시각(현지), 실행파일, 인자
$jobs = @(
    @{ n='CMSLab_Cutover_1125_fullscan'; t='2026-11-25 19:30';
       e=$py;   a="-X utf8 `"$proj\tools\cutover.py`" notify --step fullscan_defer" },
    @{ n='CMSLab_Cutover_1206_movepause'; t='2026-12-06 20:00';
       e=$py;   a="-X utf8 `"$proj\tools\cutover.py`" notify --step move_pause" },
    @{ n='CMSLab_Cutover_1128_tminus';   t='2026-11-28 11:25';
       e=$py;   a="-X utf8 `"$proj\tools\cutover.py`" notify --step t_minus" },
    @{ n='CMSLab_Cutover_1128_stop';     t='2026-11-28 11:30';
       e=$pwsh; a="-NoProfile -ExecutionPolicy Bypass -File `"$proj\tools\cutover_stop.ps1`"" },
    # 11:50 정지 보고 하나로는 놓칠 수 있다. 놓치면 위시켓이 말없이 기다린다.
    @{ n='CMSLab_Cutover_1128_dumpsig';  t='2026-11-28 12:05';
       e=$py;   a="-X utf8 `"$proj\tools\cutover.py`" notify --step dump_signal" },
    # 복원이 언제 끝나는지는 위시켓만 안다(예상 12:00~16:30). 사람이 그걸 보고
    # 있을 수 없고 토요일이다. 그래서 완료를 코드가 알아보게 한다 —
    # 11:30 기준값과 **완전히 일치**하는 순간이 완료다(휴리스틱 아님).
    # 🔴 h=8 : 기본 1시간 제한이면 폴링 도중에 작업이 죽는다.
    @{ n='CMSLab_Cutover_1128_autogo';   t='2026-11-28 12:10'; h=8;
       e=$py;   a="-X utf8 `"$proj\tools\cutover_go.py`" --until 19:00" },
    @{ n='CMSLab_Cutover_1128_restore';  t='2026-11-28 16:00';
       e=$py;   a="-X utf8 `"$proj\tools\cutover.py`" notify --step restore_check" },
    @{ n='CMSLab_Cutover_1128_restart';  t='2026-11-28 16:30';
       e=$py;   a="-X utf8 `"$proj\tools\cutover.py`" notify --step restart" },
    @{ n='CMSLab_Cutover_1128_write';    t='2026-11-28 17:10';
       e=$py;   a="-X utf8 `"$proj\tools\cutover.py`" notify --step first_write" },
    # 11/28 알림은 전부 그날 하루짜리다. 전환을 11/29 로 미루면 **아무것도 안 울린다**.
    # 그런데 cutover_stop.ps1 이 작업을 *비활성화* 해놔서 증상이 에러가 아니라 침묵이고,
    # 파수꾼도 스케줄러 안에서 도니 같이 꺼져 있어 자기가 꺼진 걸 못 알린다.
    # → 밖에서 이틀간 확인한다. 이미 살아났으면 조용히 끝난다.
    @{ n='CMSLab_Cutover_1129_watch';    t='2026-11-29 09:00';
       e=$pwsh; a="-NoProfile -ExecutionPolicy Bypass -File `"$proj\tools\cutover_watch.ps1`"" },
    @{ n='CMSLab_Cutover_1130_watch';    t='2026-11-30 09:00';
       e=$pwsh; a="-NoProfile -ExecutionPolicy Bypass -File `"$proj\tools\cutover_watch.ps1`"" }
)

$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' `
                 -LogonType ServiceAccount -RunLevel Highest
# 작업마다 실행 시간 제한이 다르다. 알림은 1시간이면 충분하지만,
# 복원을 기다리는 autogo 는 7시간 가까이 돌 수 있다 — 1시간이면 중간에 잘린다.
function New-Settings($hours) {
    New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours $hours)
}

foreach ($j in $jobs) {
    $existing = Get-ScheduledTask -TaskName $j.n -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $j.n -Confirm:$false
        Write-Output "  기존 삭제 : $($j.n)"
    }
    $trigger = New-ScheduledTaskTrigger -Once -At ([datetime]$j.t)
    $action  = New-ScheduledTaskAction -Execute $j.e -Argument $j.a -WorkingDirectory $proj
    $hours = if ($j.ContainsKey('h')) { $j.h } else { 1 }
    Register-ScheduledTask -TaskName $j.n -Trigger $trigger -Action $action `
        -Principal $principal -Settings (New-Settings $hours) | Out-Null
    Write-Output "  등록 : $($j.n)   $($j.t)   (제한 ${hours}h)"
}

Write-Output ''
Write-Output '=== 등록 결과 ==='
Get-ScheduledTask | Where-Object { $_.TaskName -like 'CMSLab_Cutover_*' } |
    ForEach-Object {
        $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
        "{0,-34} {1,-8} 다음실행 {2}" -f $_.TaskName, $_.State, $info.NextRunTime
    }
