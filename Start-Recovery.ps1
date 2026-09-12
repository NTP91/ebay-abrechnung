param([int]$Port = 8511)
$ErrorActionPreference = 'Stop'
$repoPath = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$branch = git -C $repoPath branch --show-current
if ($branch -ne 'codex/recover-payout-settlement') { throw 'Start nur auf codex/recover-payout-settlement erlaubt.' }
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw "Port $Port ist bereits belegt. Vorhandene Instanz nicht verändert." }
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$required = @('SUPABASE_ACCESS_TOKEN','SUPABASE_PROJECT_REF')
foreach ($name in $required) { if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) { throw "$name fehlt." } }
$oldBackend = $env:PAYMENT_BACKEND
try {
    $env:PAYMENT_BACKEND = 'supabase'
    $logDir = Join-Path ([IO.Path]::GetTempPath()) 'payment-tool-runtime'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $process = Start-Process -FilePath $pythonPath -ArgumentList @('-m', 'streamlit', 'run', 'app.py', '--server.address=127.0.0.1', "--server.port=$Port", '--server.headless=true', '--browser.gatherUsageStats=false', '--server.fileWatcherType=none') -WorkingDirectory $repoPath -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'recovery.stdout.log') -RedirectStandardError (Join-Path $logDir 'recovery.stderr.log') -PassThru
    $process.Id | Set-Content -LiteralPath (Join-Path $logDir 'recovery.pid')
} finally {
    $env:PAYMENT_BACKEND = $oldBackend
}
Write-Output "Recovery-Version: http://127.0.0.1:$Port"
