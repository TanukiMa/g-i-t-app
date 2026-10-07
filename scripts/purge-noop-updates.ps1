<#
.SYNOPSIS
  AI が「内容に実質的な変更はありません」と要約した更新を、ダッシュボードの記録から消す。

.DESCRIPTION
  消すもの:
    1. Supabase の updates の該当行 (summary が『内容に実質的な変更はありません』で始まる行)
    2. Supabase の archive_queue の、同じコミットの行 (Wayback への保存予定・結果の記録)
    3. -DataDir を指定したとき: g-i-t-data の public/sites/<slug>/diff_<hash7>.html (作業ツリーのファイルを消すだけ。
       commit / push は、表示される案内のとおり、あとで自分で行う)

  消さないもの:
    g-i-t-data の git の履歴 (コミット自体)。コミットは、次の更新の差分の土台で、ハッシュが Supabase・差分ページ・GitHub の
    リンクの鍵なので、書き換えると全部が壊れる。履歴に残るのは、1 行ほどの小さな差分だけで、サイズへの影響は無視できる。

  手順: 件数を表示 → 「yes」と入力されたときだけ削除 → もう一度数えて確認。
  ダッシュボードは、次の実行 (stalk) で、Supabase から作り直されるので、消した更新は、一覧・更新歴・検索・Atom から無くなる。
  SQL は ASCII のみ (Unicode エスケープ) で、一時ファイルに書いて -f で渡す。supabase.exe があればそれを、なければ npx を使う。

.PARAMETER CountOnly
  件数と対象のコミットを表示するだけで、何も変更しない。

.PARAMETER DataDir
  g-i-t-data の作業ツリー。指定すると、対応する差分ページ (public/sites/<slug>/diff_<hash7>.html) も消す。

.PARAMETER Yes
  確認の入力を省略する。

.EXAMPLE
  pwsh .\scripts\purge-noop-updates.ps1 -CountOnly
  pwsh .\scripts\purge-noop-updates.ps1 -DataDir ..\g-i-t-data
