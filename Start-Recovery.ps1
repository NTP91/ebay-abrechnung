param(
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'PaymentTool-Test\recover-6e82d0d-20260903\test-data'),
    [int]$Port = 8511
)
$ErrorActionPreference = 'Stop'
$repoPath = $PSScriptRoot
$branch = git -C $repoPath branch --show-current
if ($branch -ne 'codex/recover-payout-settlement') { throw 'Start nur auf codex/recover-payout-settlement erlaubt.' }
if (-not (Test-Path -LiteralPath $DataDir -PathType Container)) { throw 'Datenverzeichnis fehlt. Vorhandenen Recovery-Datenbestand mit Register verwenden.' }
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw "Port $Port ist bereits belegt. Vorhandene Instanz nicht verändert." }
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$oldDataDir = $env:PAYMENT_DATA_DIR
try {
    $env:PAYMENT_DATA_DIR = (Resolve-Path -LiteralPath $DataDir).Path
    $process = Start-Process -FilePath $pythonPath -ArgumentList @('-m', 'streamlit', 'run', 'app.py', '--server.address=127.0.0.1', "--server.port=$Port", '--server.headless=true', '--browser.gatherUsageStats=false', '--server.fileWatcherType=none') -WorkingDirectory $repoPath -WindowStyle Hidden -RedirectStandardOutput (Join-Path $DataDir 'recovery.stdout.log') -RedirectStandardError (Join-Path $DataDir 'recovery.stderr.log') -PassThru
    $process.Id | Set-Content -LiteralPath (Join-Path $DataDir 'recovery.pid')
} finally {
    $env:PAYMENT_DATA_DIR = $oldDataDir
}
Write-Output "Recovery-Version: http://127.0.0.1:$Port"
