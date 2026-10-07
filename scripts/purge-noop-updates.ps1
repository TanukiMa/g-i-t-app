<#
.SYNOPSIS
  更新歴を、ダッシュボードの記録から消す。
  既定: AI が「内容に実質的な変更はありません」と要約した更新。-Commit: 指定した commit の更新。
  -Contains: git の差分に、指定した文字列を含む commit の更新。

.DESCRIPTION
  消すもの:
    1. Supabase の updates の該当行
    2. Supabase の archive_queue の、同じコミットの行 (Wayback への保存予定・結果)
    3. -DataDir を指定したとき: g-i-t-data の public/sites/<slug>/diff_<hash7>.html (作業ツリーのファイルを消すだけ。
       commit / push は、表示される案内のとおり、あとで自分で行う)

  対象の選び方:
    (既定)     summary が『内容に実質的な変更はありません』で始まる行
    -Commit    commit のハッシュ (先頭 7 文字以上) を指定した行。要約が何であっても消す。1 つの指定が 2 行以上に
               当たるとき (ハッシュが曖昧) は、何も消さずに止まる。当たる行がない指定は、警告して飛ばす。
    -Contains  g-i-t-data の git で、差分 (追加・削除された行) に、その文字列を含む commit (git log -G)。そのうち、
               Supabase に更新として記録されている行を消す。-DataDir が必要。-Site で、サイトを絞る。順番が入れ替わっただけの
               差分も、行が変わっているので、見つかる。-Commit と一緒に使うと、両方に当たる行を消す。

  消さないもの:
    g-i-t-data の git の履歴 (コミット自体)。コミットは、次の更新の差分の土台で、ハッシュが Supabase・差分ページ・GitHub の
    リンクの鍵なので、書き換えると全部が壊れる。git revert で打ち消す方法も、お勧めしない (保存ページが、サイトの今の内容と
    食い違い、次の実行で、また更新として出る。後続の commit とコンフリクトもする)。

  手順: 件数と対象を表示 → 「yes」と入力されたときだけ削除 → もう一度数えて確認。
  ダッシュボードは、次の実行 (stalk) で、Supabase から作り直されるので、消した更新は、一覧・更新歴・検索・Atom から無くなる。
  SQL は ASCII のみ (Unicode エスケープ、ハッシュは 16 進だけ許可) で、一時ファイルに書いて -f で渡す。
  supabase.exe があればそれを、なければ npx を使う。

.PARAMETER CountOnly
  件数と対象のコミットを表示するだけで、何も変更しない。

.PARAMETER Commit
  消す更新の commit (先頭 7 文字以上の 16 進数。複数は、カンマか空白で区切る)。

.PARAMETER Contains
  差分に含まれる文字列 (固定の文字列。正規表現ではない)。日本語を含むときは、pwsh 7 で実行する
  (Windows PowerShell 5.1 は、コマンドの引数の日本語を、文字化けさせる)。

.PARAMETER Site
  -Contains の探す範囲を、1 つのサイト (slug) に絞る。

.PARAMETER Kind
  -Commit / -Contains で見つかった更新のうち、消すものの種類。
    noop     AI が「内容に実質的な変更はありません」と要約したもの (-Contains の既定。実際の変更を、巻き込まない)
    any      すべて (-Commit の既定。commit を名指しした以上、要約が何でも消す)
    initial  記録を開始した最初の行 / failed  要約に失敗した行 / other  ふつうの要約がある行
  -Contains は、最初の取り込み (サイトのページ全体が「追加」になる) や、文字列を含む本物の更新も、見つけてしまうので、
  既定は noop にしてある。広げるときは、表示される [noop] [initial] [other] の目印を見て、決める。

.PARAMETER DataDir
  g-i-t-data の作業ツリー。指定すると、対応する差分ページ (public/sites/<slug>/diff_<hash7>.html) も消す。
  -Contains では、git で commit を探すためにも使う。

