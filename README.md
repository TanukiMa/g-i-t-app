# G医t Orchestrator App (`g-i-t-app`)

G医t (G-I-T) 医療・行政情報 Web更新自動追跡・アーカイブプラットフォームのオーケストレーター・コードリポジトリです。

## 概要

`g-i-t-app` は以下の責務を持ちます：
1. **JIT Auto-Provisioning (`scripts/provision.py`)**: `g-i-t-data/config.yaml` を読み込み、未登録の監視対象URLがあれば `sites/<slug>/website-stalker.yaml` を自動作成してコミットします。
2. **Web Stalking Pipeline (`scripts/website_stalk.py`)**: 各 `sites/<slug>/` で `website-stalker run --all` を実行し、差分発生時にサイトごとに 1 コミットを作成します。続けて Gemini API による日本語要約、差分 HTML (diff2html) 生成、Supabase へのメタデータ登録、Wayback Machine 保存対象 URL のキュー登録（`archive_queue`）を行います。
3. **Dashboard Builder (`scripts/build_dashboard.py`)**: Supabase から更新履歴を取得し、GitHub Pages 用の静的 HTML ダッシュボードを構築します。
4. **GitHub Actions Workflow (`.github/workflows/stalk.yml`)**: 上記パイプラインを定期実行します。

## ディレクトリ構成

```text
g-i-t-app/
├── .github/
│   └── workflows/
│       ├── stalk.yml          # 定期実行ワークフロー（毎時）
│       └── archive.yml        # アーカイブワーカー（15分毎）
├── scripts/
│   ├── website_stalk.py       # パイプライン本体
│   ├── provision.py           # sites/<slug>/ 自動プロビジョニング
│   ├── archive_worker.py      # archive_queue を Internet Archive に保存
│   └── build_dashboard.py     # ダッシュボード生成（GitHub風 / dashboard / minimal）
├── templates/
│   ├── index.html             # 全体タイムラインテンプレート
│   └── site_detail.html       # サイト個別タイムラインテンプレート
├── requirements.txt           # Python依存ライブラリ
└── README.md
```
