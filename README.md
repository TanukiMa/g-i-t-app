# G医t Orchestrator App (`g-i-t-app`)

G医t (G-I-T) 医療・行政情報 Web更新自動追跡・アーカイブプラットフォームのオーケストレーター・コードリポジトリです。

## 概要

`g-i-t-app` は以下の責務を持ちます：
1. **JIT Auto-Provisioning (`scripts/provision.py`)**: `g-i-t-data/website-stalker.yaml` を読み込み、未登録の監視対象URLがあれば対応する GitHub サブモジュール (`g-i-t-data-<slug>`) を自動作成・登録します。
2. **Web Stalking Pipeline (`scripts/website_stalk.py`)**: 各サブモジュールで `website-stalker` を実行し、差分発生時に Gemini API による日本語要約、Internet Archive (Wayback Machine) への保存、Supabase へのメタデータ登録、差分 HTML (diff2html) 生成を行います。
3. **Dashboard Builder (`scripts/build_dashboard.py`)**: Supabase から更新履歴を取得し、GitHub Pages 用の静的 HTML ダッシュボードを構築します。
4. **GitHub Actions Workflow (`.github/workflows/stalk.yml`)**: 上記パイプラインを定期実行します。

## ディレクトリ構成

```text
g-i-t-app/
├── .github/
│   └── workflows/
│       └── stalk.yml          # 定期実行ワークフロー
├── scripts/
│   ├── website_stalk.py       # パイプライン本体
│   ├── provision.py           # サブモジュール自動プロビジョニング
│   └── build_dashboard.py     # ダッシュボード生成
├── templates/
│   ├── index.html             # 全体タイムラインテンプレート
│   └── site_detail.html       # サイト個別タイムラインテンプレート
├── requirements.txt           # Python依存ライブラリ
└── README.md
```
