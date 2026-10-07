# G醫t Wiki

G醫t（G-I-T）は、医療・薬事・学会関連サイトの「本当に必要な更新」を捉えて記録し、日本語で解説する個人プロジェクトです。約200のウェブサイトを毎時間見に行き、**意味のある変化だけ**を Git に残します。

**ダッシュボード（読む場所）: <https://tanukima.github.io/g-i-t-data/>**

## このWikiについて
ダッシュボードの [G醫tについて](https://tanukima.github.io/g-i-t-data/about.html) は、一般の読者向けの短い説明です。ここには、**設計の考え方・しくみ・設定・データ構造・運用**の詳しい説明を置きます。主な読者は、共同作業者、研究者、そして将来の自分です。

| ページ | 内容 |
|---|---|
| [設計思想](Design-Philosophy) | なぜこう作ったか。ノイズの扱い、履歴、保存、AIの位置づけ |
| [アーキテクチャ](Architecture) | 構成要素、ワークフロー、処理の流れ、出力されるページ |
| [設定リファレンス](Configuration) | `config.yaml` の書き方（`name` / `slug` / `tags` / `ignore`）、Secrets、環境変数 |
| [データ構造](Data-Model) | Git 上のデータ、Supabase のテーブル、研究での使い方 |
| [運用手順](Operations) | サイトの追加、手動実行、リセット、困ったときの対処 |

## 1枚でわかる図
![G醫tのしくみ](https://tanukima.github.io/g-i-t-data/assets/system.svg)

## リポジトリ
- [g-i-t-app](https://github.com/TanukiMa/g-i-t-app) … 仕組みのコード（Python、GitHub Actions、テンプレート）
- [g-i-t-data](https://github.com/TanukiMa/g-i-t-data) … 確認先の設定（`config.yaml`）と、取得したデータ
- [website-stalker（Matanuki version）](https://github.com/TanukiMa/website-stalker) … 取得と差分検出の部分（元は [EdJoPaTo/website-stalker](https://github.com/EdJoPaTo/website-stalker)）
