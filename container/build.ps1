# Builds the image of one deploy target from anywhere (the build context must be the repository root).
#   pwsh container/build.ps1                 # firebase (default)
#   pwsh container/build.ps1 cloudflare
#   pwsh container/build.ps1 base -Engine docker
# The image is tagged g-i-t-app:<target>.
param(
    [ValidateSet('firebase', 'cloudflare', 'base')] [string]$Target = 'firebase',
    [ValidateSet('wslc', 'docker')] [string]$Engine = 'wslc',
    [string[]]$ExtraArgs = @()
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot      # container/.. = the repository root
Push-Location $root
try {
    Write-Host "Building g-i-t-app:$Target with $Engine from $root"
    & $Engine build -f container/Dockerfile --target $Target -t "g-i-t-app:$Target" @ExtraArgs .
    if ($LASTEXITCODE -ne 0) { throw "$Engine build failed (exit $LASTEXITCODE)" }
    & $Engine image list
} finally {
    Pop-Location
}
