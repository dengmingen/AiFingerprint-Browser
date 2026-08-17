# 指纹浏览器工作台 · 一键启动器引擎
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File workbench.ps1 -Action start|stop|check|setup|shortcut|autostart-on|autostart-off
param(
    [ValidateSet('start', 'stop', 'check', 'setup', 'shortcut', 'autostart-on', 'autostart-off')]
    [string]$Action = 'start',
    [int]$Port = 18080
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$VenvPy = Join-Path $Root '.venv\Scripts\python.exe'
$ReqFile = Join-Path $Root 'requirements.txt'
$Icon = Join-Path $PSScriptRoot 'workbench.ico'
$ServerLog = Join-Path $Root 'server.log'
$Url = "http://127.0.0.1:$Port"

function Write-Step($m) { Write-Host "" ; Write-Host "[*] $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host " [OK] $m" -ForegroundColor Green }
function Write-Info($m) { Write-Host "  --  $m" -ForegroundColor DarkGray }
function Write-Warn2($m){ Write-Host " [!]  $m" -ForegroundColor Yellow }
function Write-Fail($m) { Write-Host " [X]  $m" -ForegroundColor Red; exit 1 }

function Test-Server {
    try {
        $r = Invoke-RestMethod "$Url/api/v1/status" -TimeoutSec 3
        return ($r.code -eq 0)
    } catch { return $false }
}

# ---------- 环境准备（首次自动配置） ----------

function Ensure-Python {
    if (Test-Path $VenvPy) { return }
    Write-Step '创建 Python 虚拟环境'
    $py = $null
    foreach ($cand in @('py', 'python')) {
        try {
            $v = & $cand --version 2>$null
            if ($LASTEXITCODE -eq 0 -and ($v -match 'Python (3\.(1[0-9]|[2-9]\d)|3\.9)')) { $py = $cand; break }
        } catch { }
    }
    if (-not $py) {
        Write-Fail '未找到 Python 3.9+。请安装 Python（勾选 Add to PATH）: https://www.python.org/downloads/'
    }
    & $py -m venv (Join-Path $Root '.venv')
    if ($LASTEXITCODE -ne 0) { Write-Fail '虚拟环境创建失败' }
    Write-Ok '虚拟环境创建完成'
}

function Ensure-Deps {
    Write-Step '检查依赖'
    & $VenvPy -c "import fastapi, uvicorn, camoufox, httpx, cryptography" 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Ok '依赖已就绪'; return }
    Write-Info '安装依赖（可能需要几分钟）…'
    & $VenvPy -m pip install --quiet -r $ReqFile
    if ($LASTEXITCODE -ne 0) {
        Write-Warn2 '默认源安装失败，改用清华镜像重试…'
        & $VenvPy -m pip install --quiet -r $ReqFile -i https://pypi.tuna.tsinghua.edu.cn/simple
        if ($LASTEXITCODE -ne 0) { Write-Fail '依赖安装失败，请检查网络' }
    }
    Write-Ok '依赖安装完成'
}

function Ensure-Kernel {
    Write-Step '检查 Camoufox 浏览器内核'
    & $VenvPy -c "import sys; sys.path.insert(0, r'$Root'); from app.kernels.camoufox_kernel import is_available; sys.exit(0 if is_available()[0] else 1)"
    if ($LASTEXITCODE -eq 0) { Write-Ok '内核已就绪'; return }
    Write-Info '下载 Camoufox 内核（约 500MB，仅首次）…'
    Push-Location $Root
    & $VenvPy -m camoufox fetch
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { Write-Warn2 '内核下载失败（可稍后重跑启动器；工作台仍可启动但 camoufox 环境不可用）' }
    else { Write-Ok '内核下载完成' }
}

function Invoke-Setup {
    Ensure-Python
    Ensure-Deps
    Ensure-Kernel
    Write-Step '首次配置完成'
}

# ---------- 服务生命周期 ----------

function Start-Server {
    if (Test-Server) { Write-Ok '工作台已在运行'; return }
    Write-Step '启动服务'
    Start-Process -FilePath $VenvPy -ArgumentList 'run.py' `
        -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput $ServerLog -RedirectStandardError (Join-Path $Root 'server.err.log')
    $ready = $false
    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep -Seconds 2
        if (Test-Server) { $ready = $true; break }
    }
    if (-not $ready) { Write-Fail "服务未在 90 秒内就绪，详见 $ServerLog" }
    Write-Ok "服务已启动: $Url"
}

function Stop-Server {
    Write-Step '停止工作台'
    $pros = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*run.py*' -and $_.CommandLine -notlike '*workbench.ps1*' }
    $mine = $pros | Where-Object {
        $_.ExecutablePath -like "$Root*" -or $_.CommandLine -like "*$Root*"
    }
    if (-not $mine) { Write-Ok '服务未在运行'; return }
    foreach ($p in $mine) {
        & taskkill /PID $p.ProcessId /T /F | Out-Null
    }
    Start-Sleep -Seconds 1
    Write-Ok '已停止（含浏览器与 Playwright 子进程）'
}

# ---------- 快捷方式 / 自启 ----------

function New-Lnk($lnkPath, $windowStyle) {
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($lnkPath)
    $sc.TargetPath = Join-Path $Root '一键启动工作台.bat'
    $sc.WorkingDirectory = $Root
    $sc.IconLocation = "$Icon,0"
    $sc.WindowStyle = $windowStyle
    $sc.Description = '指纹浏览器工作台'
    $sc.Save()
}

function New-DesktopShortcut {
    Write-Step '创建桌面快捷方式'
    if (-not (Test-Path $Icon)) { Write-Warn2 '图标缺失，快捷方式将使用默认图标' }
    $desktop = [Environment]::GetFolderPath('Desktop')
    New-Lnk (Join-Path $desktop '指纹浏览器工作台.lnk') 7
    Write-Ok "已创建: $desktop\指纹浏览器工作台.lnk"
}

function Set-Autostart([bool]$on) {
    $startup = [Environment]::GetFolderPath('Startup')
    $lnk = Join-Path $startup '指纹浏览器工作台.lnk'
    if ($on) {
        New-Lnk $lnk 7
        Write-Ok "开机自启已开启 ($startup)"
    } else {
        Remove-Item $lnk -ErrorAction SilentlyContinue
        Write-Ok '开机自启已关闭'
    }
}

# ---------- 入口 ----------

switch ($Action) {
    'check' {
        Write-Host "== 指纹浏览器工作台 环境检查 =="
        Write-Step '运行状态'
        if (Test-Server) {
            $s = Invoke-RestMethod "$Url/api/v1/status" -TimeoutSec 3
            Write-Ok "运行中 v$($s.data.version)（$($s.data.running_count) 个环境）"
        } else { Write-Info '未运行' }
        Write-Step '组件'
        Write-Info ("Python 虚拟环境: " + $(if (Test-Path $VenvPy) { '[OK]' } else { '未创建' }))
        if (Test-Path $VenvPy) {
            & $VenvPy -c "import fastapi, camoufox" 2>$null
            Write-Info ("依赖: " + $(if ($LASTEXITCODE -eq 0) { '[OK]' } else { '未安装' }))
            & $VenvPy -c "import sys; sys.path.insert(0, r'$Root'); from app.kernels.camoufox_kernel import is_available; sys.exit(0 if is_available()[0] else 1)"
            Write-Info ("Camoufox 内核: " + $(if ($LASTEXITCODE -eq 0) { '[OK]' } else { '未下载' }))
        }
        exit 0
    }
    'setup' { Invoke-Setup; New-DesktopShortcut }
    'start' {
        # 一键：缺什么补什么，然后启动并打开界面
        if (-not (Test-Path $VenvPy)) { Ensure-Python }
        & $VenvPy -c "import fastapi, uvicorn, camoufox" 2>$null
        if ($LASTEXITCODE -ne 0) { Ensure-Deps }
        & $VenvPy -c "import sys; sys.path.insert(0, r'$Root'); from app.kernels.camoufox_kernel import is_available; sys.exit(0 if is_available()[0] else 1)"
        if ($LASTEXITCODE -ne 0) { Ensure-Kernel }
        Start-Server
        Start-Process $Url
        Write-Host "`n  浏览器即将打开 $Url （未自动打开请手动访问）" -ForegroundColor Cyan
        Write-Host "  停止服务: 双击 停止工作台.bat`n" -ForegroundColor DarkGray
    }
    'stop' { Stop-Server }
    'shortcut' { New-DesktopShortcut }
    'autostart-on' { Set-Autostart $true }
    'autostart-off' { Set-Autostart $false }
}
