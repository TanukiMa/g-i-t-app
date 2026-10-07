<#
.SYNOPSIS
  Supabase の updates.summary に残っている古い「初回取得」の文言を、現在の文言に書き換える。

.DESCRIPTION
  古い: 初回取得: 監視を開始しました。次回以降の更新が差分として記録されます。
  新しい: 記録を開始しました   (scripts/common.py の SUMMARY_INITIAL)

  1. 件数を数えて表示する (古い文言 / 新しい文言 / 全体)。
  2. 「yes」と入力されたときだけ、UPDATE を実行する。
  3. もう一度数えて、古い文言が 0 件になり、新しい文言が増えたぶんだけ増えたことを確認する。

  ダッシュボードは古い文言も同じ初回取得として扱うので、この書き換えは必須ではない (データをそろえるためのもの)。
  SQL は ASCII のみ (Unicode エスケープ) で送るので、コンソールの文字コードに左右されない。
  事前に: supabase link 済みで、`supabase db query --linked` が使えること。

.PARAMETER CountOnly
  件数を表示するだけで、何も変更しない。

.PARAMETER Yes
  確認の入力を省略する (自動実行用)。

.EXAMPLE
  pwsh .\scripts\fix-initial-summary.ps1 -CountOnly
  pwsh .\scripts\fix-initial-summary.ps1
#>
[CmdletBinding()]
param(
    [switch]$CountOnly,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path -Parent $PSScriptRoot

# Windows PowerShell 5.1 でも、このファイルの文字列を正しく読むため、スクリプトは BOM 付き UTF-8 で保存してある。
$OldText = '初回取得: 監視を開始しました。次回以降の更新が差分として記録されます。'
$NewText = '記録を開始しました'

# 文字コードに依存しない、ASCII だけの文字列リテラル (Postgres の Unicode エスケープ) に変換する。
function ConvertTo-PgUnicode([string]$Text) {
    $sb = New-Object System.Text.StringBuilder
    foreach ($ch in $Text.ToCharArray()) { [void]$sb.AppendFormat('\{0:X4}', [int]$ch) }
    return "U&'" + $sb.ToString() + "'"
}
$OldLit = ConvertTo-PgUnicode $OldText
$NewLit = ConvertTo-PgUnicode $NewText

# supabase の起動方法を決める。supabase.exe があればそれを使う。なければ (npm の shim や関数だと引数が落ちることがあるので)
# shim を通さずに npx で直接呼ぶ。
function Get-SupabaseCommand {
    $app = Get-Command supabase -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($app -and $app.Source -match '\.exe$') { return @{ Exe = $app.Source; Pre = @() } }
    if (Get-Command npx -ErrorAction SilentlyContinue) { return @{ Exe = 'npx'; Pre = @('--yes', 'supabase') } }
    if ($app) { return @{ Exe = $app.Source; Pre = @() } }
    throw 'supabase コマンドも npx も見つかりません。'
}

function Invoke-Sql([string]$Sql) {
    # SQL は ASCII だけなので、一時ファイルに書いて -f で渡す。引数で渡すと、&・|・' などが cmd/npx の経由で壊れることがある。
    $file = Join-Path ([IO.Path]::GetTempPath()) ("fix-initial-summary-{0}.sql" -f [guid]::NewGuid().ToString('N'))
    [IO.File]::WriteAllText($file, $Sql, (New-Object System.Text.UTF8Encoding($false)))
    $cli = Get-SupabaseCommand
    $cliArgs = @($cli.Pre) + @('db', 'query', '--linked', '-f', $file)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'   # supabase は進捗を stderr に出す。5.1 で例外化させない
    $env:npm_config_yes = 'true'          # npx 経由のとき、「Ok to proceed?」で止まらないようにする
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
    $sql = "select 'COUNTS ' || (select count(*) from updates where summary = $OldLit) || ' ' || " +
           "(select count(*) from updates where summary = $NewLit) || ' ' || (select count(*) from updates) as c;"
    $out = Invoke-Sql $sql
    if ($out -notmatch 'COUNTS (\d+) (\d+) (\d+)') { $c = Get-SupabaseCommand; throw "件数を読み取れませんでした (使用: $($c.Exe) $($c.Pre -join ' '))。出力の先頭:`n$(($out -split "`n" | Select-Object -First 15) -join "`n")" }
    return [pscustomobject]@{ Old = [int]$Matches[1]; New = [int]$Matches[2]; Total = [int]$Matches[3] }
}

try {
    $cli = Get-SupabaseCommand
    Write-Host "使用するコマンド: $($cli.Exe) $($cli.Pre -join ' ')" -ForegroundColor DarkGray

    $before = Get-Counts
    Write-Host "古い文言の行  : $($before.Old)"
    Write-Host "新しい文言の行: $($before.New)"
    Write-Host "updates 全体  : $($before.Total)"
    if ($before.Old -eq 0) { Write-Host '書き換える行はありません。' -ForegroundColor Green; exit 0 }
    if ($CountOnly) { Write-Host '(-CountOnly: 変更しません)'; exit 0 }

    if (-not $Yes) {
        $answer = Read-Host "古い文言の $($before.Old) 行を『$NewText』に書き換えます。続けますか? (yes/no)"
        if ($answer -ne 'yes') { Write-Host '中止しました。変更していません。'; exit 0 }
    }

    Invoke-Sql "update updates set summary = $NewLit where summary = $OldLit;" | Out-Null

    $after = Get-Counts
    Write-Host "書き換え後 -> 古い: $($after.Old) / 新しい: $($after.New) / 全体: $($after.Total)"
    if ($after.Old -ne 0 -or $after.New -ne ($before.New + $before.Old) -or $after.Total -ne $before.Total) {
        throw '件数が想定と合いません。Supabase の内容を確認してください。'
    }
    Write-Host '完了しました。' -ForegroundColor Green
} catch {
    Write-Host "エラー: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