#>
[CmdletBinding()]
param(
    [switch]$CountOnly,
    [string]$DataDir,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path -Parent $PSScriptRoot

# Windows PowerShell 5.1 でも読めるよう、このスクリプトは BOM 付き UTF-8 で保存してある。
$Prefix = '内容に実質的な変更はありません'

# 文字コードに依存しない、ASCII だけの文字列リテラル (Postgres の Unicode エスケープ)
function ConvertTo-PgUnicode([string]$Text) {
    $sb = New-Object System.Text.StringBuilder
    foreach ($ch in $Text.ToCharArray()) { [void]$sb.AppendFormat('\{0:X4}', [int]$ch) }
    return "U&'" + $sb.ToString() + "'"
}
$Like = (ConvertTo-PgUnicode $Prefix) + " || '%'"

function Get-SupabaseCommand {
    $app = Get-Command supabase -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($app -and $app.Source -match '\.exe$') { return @{ Exe = $app.Source; Pre = @() } }
    if (Get-Command npx -ErrorAction SilentlyContinue) { return @{ Exe = 'npx'; Pre = @('--yes', 'supabase') } }
    if ($app) { return @{ Exe = $app.Source; Pre = @() } }
    throw 'supabase コマンドも npx も見つかりません。'
}

function Invoke-Sql([string]$Sql) {
    $file = Join-Path ([IO.Path]::GetTempPath()) ("purge-noop-{0}.sql" -f [guid]::NewGuid().ToString('N'))
    [IO.File]::WriteAllText($file, $Sql, (New-Object System.Text.UTF8Encoding($false)))
    $cli = Get-SupabaseCommand
    $cliArgs = @($cli.Pre) + @('db', 'query', '--linked', '-f', $file)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $env:npm_config_yes = 'true'
    Push-Location $AppRoot
    try {
        $out = (& $cli.Exe @cliArgs 2>&1 | Out-String).Trim()
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
        $ErrorActionPreference = $prev
        Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
    }
    if ($code -ne 0) { throw "supabase db query が失敗しました (exit $code): $($cli.Exe) $($cliArgs -join ' ')`n$out" }
    return $out
}

function Get-Counts {
    $sql = "select 'COUNTS ' || (select count(*) from updates where summary like $Like) || ' ' || " +
           "(select count(*) from archive_queue q where exists (select 1 from updates u where u.summary like $Like " +
           "and u.site_slug = q.site_slug and u.commit_hash = q.commit_hash)) || ' ' || (select count(*) from updates) as c;"
    $out = Invoke-Sql $sql
    if ($out -notmatch 'COUNTS (\d+) (\d+) (\d+)') { throw "件数を読み取れませんでした。出力の先頭:`n$(($out -split "`n" | Select-Object -First 15) -join "`n")" }
    return [pscustomobject]@{ Updates = [int]$Matches[1]; Queue = [int]$Matches[2]; Total = [int]$Matches[3] }
}

function Get-Pairs {
    $sql = "select 'PAIR ' || site_slug || ' ' || left(commit_hash, 7) as c from updates where summary like $Like order by site_slug, id;"
    $out = Invoke-Sql $sql
    return @([regex]::Matches($out, 'PAIR (\S+) ([0-9a-f]{7})') | ForEach-Object { [pscustomobject]@{ Slug = $_.Groups[1].Value; Hash = $_.Groups[2].Value } })
}

try {
    $before = Get-Counts
    Write-Host "削除の対象: updates $($before.Updates) 行 / archive_queue $($before.Queue) 行  (updates 全体 $($before.Total) 行)"
    if ($before.Updates -eq 0) { Write-Host '対象はありません。' -ForegroundColor Green; exit 0 }

    $pairs = Get-Pairs
    Write-Host "サイト別: " -NoNewline
    Write-Host (($pairs | Group-Object Slug | Sort-Object Count -Descending | ForEach-Object { "$($_.Name)=$($_.Count)" }) -join ', ')
    if ($pairs.Count -ne $before.Updates) { throw "対象のコミットの一覧 ($($pairs.Count) 件) が、件数 ($($before.Updates) 件) と合いません。" }

    $files = @()
    if ($DataDir) {
        if (-not (Test-Path -LiteralPath $DataDir)) { throw "DataDir が存在しません: $DataDir" }
        $DataDir = (Resolve-Path -LiteralPath $DataDir).Path
        $files = @($pairs | ForEach-Object { Join-Path $DataDir "public\sites\$($_.Slug)\diff_$($_.Hash).html" } | Where-Object { Test-Path -LiteralPath $_ })
        Write-Host "消す差分ページ: $($files.Count) 件 ($DataDir\public\sites\<slug>\diff_<hash7>.html)"
    }
    if ($CountOnly) { Write-Host '(-CountOnly: 変更しません)'; exit 0 }

    if (-not $Yes) {
        $answer = Read-Host "Supabase の updates $($before.Updates) 行と archive_queue $($before.Queue) 行を削除します (元に戻せません)。続けますか? (yes/no)"
        if ($answer -ne 'yes') { Write-Host '中止しました。変更していません。'; exit 0 }
    }

    # archive_queue を先に (updates の行を、結び付けの鍵として使うため)
    $del = "delete from archive_queue q using updates u where u.summary like $Like and u.site_slug = q.site_slug and u.commit_hash = q.commit_hash; " +
           "delete from updates where summary like $Like;"
    Invoke-Sql $del | Out-Null

    $after = Get-Counts
    Write-Host "削除後 -> 対象: updates $($after.Updates) / archive_queue $($after.Queue) / updates 全体 $($after.Total)"
    if ($after.Updates -ne 0 -or $after.Queue -ne 0 -or $after.Total -ne ($before.Total - $before.Updates)) {
        throw '件数が想定と合いません。Supabase の内容を確認してください。'
    }

    foreach ($f in $files) { Remove-Item -LiteralPath $f -Force }
    if ($files.Count) {
        Write-Host "差分ページを $($files.Count) 件、作業ツリーから削除しました。次で記録・公開してください:" -ForegroundColor Yellow
        Write-Host "  git -C `"$DataDir`" add -A public"
        Write-Host "  git -C `"$DataDir`" commit -m `"Remove diff pages of updates without substantive change`""
        Write-Host "  git -C `"$DataDir`" push"
    }
    Write-Host '完了しました。ダッシュボードは、次の実行で、Supabase から作り直されます (stalk.yml / コンテナを、手動で動かすと、すぐ反映されます)。' -ForegroundColor Green
} catch {
    Write-Host "エラー: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
