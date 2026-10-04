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
| `WARNING: skipping ...: slug ...` | `slug` が `[a-z0-9][a-z0-9_-]*` ではない。直す |
| `Node.js 20 is deprecated` の警告 | 古いアクションのバージョン。`actions/*` を最新のメジャーに上げる |
| 700MB 超の警告（ログの `.git size`） | サイズが大きい。変化の多いサイトに `css_select` / `css_remove` / `ignore` を足してノイズを減らす |
| リセット後も、古い更新（存在しないコミット）がダッシュボードに出る | ダッシュボードは **Supabase の行**から作られる。Git は空になっても、`updates` / `archive_queue` に古い行が残っている。件数を確認し、リセット時刻より前の行を消して再実行する（下記） |
| 更新したはずなのに、古い画面が出る（アプリ／ブラウザ） | ページはネットワーク優先なので、通常は最新が出る。出ない場合は、ブラウザの開発者ツール → Application → Service Workers で「Unregister」と「Clear site data」を実行する |
| リセット後にワークフローが動かない | 失敗した場合、無効のまま。`gh workflow enable` で戻す |
| 更新が多いサイトの Wayback が「保存待ち」のまま | `archive.yml` が1回に20件・15秒間隔で処理している。時間が経てば進む。急ぐなら `ARCHIVE_BATCH_SIZE` を増やす |

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
