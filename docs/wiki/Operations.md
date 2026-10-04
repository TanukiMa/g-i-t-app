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
流れ：事前チェック → バックアップ（mirror clone）→ ワークフローを無効化 → `g-i-t-data` の履歴を作り直して force push（`config.yaml` / `README.md` / `.gitignore` だけを残す）→ Supabase の2テーブルを空にする → ワークフローを有効化（`-Run` で実行まで）。

- 未コミットの変更があると止まります。先にコミットか破棄をしてください。
- 途中で失敗したときは、ワークフローを**無効のまま**にします。状態を確認してから `gh workflow enable stalk.yml --repo TanukiMa/g-i-t-app`（`archive.yml` も同様）で戻してください。
- Internet Archive に保存済みのものは消えません。

## 図を直す
`docs/system.d2` を編集して `pwsh .\scripts\render-diagram.ps1` を実行し、`static/system.svg` をコミットします（`d2` コマンドが必要）。

## 困ったとき

| 症状 | 原因と対処 |
|---|---|
| 表示名（`name`）が更新されない | 手元でコミットしただけで、push していない。`git push` してから再実行する |
| `AI要約を生成できませんでした。` | Gemini の一時的な失敗（503 など）。自動で再試行し、以降の実行で作り直される。`404` なら、モデルの提供終了なので `GEMINI_MODEL` を変える |
| 毎回、リンクなどの同じ種類の差分が出る | セッションIDなど。`ignore` で消す（[設定リファレンス](Configuration)） |
| `PGRST205: Could not find the table` | Supabase の URL / KEY が別プロジェクトのもの、またはスキーマ未適用。`SUPABASE_URL` / `SUPABASE_KEY` を設定し直し、`sql/schema.sql` を適用する |
| `website-stalker` が `from ... is invalid` | `WEBSITE_STALKER_FROM` が未設定、または `@` と `.` を含まない |
| `WARNING: skipping ...: slug ...` | `slug` が `[a-z0-9][a-z0-9_-]*` ではない。直す |
| `Node.js 20 is deprecated` の警告 | 古いアクションのバージョン。`actions/*` を最新のメジャーに上げる |
| 700MB 超の警告（ログの `.git size`） | サイズが大きい。変化の多いサイトに `css_select` / `css_remove` / `ignore` を足してノイズを減らす |
| リセット後にワークフローが動かない | 失敗した場合、無効のまま。`gh workflow enable` で戻す |
| 更新が多いサイトの Wayback が「保存待ち」のまま | `archive.yml` が1回に20件・15秒間隔で処理している。時間が経てば進む。急ぐなら `ARCHIVE_BATCH_SIZE` を増やす |
