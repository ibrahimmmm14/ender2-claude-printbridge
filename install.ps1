# PrintBridge installer for Windows
# - installs Python (if missing), creates a virtual environment, installs dependencies
# - writes %USERPROFILE%\PrintBridge\config.json (printer address)
# - registers the "printbridge" MCP server in Claude Desktop
# Run:  right-click install.ps1 -> "Run with PowerShell"
#   or: powershell -ExecutionPolicy Bypass -File install.ps1

param([string]$PrinterUrl = "", [string]$Password = "", [switch]$Unattended)
$ErrorActionPreference = "Stop"
$Src  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Home_ = Join-Path $env:USERPROFILE "PrintBridge"
$App  = Join-Path $Home_ "app"
$Venv = Join-Path $Home_ "venv"
$Cfg  = Join-Path $Home_ "config.json"

Write-Host "== PrintBridge setup ==" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $Home_, $App | Out-Null

# 1) Python
function Split-Cmd($c) {
    $p = $c.Split(" ")
    $a = @()
    if ($p.Length -gt 1) { $a = $p[1..($p.Length-1)] }
    return @{ exe = $p[0]; args = $a }
}
function Write-Utf8NoBom($path, $text) {
    [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding $false))
}
function Find-Python {
    foreach ($c in @("py -3.12", "py -3.11", "py -3.10", "python")) {
        try {
            $sc = Split-Cmd $c
            $v = & $sc.exe @($sc.args) -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v -and [version]$v -ge [version]"3.10") { return $c }
        } catch {}
    }
    return $null
}
$py = Find-Python
if (-not $py) {
    Write-Host "Installing Python 3.12 (winget)..." -ForegroundColor Yellow
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    $py = Find-Python
    if (-not $py) { throw "Python 3.10+ not found. Install it from python.org and run this again." }
}
Write-Host "Python: $py"

# 2) Copy app + venv
Copy-Item -Recurse -Force (Join-Path $Src "printbridge") $App
Copy-Item -Force (Join-Path $Src "requirements.txt") $App
$sc = Split-Cmd $py
& $sc.exe @($sc.args) -m venv $Venv
$VPy = Join-Path $Venv "Scripts\python.exe"
& $VPy -m pip install --upgrade pip -q
& $VPy -m pip install -r (Join-Path $App "requirements.txt") -q
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# 3) Config
if (Test-Path $Cfg) { $conf = Get-Content $Cfg -Raw | ConvertFrom-Json } else { $conf = [pscustomobject]@{} }
$current = if ($conf.printer_url) { $conf.printer_url } else { "http://esp3d.local" }
$ans = if ($PrinterUrl) { $PrinterUrl } elseif ($Unattended) { "" } else { Read-Host "Printer (ESP3D) address - LAN IP or Tailscale 100.x.y.z [$current]" }
if ($ans) { if ($ans -notmatch "^https?://") { $ans = "http://$ans" }; $current = $ans }
$pw = if ($Password -or $Unattended) { $Password } else { Read-Host "ESP3D admin password (leave empty if none)" }
$conf | Add-Member -Force NoteProperty printer_url $current
$conf | Add-Member -Force NoteProperty printer_ws_port 8282
$conf | Add-Member -Force NoteProperty esp3d_user "admin"
$conf | Add-Member -Force NoteProperty esp3d_password $pw
$conf | Add-Member -Force NoteProperty workdir $Home_
foreach ($k in @("cura_install", "cura_config", "cura_machine")) { if (-not $conf.$k) { $conf | Add-Member -Force NoteProperty $k "" } }
Write-Utf8NoBom $Cfg ($conf | ConvertTo-Json -Depth 5)

# 4) Check Cura profile + printer
$env:PYTHONPATH = $App
Write-Host "`nCura profile that will be used:" -ForegroundColor Cyan
& $VPy -m printbridge.cli profile
Write-Host "`nPrinter check:" -ForegroundColor Cyan
& $VPy -m printbridge.cli status

# 5) Register in Claude Desktop (normal + Microsoft Store installs)
$targets = @(Join-Path $env:APPDATA "Claude\claude_desktop_config.json")
Get-ChildItem (Join-Path $env:LOCALAPPDATA "Packages") -Directory -Filter "Claude_*" -ErrorAction SilentlyContinue | ForEach-Object {
    $targets += Join-Path $_.FullName "LocalCache\Roaming\Claude\claude_desktop_config.json"
}
foreach ($t in $targets) {
    $dir = Split-Path -Parent $t
    if (-not (Test-Path $dir)) { if ($t -like "*Packages*") { continue } ; New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    if (Test-Path $t) {
        Copy-Item $t "$t.bak" -Force
        $json = Get-Content $t -Raw | ConvertFrom-Json
    } else { $json = [pscustomobject]@{} }
    if (-not $json.mcpServers) { $json | Add-Member -Force NoteProperty mcpServers ([pscustomobject]@{}) }
    $server = [pscustomobject]@{
        command = $VPy
        args    = @("-m", "printbridge.server")
        env     = [pscustomobject]@{ PYTHONPATH = $App; PRINTBRIDGE_CONFIG = $Cfg }
    }
    $json.mcpServers | Add-Member -Force NoteProperty printbridge $server
    Write-Utf8NoBom $t ($json | ConvertTo-Json -Depth 20)
    Write-Host "Registered in $t"
}

Write-Host "`nDone. Fully quit Claude Desktop (tray icon -> Quit) and open it again." -ForegroundColor Green
Write-Host "Command line:  $VPy -m printbridge.cli status   (with PYTHONPATH=$App)"
if (-not $Unattended) { Read-Host "Press Enter to close" }
