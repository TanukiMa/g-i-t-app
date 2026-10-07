# 設定リファレンス

確認先のサイトは、`g-i-t-data` の **`config.yaml`** で管理します。このファイルが正（Single Source of Truth）です。

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
| `url` | ○ | 確認するページの URL |
| `name` | | 表示名。日本語でよい（UTF-8、BOM なし）。省略するとホスト名を表示する。パスやコミットには使わない |
| `slug` | | ID。**`[a-z0-9][a-z0-9_-]*` のみ**（ASCII の小文字・数字・`-`・`_`）。ディレクトリ名と URL になる。省略すると URL から自動生成する。違反するとそのサイトは作られず、ログに警告が出る |
| `tags` | | 分類のリスト（文字列、`\|` は不可）。絞り込みと、分類別の Atom フィードに使う |
| `ignore` | | 取得したページから消す（毎回変わる）文字列の正規表現のリスト |
| `default_ignore` | | `false` にすると、**標準の除去ルール**（下記）をこのサイトに適用しない |
| その他 | | `website-stalker.yaml` のサイト項目（`editors`、`headers` など）は、そのまま渡される |

`name` / `slug` / `tags` / `ignore` / `default_ignore` は G医t 独自の項目で、`website-stalker.yaml` には**書き出されません**（website-stalker は未知のキーを拒否するため）。

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

### 標準の除去ルール（全サイトに自動で適用）

WordPress などは、静的ファイルへのリンクに、アクセスのたびに変わる数値を付けます（キャッシュ回避）。これが「更新」と誤検知される原因になるため、**次の形だけ**を、全サイトで自動的に消します。

| 変更前 | 変更後 |
|---|---|
| `…/guide.pdf?1700000001` | `…/guide.pdf` |
| `…/logo.png?ver=6.4.2` | `…/logo.png` |
| `…/a.docx?t=1700000001` | `…/a.docx` |
| `…/report.pdf?download=1`、`…/page.php?id=5` | **そのまま**（意味のあるクエリは消さない） |

- 対象の拡張子：`pdf` `doc(x)` `xls(x)` `ppt(x)` `zip` `png` `jpg/jpeg` `gif` `webp` `svg` `ico` `css` `js`
- 対象のクエリ：数字・バージョン（`6.4.2`）だけ、または `ver` `v` `t` `ts` `time` `timestamp` `rev` `cb` の値が数字・バージョンのもの
- 既存のサイトにも、次回の実行で追記されます。**追記した回の変化は「更新」として記録されません**（`Re-baseline <slug> after ignore rule change` というコミットで、基準を取り直すだけ）。
- 効かせたくないサイトは、`default_ignore: false` を書きます。

**もう1つ：Cloudflare の「メールアドレス保護」。** Cloudflare 配下のサイトは、メールアドレスのリンクを暗号化し、**アクセスのたびに違う鍵**で作り直します（`/cdn-cgi/l/email-protection#a5cc…`、`<span data-cfemail="a4cd…">`）。内容が同じでも毎回変わるため、鍵の部分（16進数）だけを消します（`/cdn-cgi/l/email-protection` と `data-cfemail=""` が残る）。`title=""` などに平文のアドレスが残っていれば、その変更は検知されます。暗号化された形だけのときは、アドレス自体の変更は検知されません。

**もう1つ：期限付きの「NEW」バッジ。** 「NEW」「New!」「ＮＥＷ」「新着」だけを中身に持つ `<span>`・`<b>`・`<em>`・`<strong>`・`<i>`・`<font>`・`<small>`・`<sup>`、および `alt="NEW"` の `<img>` を、直前の空白ごと消します。掲載から数日で消えるバッジが「更新」と誤検知されるのを防ぐためです。`<span>New York</span>` や `<span>NEW 通知</span>` のように、ほかの文字を含むものは消しません。

### 全サイト共通の `ignore`（任意）

`config.yaml` のトップレベルに `ignore:` を書くと、**全サイト**に追加で適用されます（標準ルールのあと、サイト別の `ignore` の前）。

```yaml
ignore:
  - ';jsessionid=[0-9A-Fa-f]+'       # Java 系サーバーのセッションID
sites:
  - url: …
```

## GitHub Secrets（g-i-t-app）

