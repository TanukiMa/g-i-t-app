<#
.SYNOPSIS
  docs/wiki/*.md を GitHub Wiki (g-i-t-data.wiki) に取り込む。

.DESCRIPTION
  初回の取り込み用です。取り込んだ後は Wiki 側 (Web 画面) を正として編集してください。
  既に Wiki にあるページは、-Overwrite を付けない限り上書きしません。

  事前に一度だけ: GitHub の g-i-t-data → Wiki タブで、最初のページ (Home) を作成してください
  (これで g-i-t-data.wiki.git ができます)。Wiki の編集権限が「共同作業者のみ」であることも確認してください。

.EXAMPLE
  pwsh .\scripts\publish-wiki.ps1 -DryRun     # 何も変更せず、取り込む内容だけを表示
  pwsh .\scripts\publish-wiki.ps1             # 新しいページだけ取り込む
  pwsh .\scripts\publish-wiki.ps1 -Overwrite  # 既存のページも上書きする
#>
[CmdletBinding()]
param(
    [string]$WikiUrl = 'https://github.com/TanukiMa/g-i-t-data.wiki.git',
    [string]$SourceDir,                                 # 既定: g-i-t-app\docs\wiki
    [switch]$Overwrite,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 では param() の既定値で $PSScriptRoot が空になるため、ここで補う。
if (-not $SourceDir) { $SourceDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'docs\wiki' }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'git コマンドが見つかりません。' }
if (-not (Test-Path -LiteralPath $SourceDir)) { throw "SourceDir がありません: $SourceDir" }

function Git-Run {
    param([string[]]$GitArgs, [string]$WorkDir, [switch]$AllowFail)
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'   # git は進捗を stderr に出す
    Push-Location $WorkDir
    try { $out = (& git @GitArgs 2>&1 | Out-String).Trim(); $code = $LASTEXITCODE } finally { Pop-Location; $ErrorActionPreference = $prev }
    if ($code -ne 0 -and -not $AllowFail) { throw "git $($GitArgs -join ' ') が失敗しました (exit $code)`n$out" }
    return $out
}

$work = Join-Path ([IO.Path]::GetTempPath()) ("git-wiki-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
try {
    Write-Host "Wiki を取得します: $WikiUrl"
    try { Git-Run @('clone', '--quiet', $WikiUrl, $work) -WorkDir (Get-Location).Path | Out-Null }
    catch { throw "Wiki を clone できません。GitHub の Wiki タブで最初のページ (Home) を作成したか確認してください。`n$($_.Exception.Message)" }

    $files = Get-ChildItem -LiteralPath $SourceDir -Filter '*.md' -File
    if (-not $files) { throw "取り込む .md がありません: $SourceDir" }

    $added = @(); $updated = @(); $skipped = @()
    foreach ($f in $files) {
        $dest = Join-Path $work $f.Name
        if (Test-Path -LiteralPath $dest) {
            if ((Get-FileHash $dest).Hash -eq (Get-FileHash $f.FullName).Hash) { continue }          # 同じ内容
            if (-not $Overwrite) { $skipped += $f.Name; continue }
            $updated += $f.Name
        } else { $added += $f.Name }
        if (-not $DryRun) { Copy-Item -LiteralPath $f.FullName -Destination $dest -Force }
    }

    Write-Host ''
    Write-Host ("追加      : {0}" -f ($(if ($added) { $added -join ', ' } else { '(なし)' })))
    Write-Host ("上書き    : {0}" -f ($(if ($updated) { $updated -join ', ' } else { '(なし)' })))
    Write-Host ("スキップ  : {0}  ← 既に Wiki にある。上書きするには -Overwrite" -f ($(if ($skipped) { $skipped -join ', ' } else { '(なし)' })))
    if ($DryRun) { Write-Host "`nドライラン完了 (変更なし)" -ForegroundColor Green; return }
    if (-not ($added + $updated)) { Write-Host "`n変更はありません。"; return }

    Git-Run @('add', '-A') -WorkDir $work | Out-Null
    Git-Run @('commit', '-m', 'Import wiki pages from g-i-t-app/docs/wiki') -WorkDir $work | Out-Null
    Git-Run @('push', 'origin', 'HEAD') -WorkDir $work | Out-Null
    Write-Host "`n取り込みました: https://github.com/TanukiMa/g-i-t-data/wiki" -ForegroundColor Green
}
catch {
    Write-Host "エラー: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
finally {
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
}
