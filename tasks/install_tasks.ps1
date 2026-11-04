# 安装 Windows 计划任务：开盘记录 + 收盘复盘
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\tasks\install_tasks.ps1
#   powershell -ExecutionPolicy Bypass -File .\tasks\install_tasks.ps1 -NoLoggedOff   # 强制只用"登录时运行"
#   powershell -ExecutionPolicy Bypass -File .\tasks\install_tasks.ps1 -Uninstall
#   powershell -ExecutionPolicy Bypass -File .\tasks\install_tasks.ps1 -Python "C:\...\pythonw.exe" -Root "D:\..."
#
# 说明：默认值全部自动探测（脚本所在仓库根 + 本机 WorkBuddy 自带 Python），
#       换机器/换目录后直接重跑本脚本即可，不需要改参数。

param(
    [string]$Python = "",
    [string]$Root = "",
    [switch]$NoLoggedOff,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$openName = "Astocks-Open"
$closeName = "Astocks-Close"

if ($Uninstall) {
    foreach ($n in @($openName, $closeName)) {
        $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
        if ($t) { Unregister-ScheduledTask -TaskName $n -Confirm:$false; Write-Host "已删除任务 $n" }
    }
    exit 0
}

# ---------------------------------------------------------------- 自动探测根目录
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$Root = (Resolve-Path $Root).Path

# ---------------------------------------------------------------- 自动探测 Python
if (-not $Python) {
    $candidates = @()
    # 1) WorkBuddy 自带解释器（按版本号倒序取最新）
    $vroot = Join-Path $env:USERPROFILE ".workbuddy\binaries\python\versions"
    if (Test-Path $vroot) {
        Get-ChildItem $vroot -Directory |
            Where-Object { $_.Name -match '^\d+\.\d+\.\d+$' } |
            Sort-Object { [version]$_.Name } -Descending |
            ForEach-Object {
                $p = Join-Path $_.FullName "pythonw.exe"
                if (Test-Path $p) { $candidates += $p }
            }
    }
    # 2) 系统 PATH 里的 pythonw / python
    foreach ($n in @("pythonw.exe", "python.exe")) {
        $cmd = Get-Command $n -ErrorAction SilentlyContinue
        if ($cmd) { $candidates += $cmd.Source }
    }
    # 3) 常见安装位置
    foreach ($p in @("$env:LOCALAPPDATA\Programs\Python\Python313\pythonw.exe",
                     "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
                     "C:\Python313\pythonw.exe")) {
        if (Test-Path $p) { $candidates += $p }
    }
    if ($candidates.Count -eq 0) { throw "未找到 Python，请用 -Python 指定 pythonw.exe 路径" }
    $Python = $candidates[0]
}
$Python = (Resolve-Path $Python).Path

if (-not (Test-Path (Join-Path $Root "run.py"))) { throw "找不到 run.py：$Root" }

Write-Host "Python : $Python"
Write-Host "Root   : $Root"
Write-Host ""

$days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

function New-AstocksTask {
    param([string]$Name, [string]$Mode, [string]$At, [string]$Desc)

    $action = New-ScheduledTaskAction -Execute $Python `
        -Argument ('"{0}\run.py" {1}' -f $Root, $Mode) -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $At
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)

    # 首选 S4U：未登录/锁屏也能跑（无需保存密码）。权限不足时退回"仅登录时运行"。
    if (-not $NoLoggedOff) {
        try {
            $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                -LogonType S4U -RunLevel Limited
            Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
                -Settings $settings -Principal $principal -Description $Desc -Force | Out-Null
            Write-Host ("已注册 {0} -> 工作日 {1}  [{2}]  运行条件：无人值守" -f $Name, $At, $Mode)
            return
        } catch {
            Write-Host ("S4U 注册失败（{0}），退回仅登录时运行" -f $_.Exception.Message)
        }
    }

    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Settings $settings -Description $Desc -Force | Out-Null
    Write-Host ("已注册 {0} -> 工作日 {1}  [{2}]  运行条件：用户已登录" -f $Name, $At, $Mode)
}

New-AstocksTask -Name $openName  -Mode "open"  -At "09:31" -Desc "磐石选股：开盘记录推荐股并入库"
New-AstocksTask -Name $closeName -Mode "close" -At "15:05" -Desc "磐石选股：收盘复盘推荐股表现并更新策略统计"

Write-Host ""
Write-Host "查看状态：Get-ScheduledTask -TaskName Astocks-Open,Astocks-Close | Format-Table TaskName,State"
Write-Host "下次时间：Get-ScheduledTaskInfo -TaskName Astocks-Open"
Write-Host "手动跑：  & `"$Python`" `"$Root\run.py`" open"
Write-Host "说明：任务按周一至周五触发，脚本内部再用行情时间戳判定交易日，法定节假日自动跳过。"
Write-Host "      收盘任务会把所有未满 5 个交易日的历史推荐一并补齐（漏跑可自愈）。"