| 名前 | 内容 |
|---|---|
| `GH_PAT` | `g-i-t-data` への読み書き（fine-grained の `Contents: Read and write` で足りる）。ダッシュボードの公開にも使う |
| `GEMINI_API_KEY` | Gemini API のキー |
| `LLM_FALLBACK_KEY` | （任意）OpenAI 互換の別プロバイダーの API キー。Gemini が全部だめなときだけ使う |
| `SUPABASE_URL` | Supabase のプロジェクト URL（`https://<REF>.supabase.co`、末尾に `/` を付けない） |
| `SUPABASE_KEY` | Supabase の Secret key（`sb_secret_...`）。サーバー側専用。公開しない |
| `WEBSITE_STALKER_FROM` | 連絡先メールアドレス。確認先サイトへの HTTP `From` ヘッダーとして送られる（`@` と `.` を含むこと） |
| `IA_ACCESS_KEY` / `IA_SECRET_KEY` | Internet Archive の S3 キー（`archive.yml` のみ使用） |

```powershell
gh secret set SUPABASE_KEY --repo TanukiMa/g-i-t-app     # 値は対話入力（履歴に残さない）
gh secret list --repo TanukiMa/g-i-t-app
```

## 環境変数（任意）

| 名前 | 既定 | 内容 |
|---|---|---|
| `GEMINI_MODELS` | `gemini-3.8-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite` | 要約に使うモデルを、**優先順にカンマ区切り**で。無料枠の1日の上限（RPD）はモデルごとに数えられるため、1つが使い切りになると次のモデルに移る。リポジトリの Variables で設定する |
| `LLM_FALLBACK_URL` / `LLM_FALLBACK_MODEL` | （未設定） | （任意）別プロバイダーの OpenAI 互換 API（`.../chat/completions` の URL とモデル名）。Variables で設定し、キーは Secrets の `LLM_FALLBACK_KEY` |
| `SUMMARY_BACKFILL_LIMIT` | `10` | 1回の実行で作り直す、失敗した要約の最大件数（`0` で無効） |
| `ARCHIVE_BATCH_SIZE` | `20` | アーカイブ処理が、1ラウンドで取り出す URL の件数 |
| `ARCHIVE_RUNTIME_MIN` | `14` | 1回の実行で、新しい URL に着手する時間の予算（分）。待ちがなくなれば、その前に終わる |
| `ARCHIVE_INTERVAL_SEC` | `15` | 保存の間隔（秒） |
| `SITE_BASE_URL` | `https://tanukima.github.io/g-i-t-data/` | Atom フィード内の絶対 URL の基準 |
| `GA_MEASUREMENT_ID` | （未設定） | Google アナリティクス 4 の測定 ID（`G-XXXXXXXXXX`）。Variables に設定する。未設定または形式が違うと、Google のタグは出力されない |
| `CF_BEACON_TOKEN` | （未設定） | Cloudflare Web Analytics のトークン。Variables に設定する。未設定または形式が違うと、Cloudflare のタグは出力されない |

## アクセス解析の設定（任意）

どちらも **GitHub の Variables**（Secrets ではありません。ページに公開される値です）に設定します。設定すると、次回の `stalk.yml` で全ページに組み込まれます。

```powershell
gh variable set GA_MEASUREMENT_ID --repo TanukiMa/g-i-t-app --body "G-XXXXXXXXXX"
gh variable set CF_BEACON_TOKEN   --repo TanukiMa/g-i-t-app --body "<Cloudflare のトークン>"
```

1. **Google アナリティクス 4:** プロパティを作り、ウェブのデータストリーム（URL は `https://tanukima.github.io/g-i-t-data/`）を追加して、測定 ID（`G-` で始まる）を取得する。
2. **Cloudflare Web Analytics:** ダッシュボードの Web Analytics で「サイトを追加」し、ホスト名を入れる。Cloudflare を経由していないサイトなので、表示されるスニペットの `data-cf-beacon` の `token` の値を使う。
3. 組み込んだら、サイトの `プライバシーとアクセス解析` のページに、使っているサービスだけが表示されることを確認する。

仕様：既定で計測する（オプトアウト）。読者が停止した場合や、ブラウザが Do Not Track / Global Privacy Control を送っている場合は、外部サービスの読み込み自体を行わない。URL の `?follow=…` は、Google には送らず（URL から除く）、Cloudflare のタグはその表示では読み込まない。実装は `static/analytics.js`。
