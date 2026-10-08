# アーキテクチャ

![G醫tのしくみ](https://tanukima.github.io/g-i-t-data/assets/system.svg)

## 構成要素

| 要素 | 役割 |
|---|---|
| **g-i-t-app**（リポジトリ） | コード。Python のパイプライン、GitHub Actions のワークフロー、ページのテンプレート |
| **g-i-t-data**（リポジトリ） | データ。`config.yaml`（確認先のサイト）、`sites/<slug>/`（取得したページ）、`public/`（ダッシュボード） |
| **website-stalker**（Matanuki version） | 各ページの取得と、本文の整形（Rust 製 CLI） |
| **GitHub Actions** | 1 日 4 回の本処理と、その約 50 分後の、アーカイブ処理 |
| **Gemini API** | 差分の要約（日本語） |
| **Supabase** | 更新のメタデータと、アーカイブ待ちの一覧 |
| **Internet Archive** | ページのコピーの保存（Wayback Machine） |
| **GitHub Pages** | `public/` の配信（`gh-pages` ブランチ） |

サブモジュールや、サイトごとのリポジトリは使いません。すべての取得データは `g-i-t-data` の `sites/<slug>/` に入ります。

## 2つのワークフロー

| ワークフロー | 実行 | 内容 |
|---|---|---|
| `stalk.yml` | 1 日 4 回（時刻は、`stalk.yml` の `cron` が正。計画は、JST の 8:30・12:30・16:30・22:00）と手動。Cloud Run に移したあとは、使わない（Cloud Scheduler が起動する） | 取得 → コミット → 要約 → 記録 → ダッシュボード生成 → 公開 |
| `archive.yml` | 1 日 4 回（JST の 9:20・13:20・17:20・22:50 ＝ UTC の 00:20・04:20・08:20・13:50。計画の取得時刻の約 50 分後）と手動。1 回の実行は、最大 60 分。Cloud Run に移したあとも、これは GitHub Actions で動かす（待ち時間が長く、Cloud Run だと課金が無駄になる） | アーカイブ待ちの URL を、1件ずつ間隔を空けて Internet Archive に保存 |

どちらも同時に2つ走らないよう `concurrency` で直列化しています。`archive.yml` は Git に触れず、Supabase と Internet Archive だけを使うので、`stalk.yml` とは競合しません。

## 1 回の処理（stalk.yml → website_stalk.py）

1. **プロビジョニング** … `config.yaml` に載っていて、`sites/<slug>/` が無いサイトに、`website-stalker.yaml` を作ってコミットする。`ignore` の追加分は、既存サイトにも追記する。
2. **取得** … サイトごとに `website-stalker run --all` を実行する。
3. **変化の判定** … `sites/<slug>/` に変更があれば、そのサイトだけをステージする。
4. **要約** … 差分（先頭 10,000 文字）を Gemini に渡して要約する。初回取得は要約しない。
5. **コミット** … サイトごとに1コミット（`Update <slug>`）。
6. **差分ページ** … `diff2html` で左右比較の HTML を作り、`public/sites/<slug>/diff_<hash7>.html` に置く。
7. **push** … 全サイトの処理が終わってから、1回だけ push する。
8. **記録** … push に成功したときだけ、Supabase の `updates` に1行ずつ書き、アーカイブ待ち（`archive_queue`）に登録する。
9. **要約の作り直し** … 以前に失敗した要約を、最大10件まで作り直す。
10. **ダッシュボード生成** … Supabase から全件を読み、`public/` を作って commit・push し、`gh-pages` に公開する。

## アーカイブ処理（archive.yml → archive_worker.py）

- **15秒**間隔（`ARCHIVE_INTERVAL_SEC`）で1件ずつ保存する。20 件（`ARCHIVE_BATCH_SIZE`）ずつ取り出し、**待ちがなくなるか、時間の予算（`ARCHIVE_RUNTIME_MIN`、既定 14 分）を使い切るまで**続ける。
  - GitHub は、頻度の高いスケジュールを間引きます（「15分おき」でも1日に数回しか動かないことがある）。そのため、1回の実行で打ち切らず、動いたときにまとめて処理する作りにしています。
- 登録する URL は、確認先のページ自体と、**差分で追加された同一ドメインの文書**（`.pdf` / `.doc(x)` / `.xls(x)` / `.ppt(x)`、1回につき最大50件）。
- HTTP 429 のときは、その回を打ち切り、15分後に再試行する。robots.txt で拒否された URL は即 `failed`。その他の失敗は、30分から倍々の間隔で最大5回まで再試行し、`last_error` に例外の種類（`BadGateway` など）を記録する。
- IA のキーが無効（`Unauthorized`）のときは、再試行の回数を使い切らないよう、行を変えずに止めて、実行を失敗にする。

## 出力されるページ（`public/`）

| ファイル | 内容 |
|---|---|
| `index.html` / `dashboard.html` / `minimal.html` | 直近 **200件**の更新。3種類の見た目 |
| `sites.html` | サイト一覧 |
| `sites/<slug>/history.html` | サイト別の更新歴（全件） |
| `sites/<slug>/diff_<hash7>.html` | 差分ページ |
| `archive/index.html`、`archive/<年>-W<週>.html`、`archive/<年>-<月>.html` | 週別（詳細）・月別（一覧）の過去ログ |
| `feeds/all.xml`、`feeds/<slug>.xml`、`feeds/tag-*.xml` | Atom フィード（すべて／サイト別／分類別） |
| `about.html` | G醫tについて |
| `privacy.html` | プライバシーとアクセス解析 |
| `manifest.webmanifest`、`sw.js`、`offline.html` | **アプリとしてインストール**するための設定、Service Worker、オフライン用の案内ページ |
| `assets/` | CSS、`app.js`（フォロー・絞り込み）、図 |

時刻はすべて日本時間（JST）で表示します。

## フォローと絞り込み（ブラウザ内）
`assets/app.js` が、星ボタン、「フォロー中のみ」の切り替え、検索、分類での絞り込みを担当します。フォローの選択は `localStorage`（キーは `g-i-t-data:v1:` で始まる）にだけ保存され、サーバーには送られません。`?follow=a,b,c` で選択を共有できます。JavaScript が無効でも、すべての更新が表示されます。

## サイズの確認
`stalk.yml` が実行のたびに `g-i-t-data` の `.git` のサイズをログに出し、700MB を超えると警告します（GitHub の推奨は 1GB 未満）。`gh-pages` は毎回1コミットに作り直す（`force_orphan`）ので、履歴は溜まりません。

## アプリとして使う（PWA）
ブラウザから「インストール」すると、ホーム画面やスタートメニューから、ブラウザの枠なしで開けます（iPhone は Safari の「ホーム画面に追加」）。サーバー側の変更はありません。

- **最新であること**を最優先にしています。ページは**ネットワーク優先**で、つながっていれば常に最新を表示します。保存済みの控えは、**接続できないとき**（または、控えがあって、4 秒待っても応答がないとき）だけ使います。
- CSS・JS・アイコンは、保存済みのものをすぐ出しつつ、裏で更新します。
- Atom フィード、差分ページ、他のドメイン（Google・Cloudflare・Wayback）には関与しません。
- 控えは、最近開いた**最大 60 ページ**です。共有リンクの `?follow=…` は保存しません。
- デプロイのたびに Service Worker は作り直されます（`sw.js` のビルド ID）。古い資産の控えは、新しい版の起動時に消えます。
- アイコンは `python scripts/make-icons.py`（Pillow が必要）で作り直し、PNG をコミットします。
