<#
.SYNOPSIS
  g-i-t-data を初期化する: 履歴とデータを消し、設定ファイルだけを残して最初からやり直す。

.DESCRIPTION
  1. 事前チェック (対象リポジトリ・ブランチ・実行中のワークフロー)
  2. バックアップ (git clone --mirror)
  3. stalk.yml / archive.yml を無効化
  4. g-i-t-data の履歴を作り直し (config.yaml / README.md / .gitignore だけを残して force push)
  5. Supabase の updates / archive_queue を空にする
  6. ワークフローを有効化 (-Run で stalk.yml も実行)

  失敗した場合、ワークフローは無効のままにします (データが中途半端な状態で走らせないため)。
  Internet Archive に保存済みのものは消えません。

.EXAMPLE
  .\scripts\reset-data.ps1 -DryRun        # 何も変更せず、実行内容だけを表示
  .\scripts\reset-data.ps1                # 確認入力のあと、初期化
  .\scripts\reset-data.ps1 -Run -Yes      # 確認なしで初期化し、stalk.yml を実行

.NOTES
  実行ポリシーで拒否される場合: pwsh -ExecutionPolicy Bypass -File .\scripts\reset-data.ps1
#>
[CmdletBinding()]
param(
    [string]$DataDir,                                   # 既定: g-i-t-app の隣の g-i-t-data
    [string]$DataRepo = 'TanukiMa/g-i-t-data',          # origin の URL がこれを含むことを確認する
    [string]$AppRepo  = 'TanukiMa/g-i-t-app',
    [string[]]$Keep = @('config.yaml', 'README.md', '.gitignore'),
    [string]$BackupDir,                                 # 既定: g-i-t-data の隣の g-i-t-data-backup-<日時>.git
    [switch]$NoBackup,
    [switch]$SkipSupabase,
    [switch]$SkipWorkflows,
    [switch]$Run,                                       # 完了後に stalk.yml を実行する
    [switch]$Yes,                                       # 確認入力を省略する
    [switch]$DryRun                                     # 変更を伴う操作は表示だけにする
)

$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path -Parent $PSScriptRoot
if (-not $DataDir) { $DataDir = Join-Path (Split-Path -Parent $AppRoot) 'g-i-t-data' }
$Workflows = @('stalk.yml', 'archive.yml')

function Exec {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [string[]]$ArgList = @(),
        [string]$WorkDir = $DataDir,
        [switch]$Mutating,    # 変更を伴う操作 (-DryRun では実行しない)
        [switch]$AllowFail
    )
    $line = "$Exe $($ArgList -join ' ')"
    if ($Mutating -and $DryRun) { Write-Host "[dry-run] $line" -ForegroundColor DarkYellow; return '' }
    Write-Host "> $line" -ForegroundColor DarkGray
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'   # git/gh は進捗を stderr に出す。Windows PowerShell 5.1 で例外化させない
    Push-Location $WorkDir
    try {
        $out = (& $Exe @ArgList 2>&1 | Out-String).Trim()
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0 -and -not $AllowFail) { throw "コマンドが失敗しました (exit $code): $line`n$out" }
    return $out
}

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "$Name コマンドが見つかりません。" }
}

# supabase の起動方法を決める。supabase.exe があればそれを使う。なければ (npm の shim や関数だと引数が落ちることがあるので)
# shim を通さずに npx で直接呼ぶ。
function Get-SupabaseCommand {
    $app = Get-Command supabase -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($app -and $app.Source -match '\.exe$') { return @{ Exe = $app.Source; Pre = @() } }
    if (Get-Command npx -ErrorAction SilentlyContinue) { return @{ Exe = 'npx'; Pre = @('--yes', 'supabase') } }
    if ($app) { return @{ Exe = $app.Source; Pre = @() } }
    throw 'supabase コマンドも npx も見つかりません。'
}

# SQL (ASCII のみ) を一時ファイルに書いて `supabase db query --linked -f` で実行する。引数で渡すと、&・|・' などが
# cmd/npx の経由で壊れることがある。-Mutating は -DryRun では実行しない。
function Invoke-SupabaseSql {
    param([Parameter(Mandatory)][string]$Sql, [switch]$Mutating)
    if ($Mutating -and $DryRun) { Write-Host "[dry-run] supabase db query --linked: $Sql" -ForegroundColor DarkYellow; return '' }
    $cli = Get-SupabaseCommand
    $file = Join-Path ([IO.Path]::GetTempPath()) ("reset-data-{0}.sql" -f [guid]::NewGuid().ToString('N'))
    [IO.File]::WriteAllText($file, $Sql, (New-Object System.Text.UTF8Encoding($false)))
    try {
        $env:npm_config_yes = 'true'   # npx 経由のとき、「Ok to proceed?」で止まらないようにする
        $cliArgs = @($cli.Pre) + @('db', 'query', '--linked', '-f', $file)
        return Exec $cli.Exe $cliArgs -WorkDir $AppRoot
    } finally {
        Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
    }
}

