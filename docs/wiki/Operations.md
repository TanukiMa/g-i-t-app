# 運用手順

コマンドは PowerShell で、`g-i-t-app` のディレクトリから実行する想定です。

## サイトを追加・変更する
1. `g-i-t-data` の `config.yaml` に追記します（書き方は [設定リファレンス](Configuration)）。
2. **`git push` します。**（ワークフローは GitHub 上の内容を読みます。手元のコミットだけでは反映されません。）
3. 実行します。
   ```powershell
   gh workflow run stalk.yml --repo TanukiMa/g-i-t-app
   gh run watch --repo TanukiMa/g-i-t-app
   ```

> 手元の `g-i-t-data` を編集する前に `git pull` してください。ワークフローが `main` に push する（`Update <slug>` や `Snapshot`）ため、手元が古いと、マージが必要になります。

追加したサイトの最初の実行は「初回取得」になり、AI要約は付きません。2回目以降から、変化が要約されます。

## 設定ファイルを検査する（commit の前）

`config.yaml` の書き間違い（全角の引用符、スラッグの規則違反・重複、使えない正規表現、不正な CSS セレクターなど）は、実行してから気づくと、新しいサイトの追加や、ダッシュボードの作成が、止まります。commit する前に検査してください。

```powershell
cd g-i-t-app
python scripts\check_config.py --data-dir ..\g-i-t-data        # 作業ツリーの config.yaml
pwsh .\scripts\install-config-hook.ps1                         # commit のたびに、自動で検査する（pre-commit フック）
```
- エラーは commit を止め、警告（綴りの間違いらしいキー、同じ URL の重複など）は表示だけです。1 回だけ飛ばすときは `git commit --no-verify`。
- フックは `g-i-t-data` の `.git/hooks` に入ります（clone し直したら、もう一度）。外すときは `-Uninstall`。
- それでも壊れた設定が push されたときは、実行は、**直前に読めた版の `config.yaml` で続き**、ログに行と原因を出して、最後に失敗として終わります。直して push してください。

## 手動で実行する
```powershell
gh workflow run stalk.yml   --repo TanukiMa/g-i-t-app   # 本処理（取得 → 公開）
gh workflow run archive.yml --repo TanukiMa/g-i-t-app   # アーカイブ保存だけ
gh run list  --repo TanukiMa/g-i-t-app --limit 5
gh run view <ID> --repo TanukiMa/g-i-t-app --log        # ログ
```
ログでは `Command failed` / `Error` / `WARNING` を探してください。個別の失敗は、全体を止めずにログに残る作りです（ワークフローは緑でも、中で失敗していることがあります）。

## Supabase の準備と確認
```powershell
supabase db query --linked -f sql\schema.sql                     # スキーマ適用（再実行しても安全）
supabase db query --linked "select table_name from information_schema.tables where table_schema='public';"
```
`updates` と `archive_queue` の2つが出れば準備できています。

## リセット（履歴とデータを消して、設定だけ残す）
```powershell
.\scripts\reset-data.ps1 -DryRun     # 何も変えずに、実行内容だけを表示
.\scripts\reset-data.ps1             # リポジトリ名の入力を求められる
```
流れ：事前チェック → バックアップ（mirror clone）→ ワークフローを無効化 → `g-i-t-data` の履歴を作り直して force push（`config.yaml` / `README.md` / `.gitignore` だけを残す）→ Supabase の2テーブルを空にする（**空になったことを件数で確認し、残っていれば停止**します。リンク先のプロジェクトも表示されるので、`SUPABASE_URL` のものと同じか確認してください）→ ワークフローを有効化（`-Run` で実行まで）。

- 未コミットの変更があると止まります。先にコミットか破棄をしてください。
- 途中で失敗したときは、ワークフローを**無効のまま**にします。状態を確認してから `gh workflow enable stalk.yml --repo TanukiMa/g-i-t-app`（`archive.yml` も同様）で戻してください。
- Internet Archive に保存済みのものは消えません。

## 図を直す
`docs/system.d2` を編集して `pwsh .\scripts\render-diagram.ps1` を実行し、`static/system.svg` をコミットします（`d2` コマンドが必要）。

## 困ったとき

