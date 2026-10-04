# 設定リファレンス

監視対象は、`g-i-t-data` の **`config.yaml`** で管理します。このファイルが正（Single Source of Truth）です。

> **反映されるタイミング:** 設定は、**GitHub 上の `g-i-t-data`** を次回の実行が読んだときに反映されます。手元で編集したら、`git push` してからワークフローを実行してください。

## config.yaml

```yaml
sites:
  - url: https://www.naika.or.jp/
    name: "日本内科学会"
    slug: "naika"
    tags: ["学会", "内科"]
    ignore:
      - ';jsessionid=[0-9A-Fa-f]+'
```

| キー | 必須 | 内容 |
|---|---|---|
| `url` | ○ | 監視するページの URL |
| `name` | | 表示名。日本語でよい（UTF-8、BOM なし）。省略するとホスト名を表示する。パスやコミットには使わない |
| `slug` | | ID。**`[a-z0-9][a-z0-9_-]*` のみ**（ASCII の小文字・数字・`-`・`_`）。ディレクトリ名と URL になる。省略すると URL から自動生成する。違反するとそのサイトは作られず、ログに警告が出る |
| `tags` | | 分類のリスト（文字列、`\|` は不可）。絞り込みと、分類別の Atom フィードに使う |
| `ignore` | | 取得したページから消す（毎回変わる）文字列の正規表現のリスト |
| その他 | | `website-stalker.yaml` のサイト項目（`editors`、`headers` など）は、そのまま渡される |

`name` / `slug` / `tags` / `ignore` は G医t 独自の項目で、`website-stalker.yaml` には**書き出されません**（website-stalker は未知のキーを拒否するため）。

## 標準の取得設定

新しいサイトには、次の `website-stalker.yaml` が作られます（`sites/<slug>/website-stalker.yaml`）。

```yaml
sites:
- url: <url>
  headers:
  - 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
  editors:
  - css_select: body
  - css_remove: script, style, noscript, iframe
  - html_sanitize
  - html_prettify
```

- `editors` や `headers` を `config.yaml` のサイトに書くと、**標準の値を丸ごと置き換えます**（足し算ではありません）。標準を残して足したいときは、標準の項目も書き直してください。
- `css_select: body` は、一致する要素が無いとエラーになります（そのサイトの取得だけが失敗します）。

## ignore（毎回変わる文字列を消す）

セッションIDのように、内容と関係なく毎回変わる文字列は、差分を生み続けます。`ignore` に正規表現を書くと、保存・比較の前にその部分が消えます。

```yaml
    ignore:
      - ';jsessionid=[0-9A-Fa-f]+'                                   # 消す
      - { pattern: 'token=[0-9a-f]+', replace: 'token=X' }          # 置き換える
```

- 実体は `regex_replace` エディタで、`html_sanitize` の前に挿入されます。
- 正規表現は Rust の `regex` です。**先読み（`(?=`）・後方参照（`\1`）は使えません**（書くと警告を出してそのルールだけ飛ばします）。
- YAML では**シングルクォート**で囲むと、`\` をそのまま書けます。
- **既存サイトに追加した場合**は、次回の実行時に `sites/<slug>/website-stalker.yaml` へ追記されます（`Update ignore rules for <slug>` というコミット）。他の部分は変わりません。その回だけ、保存済みの内容との差で、1回の「更新」が記録されます。
- `config.yaml` から消しても、`website-stalker.yaml` からは自動では消えません。外すときは、手でそのファイルを編集するか、[リセット](Operations)してください。

## GitHub Secrets（g-i-t-app）

| 名前 | 内容 |
|---|---|
| `GH_PAT` | `g-i-t-data` への読み書き（fine-grained の `Contents: Read and write` で足りる）。ダッシュボードの公開にも使う |
| `GEMINI_API_KEY` | Gemini API のキー |
| `SUPABASE_URL` | Supabase のプロジェクト URL（`https://<REF>.supabase.co`、末尾に `/` を付けない） |
| `SUPABASE_KEY` | Supabase の Secret key（`sb_secret_...`）。サーバー側専用。公開しない |
| `WEBSITE_STALKER_FROM` | 連絡先メールアドレス。監視先サイトへの HTTP `From` ヘッダーとして送られる（`@` と `.` を含むこと） |
| `IA_ACCESS_KEY` / `IA_SECRET_KEY` | Internet Archive の S3 キー（`archive.yml` のみ使用） |

```powershell
gh secret set SUPABASE_KEY --repo TanukiMa/g-i-t-app     # 値は対話入力（履歴に残さない）
gh secret list --repo TanukiMa/g-i-t-app
```

## 環境変数（任意）

| 名前 | 既定 | 内容 |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.8-flash` | 要約に使うモデル。提供が終了したら変更する |
| `SUMMARY_BACKFILL_LIMIT` | `10` | 1回の実行で作り直す、失敗した要約の最大件数（`0` で無効） |
| `ARCHIVE_BATCH_SIZE` | `20` | 1回のアーカイブ処理で保存する URL の最大件数 |
| `ARCHIVE_INTERVAL_SEC` | `15` | 保存の間隔（秒） |
| `SITE_BASE_URL` | `https://tanukima.github.io/g-i-t-data/` | Atom フィード内の絶対 URL の基準 |
