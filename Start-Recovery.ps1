param(
    [string]$DataDir = '',
    [int]$Port = 8511
)
$ErrorActionPreference = 'Stop'
$repoPath = (Resolve-Path -LiteralPath $PSScriptRoot).Path
if ([string]::IsNullOrWhiteSpace($DataDir)) { $DataDir = $repoPath }
$branch = git -C $repoPath branch --show-current
if ($branch -ne 'codex/recover-payout-settlement') { throw 'Start nur auf codex/recover-payout-settlement erlaubt.' }
if (-not (Test-Path -LiteralPath $DataDir -PathType Container)) { throw 'Datenverzeichnis fehlt. Vorhandenen Recovery-Datenbestand mit Register verwenden.' }
$DataDir = (Resolve-Path -LiteralPath $DataDir).Path
if ($repoPath -match '[\\/]OneDrive[\\/]' -or $DataDir -match '[\\/]OneDrive[\\/]') { throw 'Operativer Start aus OneDrive ist gesperrt. Das Dropbox-Durchstarter-Verzeichnis verwenden.' }
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw "Port $Port ist bereits belegt. Vorhandene Instanz nicht verändert." }
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$oldDataDir = $env:PAYMENT_DATA_DIR
try {
    $env:PAYMENT_DATA_DIR = $DataDir
    $process = Start-Process -FilePath $pythonPath -ArgumentList @('-m', 'streamlit', 'run', 'app.py', '--server.address=127.0.0.1', "--server.port=$Port", '--server.headless=true', '--browser.gatherUsageStats=false', '--server.fileWatcherType=none', '--theme.base=light', '--theme.primaryColor=#246bfe', '--theme.backgroundColor=#f5f7fb', '--theme.secondaryBackgroundColor=#ffffff', '--theme.textColor=#17243c') -WorkingDirectory $repoPath -WindowStyle Hidden -RedirectStandardOutput (Join-Path $DataDir 'recovery.stdout.log') -RedirectStandardError (Join-Path $DataDir 'recovery.stderr.log') -PassThru
    $process.Id | Set-Content -LiteralPath (Join-Path $DataDir 'recovery.pid')
} finally {
    $env:PAYMENT_DATA_DIR = $oldDataDir
}
Write-Output "Recovery-Version: http://127.0.0.1:$Port"
