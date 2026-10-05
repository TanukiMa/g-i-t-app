# G医t

## Orchestrator App (`g-i-t-app`)

**G医t-app**は医療情報のWeb更新自動追跡・アーカイブプラットフォームのオーケストレーターです。

## 概要

`g-i-t-app` は以下の動作をする

1. **JIT Auto-Provisioning (`scripts/provision.py`)**

`g-i-t-data/config.yaml` を読み込み、未登録の監視対象URLがあれば `sites/<slug>/website-stalker.yaml` を自動作成し、git repositoryにcommitする。

2. **Web Stalking Pipeline (`scripts/website_stalk.py`)**

各 `sites/<slug>/` で `website-stalker run --all` を実行し、差分発生時にサイトごとに 1 commitを実施。
続けて 差分に対してGemini API による日本語要約を生成、差分 HTML ([diff2html](https://diff2html.xyz/)) を生成、
Supabase へのメタデータ登録、Wayback Machine 保存対象URLのキュー登録（`archive_queue`）を行う。

3. **Dashboard Builder (`scripts/build_dashboard.py`)**

[Supabase](https://supabase.com/) から更新履歴を取得し、GitHub Pages 用の静的 HTML ダッシュボードを構築する。

4. **GitHub Actions Workflow (`.github/workflows/stalk.yml`)**

上記パイプラインを定期実行する。

