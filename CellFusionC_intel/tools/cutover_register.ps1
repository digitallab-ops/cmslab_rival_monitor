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
    @{ n='CMSLab_Cutover_1128_tminus';   t='2026-11-28 11:25';
       e=$py;   a="-X utf8 `"$proj\tools\cutover.py`" notify --step t_minus" },
    @{ n='CMSLab_Cutover_1128_stop';     t='2026-11-28 11:30';
       e=$pwsh; a="-NoProfile -ExecutionPolicy Bypass -File `"$proj\tools\cutover_stop.ps1`"" },
    @{ n='CMSLab_Cutover_1128_restore';  t='2026-11-28 16:00';
       e=$py;   a="-X utf8 `"$proj\tools\cutover.py`" notify --step restore_check" },
    @{ n='CMSLab_Cutover_1128_restart';  t='2026-11-28 16:30';
       e=$py;   a="-X utf8 `"$proj\tools\cutover.py`" notify --step restart" },
    @{ n='CMSLab_Cutover_1128_write';    t='2026-11-28 17:10';
       e=$py;   a="-X utf8 `"$proj\tools\cutover.py`" notify --step first_write" }
)

$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' `
                 -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                 -ExecutionTimeLimit (New-TimeSpan -Hours 1)

foreach ($j in $jobs) {
    $existing = Get-ScheduledTask -TaskName $j.n -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $j.n -Confirm:$false
        Write-Output "  기존 삭제 : $($j.n)"
    }
    $trigger = New-ScheduledTaskTrigger -Once -At ([datetime]$j.t)
    $action  = New-ScheduledTaskAction -Execute $j.e -Argument $j.a -WorkingDirectory $proj
    Register-ScheduledTask -TaskName $j.n -Trigger $trigger -Action $action `
        -Principal $principal -Settings $settings | Out-Null
    Write-Output "  등록 : $($j.n)   $($j.t)"
}

Write-Output ''
Write-Output '=== 등록 결과 ==='
Get-ScheduledTask | Where-Object { $_.TaskName -like 'CMSLab_Cutover_*' } |
    ForEach-Object {
        $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
        "{0,-34} {1,-8} 다음실행 {2}" -f $_.TaskName, $_.State, $info.NextRunTime
    }