.PARAMETER Yes
  確認の入力を省略する。

.EXAMPLE
  pwsh .\scripts\purge-noop-updates.ps1 -CountOnly -DataDir ..\g-i-t-data
  pwsh .\scripts\purge-noop-updates.ps1 -DataDir ..\g-i-t-data
  pwsh .\scripts\purge-noop-updates.ps1 -Commit 'ee536d4,67036a2,7ac47c5,cb06f1b' -DataDir ..\g-i-t-data
  pwsh .\scripts\purge-noop-updates.ps1 -Contains '?var=' -Site anesth -CountOnly -DataDir ..\g-i-t-data
  pwsh .\scripts\purge-noop-updates.ps1 -Contains '?var=' -Site anesth -Kind any -DataDir ..\g-i-t-data
#>
[CmdletBinding()]
param(
    [switch]$CountOnly,
    [string[]]$Commit,
    [string]$Contains,
    [string]$Site,
    [ValidateSet('any', 'noop', 'failed', 'initial', 'other')][string]$Kind,
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
# 要約の種類 (ASCII の目印)。日本語の文言は、Unicode エスケープで書く。
$KindSql = "case when summary like $Like then 'noop' when summary = U&'\8A18\9332\3092\958B\59CB\3057\307E\3057\305F' then 'initial' " +
           "when summary like U&'\8981\7D04\3092\751F\6210\3067\304D\307E\305B\3093\3067\3057\305F%' then 'failed' else 'other' end"

# 対象の行を選ぶ SQL の条件 ($Where は updates の別名なしの列名で書く)
# cmd.exe は、単一引用符 ' を引用符として扱わない (値に、そのまま、入ってくる)。前後の引用符・バッククォートは、取り除く。
$QuoteChars = [char[]]@([char]39, [char]34, [char]96)
if ($Contains) {
    $Contains = $Contains.Trim()
    if ($Contains.Length -ge 2 -and $Contains.StartsWith("'") -and $Contains.EndsWith("'")) { $Contains = $Contains.Substring(1, $Contains.Length - 2) }
}
if ($Site) { $Site = $Site.Trim().Trim($QuoteChars) }

$Hashes = @()
if ($Commit) {
    $Hashes = @($Commit | ForEach-Object { $_ -split '[,\s]+' } | ForEach-Object { $_.Trim().Trim($QuoteChars).ToLower() } | Where-Object { $_ } | Select-Object -Unique)
    foreach ($h in $Hashes) {
        if ($h -notmatch '^[0-9a-f]{7,40}$') { Write-Host "エラー: commit は 7〜40 桁の 16 進数で指定してください: '$h'" -ForegroundColor Red; exit 1 }
    }
    if (-not $Hashes.Count) { Write-Host 'エラー: -Commit に値がありません。' -ForegroundColor Red; exit 1 }
}

# -Contains: git で、差分に文字列を含む commit を探す (Supabase に更新として記録されているものだけが、あとで対象になる)
$GitFound = $null
if ($Contains) {
    if (-not $DataDir) { Write-Host 'エラー: -Contains には -DataDir (g-i-t-data の作業ツリー) が必要です。' -ForegroundColor Red; exit 1 }
    if (-not (Test-Path -LiteralPath (Join-Path $DataDir '.git'))) { Write-Host "エラー: g-i-t-data の作業ツリーではありません: $DataDir" -ForegroundColor Red; exit 1 }
    if ($PSVersionTable.PSVersion.Major -lt 6 -and $Contains -match '[^\x00-\x7F]') {
        Write-Host 'エラー: Windows PowerShell 5.1 は、コマンドの引数の日本語を文字化けさせます。pwsh 7 で実行するか、ASCII の文字列を指定してください。' -ForegroundColor Red; exit 1
    }
    if ($Site -and $Site -notmatch '^[a-z0-9][a-z0-9_-]*$') { Write-Host "エラー: -Site は slug (小文字の英数字と - _) で指定してください: '$Site'" -ForegroundColor Red; exit 1 }
    $path = if ($Site) { "sites/$Site" } else { 'sites' }
    $regex = [regex]::Escape($Contains)               # 固定の文字列として探す (正規表現の記号は、すべて、エスケープ)
    $regex = $regex -replace '\\ ', ' ' -replace '\\#', '#'   # git の ERE では、\ の後ろの空白と # は、そのままで良い
    $found = & git -C $DataDir -c core.quotepath=false log --format=%H "-G$regex" -- $path 2>$null
    $GitFound = @($found | Where-Object { $_ -match '^[0-9a-f]{40}$' })
    Write-Host "git の差分に '$Contains' を含む commit: $($GitFound.Count) 件 ($path)"
    if ($Hashes.Count) { $Hashes = @($Hashes | Where-Object { $h = $_; $GitFound | Where-Object { $_.StartsWith($h) } }) }   # -Commit との共通部分
    else { $Hashes = $GitFound }
    if (-not $Hashes.Count) { Write-Host '該当する commit は、ありません。' -ForegroundColor Green; exit 0 }
    if ($Hashes.Count -gt 800) { Write-Host "エラー: commit が $($Hashes.Count) 件と多すぎます。-Site で絞るか、より具体的な文字列にしてください (上限 800)。" -ForegroundColor Red; exit 1 }
}

if (-not $Kind) { $Kind = if ($Contains) { 'noop' } else { 'any' } }
if ($Hashes.Count) {
    $Where = '(' + (($Hashes | ForEach-Object { "commit_hash like '$_%'" }) -join ' or ') + ')'
    if ($Kind -ne 'any') { $Where += " and ($KindSql) = '$Kind'" }
} else {
    $Where = "summary like $Like"
}
# archive_queue 側 (updates u と結ぶ) の条件
$WhereU = $Where -replace 'commit_hash', 'u.commit_hash' -replace '(?<![.\w])summary', 'u.summary'

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
    $sql = "select 'COUNTS ' || (select count(*) from updates where $Where) || ' ' || " +
           "(select count(*) from archive_queue q where exists (select 1 from updates u where $WhereU " +
           "and u.site_slug = q.site_slug and u.commit_hash = q.commit_hash)) || ' ' || (select count(*) from updates) as c;"
    $out = Invoke-Sql $sql
    if ($out -notmatch 'COUNTS (\d+) (\d+) (\d+)') { throw "件数を読み取れませんでした。出力の先頭:`n$(($out -split "`n" | Select-Object -First 15) -join "`n")" }
    return [pscustomobject]@{ Updates = [int]$Matches[1]; Queue = [int]$Matches[2]; Total = [int]$Matches[3] }
}