| 症状 | 原因と対処 |
|---|---|
| 表示名（`name`）が更新されない | 手元でコミットしただけで、push していない。`git push` してから再実行する |
| `AI要約を生成できませんでした。` | Gemini の一時的な失敗（503 など）や、無料枠の**1日の上限（RPD）**。自動で再試行し、次のモデルに移り、以降の実行で作り直される。上限は太平洋時間の0時（日本時間の16時または17時）にリセットされる。続くなら、`GEMINI_MODELS` にモデルを足すか、別プロバイダー（`LLM_FALLBACK_*`）を設定する。`404` はモデルの提供終了なので、`GEMINI_MODELS` から外す |
| 毎回、リンクなどの同じ種類の差分が出る | セッションIDなど。`ignore` で消す（[設定リファレンス](Configuration)）。WordPress の `file.pdf?数値` 形式は標準ルールで自動的に消える |
| `PGRST205: Could not find the table` | Supabase の URL / KEY が別プロジェクトのもの、またはスキーマ未適用。`SUPABASE_URL` / `SUPABASE_KEY` を設定し直し、`sql/schema.sql` を適用する |
| `website-stalker` が `from ... is invalid` | `WEBSITE_STALKER_FROM` が未設定、または `@` と `.` を含まない |
| `config.yaml cannot be read (line N, column M)` | `config.yaml` の YAML が壊れている（全角の引用符 `”` など）。上の「設定ファイルを検査する」で行を確認して直す。直るまでは、前回読めた版で動く |
| `WARNING: skipping ...: slug ...` | `slug` が `[a-z0-9][a-z0-9_-]*` ではない。直す |
| `Node.js 20 is deprecated` の警告 | 古いアクションのバージョン。`actions/*` を最新のメジャーに上げる |
| 700MB 超の警告（ログの `.git size`） | サイズが大きい。変化の多いサイトに `css_select` / `css_remove` / `ignore` を足してノイズを減らす |
| リセット後も、古い更新（存在しないコミット）がダッシュボードに出る | ダッシュボードは **Supabase の行**から作られる。Git は空になっても、`updates` / `archive_queue` に古い行が残っている。件数を確認し、リセット時刻より前の行を消して再実行する（下記） |
| 更新したはずなのに、古い画面が出る（アプリ／ブラウザ） | ページはネットワーク優先なので、通常は最新が出る。出ない場合は、ブラウザの開発者ツール → Application → Service Workers で「Unregister」と「Clear site data」を実行する |
| リセット後にワークフローが動かない | 失敗した場合、無効のまま。`gh workflow enable` で戻す |
| Wayback が「保存待ち」のまま（`archive_queue` が `pending` で `archive_url` が空） | `pending` の行は、次の実行で再試行される（失敗は 30 分から倍々の間隔で最大5回、そのあと `failed`）。ただし GitHub のスケジュールは間引かれることがあり、実行の間隔が数時間になる。実行の履歴と、下のクエリで状況を見る。急ぐときは `gh workflow run archive.yml` |

### リセット後も古い更新が表示されるとき

```powershell
# 1) 残っている行を確認（oldest がリセット前なら、消えていない）
supabase db query --linked "select count(*) as n, min(created_at) as oldest, max(created_at) as newest from updates;"
supabase db query --linked "select count(*) as n, min(created_at) as oldest from archive_queue;"

# 2) リセット時刻（g-i-t-data の最初のコミットの時刻。UTC）より前の行だけを消す
git -C ..\g-i-t-data log --reverse --format=%aI -1      # 例: 2026-10-04T23:59:14+09:00 → UTC では 14:59:14Z
supabase db query --linked "delete from archive_queue where created_at < '2026-10-04T14:59:14Z';"
supabase db query --linked "delete from updates       where created_at < '2026-10-04T14:59:14Z';"

# 3) ダッシュボードを作り直す
gh workflow run stalk.yml --repo TanukiMa/g-i-t-app
```
全部消してよければ、`truncate updates, archive_queue restart identity;` でも構いません。

### アーカイブの待ちの状況を見る

```powershell
# 状態別の件数と、いちばん古い行
supabase db query --linked "select status, count(*) as n, min(created_at) as oldest, max(attempts) as max_attempts from archive_queue group by status order by status;"

# 保留中の行（なぜ終わっていないか）
supabase db query --linked "select left(url, 70) as url, attempts, next_try_at, left(last_error, 60) as last_error from archive_queue where status = 'pending' order by created_at limit 20;"

# 実行の間隔（スケジュールが間引かれていないか）
gh run list --repo TanukiMa/g-i-t-app --workflow archive.yml --limit 15
```
- `attempts = 0` のままの行は、まだ着手されていない（順番待ち）。
- `last_error` が `Unauthorized` なら、`IA_ACCESS_KEY` / `IA_SECRET_KEY` を確認する。
- `failed` になった行を再試行したいときは、`update archive_queue set status = 'pending', attempts = 0, next_try_at = now() where status = 'failed';`
