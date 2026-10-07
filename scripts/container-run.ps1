Set-Location -LiteralPath $PSScriptRoot

$logDir = Join-Path $PSScriptRoot 'logs'
$log    = Join-Path $logDir 'run.log'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-Log([string]$msg) {
    "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $msg |
        Add-Content -LiteralPath $log -Encoding utf8
}

Write-Log 'start'
& wslc run --rm --env-file .env g-i-t-app 2>&1 |
    ForEach-Object { "$_" } |
    Add-Content -LiteralPath $log -Encoding utf8
$code = $LASTEXITCODE
Write-Log "exit=$code"
exit $code