# 対象の行: site / 7 桁のハッシュ / 日付 / 要約の種類 (ASCII の目印。日本語は、文字コードの問題を避けるため、出さない)
function Get-Rows {
    $sql = "select 'ROW ' || site_slug || ' ' || commit_hash || ' ' || to_char(created_at at time zone 'Asia/Tokyo', 'YYYY-MM-DD_HH24:MI') || ' ' || ($KindSql) as c " +
           "from updates where $Where order by site_slug, id;"
    $out = Invoke-Sql $sql
    return @([regex]::Matches($out, 'ROW (\S+) ([0-9a-f]{7,64}) (\S+) (\w+)') | ForEach-Object {
        [pscustomobject]@{ Slug = $_.Groups[1].Value; Full = $_.Groups[2].Value; Hash = $_.Groups[2].Value.Substring(0, 7); When = $_.Groups[3].Value; Kind = $_.Groups[4].Value }
    })
}

try {
    $before = Get-Counts
    $mode = if ($Contains) { "差分に '$Contains' を含む commit" + $(if ($Site) { " (サイト $Site)" } else { '' }) + ", git で $($Hashes.Count) 件" }
            elseif ($Hashes.Count) { "commit 指定 ($($Hashes -join ', '))" }
            else { '要約が「実質的な変更なし」の更新' }
    if ($Hashes.Count) { $mode += "、種類: $Kind" }
    Write-Host "対象: $mode"
    Write-Host "削除の対象: updates $($before.Updates) 行 / archive_queue $($before.Queue) 行  (updates 全体 $($before.Total) 行)"

    $rows = Get-Rows
    if ($rows.Count -ne $before.Updates) { throw "対象の一覧 ($($rows.Count) 件) が、件数 ($($before.Updates) 件) と合いません。" }

    if ($Hashes.Count) {
        # 指定ごとに、当たった行数を確かめる (0 件は警告、2 行以上は曖昧なので止める)。-Contains の commit は、更新として
        # 記録されていないもの (Re-baseline / Reorder / 初回以外の取り込みなど) が多いので、0 件は、いちいち警告しない。
        foreach ($h in $Hashes) {
            $hit = @($rows | Where-Object { $_.Full.StartsWith($h) })
            if ($hit.Count -eq 0 -and -not $Contains) { Write-Host "警告: $h に当たる更新は、ありません。" -ForegroundColor Yellow }
            elseif ($hit.Count -gt 1) { throw "$h は $($hit.Count) 行に当たります (曖昧)。もっと長いハッシュを指定してください。" }
        }
        if ($Contains) { Write-Host "  git で見つかった $($Hashes.Count) 件の commit のうち、Supabase に更新として記録されていて、種類が $Kind のものは $($rows.Count) 件です。" }
        foreach ($r in $rows) { Write-Host ("  {0,-26} {1}  {2}  [{3}]" -f $r.Slug, $r.Hash, $r.When, $r.Kind) }
        Write-Host '  ([noop] = 実質的な変更なし / [initial] = 記録開始 / [failed] = 要約の失敗 / [other] = 通常の要約)'
    } elseif ($rows.Count) {
        Write-Host "サイト別: " -NoNewline
        Write-Host (($rows | Group-Object Slug | Sort-Object Count -Descending | ForEach-Object { "$($_.Name)=$($_.Count)" }) -join ', ')
    }
    if ($before.Updates -eq 0) { Write-Host '対象はありません。' -ForegroundColor Green; exit 0 }

    $files = @()
    if ($DataDir) {
        if (-not (Test-Path -LiteralPath $DataDir)) { throw "DataDir が存在しません: $DataDir" }
        $DataDir = (Resolve-Path -LiteralPath $DataDir).Path
        $files = @($rows | ForEach-Object { Join-Path $DataDir "public\sites\$($_.Slug)\diff_$($_.Hash).html" } | Where-Object { Test-Path -LiteralPath $_ })
        Write-Host "消す差分ページ: $($files.Count) 件 ($DataDir\public\sites\<slug>\diff_<hash7>.html)"
    }
    if ($CountOnly) { Write-Host '(-CountOnly: 変更しません)'; exit 0 }

    if (-not $Yes) {
        $answer = Read-Host "Supabase の updates $($before.Updates) 行と archive_queue $($before.Queue) 行を削除します (元に戻せません)。続けますか? (yes/no)"
        if ($answer -ne 'yes') { Write-Host '中止しました。変更していません。'; exit 0 }
    }

    # archive_queue を先に (updates の行を、結び付けの鍵として使うため)
    $del = "delete from archive_queue q using updates u where $WhereU and u.site_slug = q.site_slug and u.commit_hash = q.commit_hash; " +
           "delete from updates where $Where;"
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
        Write-Host "  git -C `"$DataDir`" commit -m `"Remove diff pages of deleted updates`""
        Write-Host "  git -C `"$DataDir`" push"
    }
    Write-Host '完了しました。ダッシュボードは、次の実行で、Supabase から作り直されます (stalk.yml / コンテナを、手動で動かすと、すぐ反映されます)。' -ForegroundColor Green
} catch {
    Write-Host "エラー: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