try {
    Write-Host "=== g-i-t-data 初期化 $(if ($DryRun) { '(ドライラン)' }) ===" -ForegroundColor Cyan

    # ---- 1. 事前チェック ----
    Require-Command git
    if (-not $SkipWorkflows) { Require-Command gh }
    if (-not $SkipSupabase)  { [void](Get-SupabaseCommand) }
    if (-not (Test-Path -LiteralPath $DataDir)) { throw "DataDir が存在しません: $DataDir" }
    $DataDir = (Resolve-Path -LiteralPath $DataDir).Path

    $top = (Exec git @('rev-parse', '--show-toplevel')) -replace '/', '\'
    if ((Resolve-Path -LiteralPath $top).Path -ne $DataDir) {
        throw "DataDir は Git リポジトリのルートではありません (ルート: $top)。親リポジトリを誤って初期化しないよう中止します。"
    }
    $origin = Exec git @('remote', 'get-url', 'origin')
    if ($origin -notlike "*$DataRepo*") { throw "origin ($origin) が $DataRepo ではありません。" }
    if ((Exec git @('branch', '--show-current')) -ne 'main') { throw 'main ブランチで実行してください。' }
    if (Exec git @('status', '--porcelain', '--untracked-files=no')) {
        throw '未コミットの変更があります。コミットまたは破棄してから実行してください。'
    }
    if (-not $SkipWorkflows) {
        $busy = Exec gh @('run', 'list', '--repo', $AppRepo, '--json', 'status', '-q', '[.[] | select(.status != "completed")] | length')
        if ($busy -ne '0') { throw "実行中または待機中のワークフローが $busy 件あります。完了後に再実行してください。" }
    }

    Exec git @('pull', '--ff-only') -Mutating | Out-Null    # 手元が古いと、古い config.yaml を残してしまう
    $missing = $Keep | Where-Object { $_ -eq 'config.yaml' -and -not (Test-Path -LiteralPath (Join-Path $DataDir $_)) }
    if ($missing) { throw 'config.yaml が見つかりません。' }
    $keepExisting = @($Keep | Where-Object { Test-Path -LiteralPath (Join-Path $DataDir $_) })
    $tracked = @((Exec git @('ls-files')) -split '\r?\n' | ForEach-Object { $_.Trim() } | Where-Object { $_ } |
        ForEach-Object { ($_ -split '/')[0] } | Sort-Object -Unique)
    $toDelete = @($tracked | Where-Object { $keepExisting -notcontains $_ })

    # ---- 計画の表示と確認 ----
    Write-Host ''
    Write-Host "対象リポジトリ : $DataRepo  ($DataDir)"
    Write-Host "残すファイル   : $($keepExisting -join ', ')"
    Write-Host "削除するもの   : $($toDelete -join ', ')  + 全コミット履歴 (main を force push)"
    $linkedRef = $null
    $refFile = Join-Path $AppRoot 'supabase\.temp\project-ref'
    if (Test-Path -LiteralPath $refFile) { $linkedRef = (Get-Content -LiteralPath $refFile -Raw).Trim() }
    Write-Host "Supabase       : $(if ($SkipSupabase) { '変更しない' } else { 'updates / archive_queue を truncate (リンク先プロジェクト: ' + $(if ($linkedRef) { $linkedRef } else { '不明' }) + ')' })"
    Write-Host "ワークフロー   : $(if ($SkipWorkflows) { '変更しない' } else { ($Workflows -join ', ') + ' を一時的に無効化' })"
    Write-Host "バックアップ   : $(if ($NoBackup) { 'なし' } else { 'あり (mirror clone)' })"
    Write-Host ''
    if (-not $DryRun -and -not $Yes) {
        $answer = Read-Host "続行するにはリポジトリ名 '$DataRepo' を入力してください"
        if ($answer -ne $DataRepo) { Write-Host '中止しました。'; exit 1 }
    }

    # ---- 2. バックアップ ----
    if (-not $NoBackup) {
        if (-not $BackupDir) {
            $BackupDir = Join-Path (Split-Path -Parent $DataDir) ("g-i-t-data-backup-{0}.git" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
        }
        Exec git @('clone', '--mirror', $origin, $BackupDir) -Mutating | Out-Null
        Write-Host "バックアップ: $BackupDir" -ForegroundColor Green
    }

    # ---- 3. ワークフローを無効化 ----
    if (-not $SkipWorkflows) {
        foreach ($wf in $Workflows) { Exec gh @('workflow', 'disable', $wf, '--repo', $AppRepo) -Mutating | Out-Null }
    }

    # ---- 4. 履歴を作り直す ----
    Exec git @('branch', '-D', 'fresh') -AllowFail | Out-Null
    Exec git @('checkout', '--orphan', 'fresh') -Mutating | Out-Null
    Exec git @('rm', '-rf', '--cached', '.', '-q') -Mutating | Out-Null
    foreach ($name in $toDelete) {
        $path = Join-Path $DataDir $name
        if ($DryRun) { Write-Host "[dry-run] Remove-Item -Recurse -Force $path" -ForegroundColor DarkYellow }
        elseif (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
    }
    Exec git @(@('add', '--') + $keepExisting) -Mutating | Out-Null
    Exec git @('commit', '-m', 'Reset: keep configuration only') -Mutating | Out-Null
    Exec git @('branch', '-M', 'main') -Mutating | Out-Null
    Exec git @('push', '--force', 'origin', 'main') -Mutating | Out-Null

    # ---- 5. Supabase ----
    if (-not $SkipSupabase) {
        # The dashboard is built from these tables, so rows left behind would keep showing commits that no longer
        # exist. Never trust the exit code alone: count the rows afterwards (ASCII marker -> independent of the console encoding).
        $countSql = "select 'COUNTS ' || (select count(*) from updates) || ' ' || (select count(*) from archive_queue) as c;"
        function Get-SupabaseCounts {
            $out = Invoke-SupabaseSql $countSql
            if ($out -notmatch 'COUNTS (\d+) (\d+)') {
                throw "Supabase の件数を確認できませんでした。次を実行して、updates と archive_queue が空か確認してください:`n  supabase db query --linked `"$countSql`"`n出力: $out"
            }
            return @([int]$Matches[1], [int]$Matches[2])
        }

        Invoke-SupabaseSql 'truncate updates, archive_queue restart identity;' -Mutating | Out-Null
        if (-not $DryRun) {
            $counts = Get-SupabaseCounts
            if ($counts[0] -ne 0 -or $counts[1] -ne 0) {
                # `truncate` reported success but removed nothing (seen with `supabase db query`): fall back to DELETE.
                Write-Host "truncate では空になりませんでした (updates=$($counts[0]), archive_queue=$($counts[1]))。delete で空にします。" -ForegroundColor Yellow
                Invoke-SupabaseSql 'delete from archive_queue;' | Out-Null
                Invoke-SupabaseSql 'delete from updates;' | Out-Null
                $counts = Get-SupabaseCounts
            }
            if ($counts[0] -ne 0 -or $counts[1] -ne 0) {
                throw "Supabase の行が消えません (updates=$($counts[0]), archive_queue=$($counts[1]))。リンク先のプロジェクトが SUPABASE_URL のものと同じか、権限があるかを確認してください。"
            }
            Write-Host "Supabase: updates / archive_queue は空です。" -ForegroundColor Green
        }
    }

    # ---- 6. ワークフローを再開 ----
    if (-not $SkipWorkflows) {
        foreach ($wf in $Workflows) { Exec gh @('workflow', 'enable', $wf, '--repo', $AppRepo) -Mutating | Out-Null }
        if ($Run) { Exec gh @('workflow', 'run', 'stalk.yml', '--repo', $AppRepo) -Mutating | Out-Null }
    }

    Write-Host ''
    Write-Host "$(if ($DryRun) { 'ドライラン完了 (変更なし)' } else { '初期化が完了しました' })" -ForegroundColor Green
    if (-not $DryRun -and -not $SkipWorkflows -and -not $Run) {
        Write-Host "次: gh workflow run stalk.yml --repo $AppRepo"
    }
}
catch {
    Write-Host ''
    Write-Host "エラー: $($_.Exception.Message)" -ForegroundColor Red
    if (-not $SkipWorkflows -and -not $DryRun) {
        Write-Host '途中で失敗した可能性があります。ワークフローは (無効化済みなら) 無効のままです。状態を確認してから、手動で有効化してください:' -ForegroundColor Yellow
        foreach ($wf in $Workflows) { Write-Host "  gh workflow enable $wf --repo $AppRepo" -ForegroundColor Yellow }
    }
    exit 1
}
