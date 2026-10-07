<#
.SYNOPSIS
  docs/system.d2 (G醫t を1枚で説明する図) を static/system.svg に描画する。

.DESCRIPTION
  ライト/ダークの両テーマを1つの SVG に埋め込む (ブラウザの設定に自動で追従)。
  図を直したら、このスクリプトを実行して static/system.svg をコミットする。
  D2 のインストール: https://d2lang.com/tour/install

.EXAMPLE
  pwsh .\scripts\render-diagram.ps1
#>
[CmdletBinding()]
param(
    [string]$Source,   # 既定: g-i-t-app\docs\system.d2
    [string]$Output    # 既定: g-i-t-app\static\system.svg
)

$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 では param() の既定値で $PSScriptRoot が空になるため、ここで補う。
$AppRoot = Split-Path -Parent $PSScriptRoot
if (-not $Source) { $Source = Join-Path $AppRoot 'docs\system.d2' }
if (-not $Output) { $Output = Join-Path $AppRoot 'static\system.svg' }
if (-not (Get-Command d2 -ErrorAction SilentlyContinue)) { throw 'd2 コマンドが見つかりません。https://d2lang.com/tour/install' }

& d2 fmt $Source
& d2 --theme 0 --dark-theme 200 --pad 24 $Source $Output
if ($LASTEXITCODE -ne 0) { throw "d2 が失敗しました (exit $LASTEXITCODE)" }
Write-Host ("OK: {0} ({1:N0} bytes)" -f $Output, (Get-Item $Output).Length)
