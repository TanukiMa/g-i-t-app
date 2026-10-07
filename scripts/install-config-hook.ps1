<#
.SYNOPSIS
  g-i-t-data に、config.yaml を commit する前に検査する Git フック (pre-commit) を入れる。

.DESCRIPTION
  config.yaml を含む commit のとき、ステージされた内容を scripts/check_config.py で検査し、エラーがあれば commit を止める。
  (YAML が読めない、全角の引用符、スラッグの規則違反・重複、使えない正規表現、不正な CSS セレクターなど)
  警告は表示するだけで、commit は止めない。1 回だけ飛ばすときは  git commit --no-verify

  フックは g-i-t-data の .git/hooks/pre-commit に書く (リポジトリには含まれない。clone し直したら、もう一度入れる)。
  すでに別の pre-commit フックがあるときは、上書きしない (-Force で、.bak に退避してから置き換える)。
  Python が使えること (python / python3 / py のどれか。PyYAML が入っていること: pip install -r requirements.txt)。

.PARAMETER DataDir
  g-i-t-data の作業ツリー (既定: このリポジトリの隣の g-i-t-data)。

.PARAMETER Uninstall
  このスクリプトが入れたフックを外す。

.PARAMETER Force
  別のフックがあっても、.bak に退避して、置き換える。

.EXAMPLE
  pwsh .\scripts\install-config-hook.ps1
  pwsh .\scripts\install-config-hook.ps1 -DataDir D:\work\g-i-t-data
#>
[CmdletBinding()]
param(
    [string]$DataDir,
    [switch]$Uninstall,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path -Parent $PSScriptRoot
if (-not $DataDir) { $DataDir = Join-Path (Split-Path -Parent $AppRoot) 'g-i-t-data' }
$Marker = '# g-i-t: config.yaml check (installed by scripts/install-config-hook.ps1)'

try {
    if (-not (Test-Path -LiteralPath (Join-Path $DataDir '.git'))) { throw "g-i-t-data の作業ツリーではありません (.git がありません): $DataDir" }
    $DataDir = (Resolve-Path -LiteralPath $DataDir).Path
    $hookDir = Join-Path $DataDir '.git\hooks'
    $hook = Join-Path $hookDir 'pre-commit'
    New-Item -ItemType Directory -Force -Path $hookDir | Out-Null
    $existing = if (Test-Path -LiteralPath $hook) { [IO.File]::ReadAllText($hook) } else { '' }

    if ($Uninstall) {
        if ($existing -and $existing.Contains($Marker)) { Remove-Item -LiteralPath $hook -Force; Write-Host "フックを外しました: $hook" -ForegroundColor Green }
        else { Write-Host 'このスクリプトが入れたフックは、ありません。' }
        exit 0
    }

    if ($existing -and -not $existing.Contains($Marker)) {
        if (-not $Force) { throw "別の pre-commit フックがあります: $hook`n  置き換えるなら -Force (元のものは pre-commit.bak に退避します)。" }
        Copy-Item -LiteralPath $hook -Destination "$hook.bak" -Force
        Write-Host "元のフックを退避しました: $hook.bak" -ForegroundColor Yellow
    }

    $checker = (Join-Path $AppRoot 'scripts\check_config.py') -replace '\\', '/'
    $script = @"
#!/bin/sh
$Marker
# Only when config.yaml is part of this commit: check the STAGED version (what will really be committed).
if git diff --cached --name-only | grep -qx 'config.yaml'; then
  PY=python
  command -v python >/dev/null 2>&1 || PY=python3
  command -v "`$PY" >/dev/null 2>&1 || PY="py"
  if ! git show :config.yaml | "`$PY" "$checker" --stdin; then
    echo "" >&2
    echo "config.yaml has errors: the commit was stopped. Fix them and commit again (once only: git commit --no-verify)." >&2
    exit 1
  fi
fi
exit 0
"@
    # LF line endings, no BOM: Git for Windows runs the hook with its own sh.
    [IO.File]::WriteAllText($hook, ($script -replace "`r`n", "`n"), (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "フックを入れました: $hook" -ForegroundColor Green
    Write-Host "  検査するスクリプト: $checker"
    Write-Host '  確認: config.yaml を変更して git add し、git commit すると、検査が走ります。'
} catch {
    Write-Host "エラー: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
