# データ構造

G醫t のデータは2か所にあります。**Git（g-i-t-data）が一次の記録**で、Supabase は検索・集計のための索引です。

> 現時点で公開しているのは、**`g-i-t-data` リポジトリ（Git の履歴）と、このダッシュボード**です。Supabase のテーブルは非公開（運営者のみ）です。研究などで履歴を使うときは、Git の履歴から取り出せます。

## 1. Git（g-i-t-data）

```text
g-i-t-data/
├── config.yaml                  確認先のマスター設定
├── sites/<slug>/
│   ├── website-stalker.yaml     そのサイトの取得設定
│   └── <host>/<path>.html       取得して整形したページ（URL から決まるファイル名）
└── public/                      ダッシュボード（生成物）
```

### コミットの種類
| メッセージ | 意味 |
|---|---|
| `Provision <slug>` | サイトを新しく登録した（設定ファイルの作成だけ） |
| `Update ignore rules for <slug>` | `ignore` のルールを既存サイトに追記した |
| `Update <slug>` | **取得したページに変化があった**（1サイト・1回の実行・1コミット） |
| `Snapshot <日時> UTC` | ダッシュボード（`public/`）の更新 |
| `Reset: keep configuration only` | リセットで履歴を作り直した（[運用手順](Operations)） |

`Update <slug>` のコミットは書き換えません。このコミットのハッシュが、Supabase の `commit_hash` と一致します。

### 履歴の取り出し方
```bash
git clone https://github.com/TanukiMa/g-i-t-data.git && cd g-i-t-data

# あるサイトの更新履歴（新しい順）
git log --format='%h %ad %s' --date=iso -- sites/naika/

# 1回分の変化
git show <ハッシュ> -- sites/naika/

# ある日以降の差分を通しで
git log -p --since=2026-10-01 -- sites/naika/
```
`public/` のコミット（`Snapshot`）を除くには、`-- sites/<slug>/` でパスを絞ります。時刻は Git の記録では UTC 基準、ダッシュボードの表示は JST です。

## 2. Supabase

### `updates` … 更新の記録（`Update <slug>` 1つにつき1行）
| 列 | 型 | 内容 |
|---|---|---|
| `id` | bigserial | 主キー |
| `site_slug` | varchar(255) | サイトの ID |
| `domain` | varchar(255) | 確認先のホスト名 |
| `url` | text | 確認先の URL |
| `commit_hash` | varchar(64) | `g-i-t-data` のコミット（`Update <slug>`） |
| `summary` | text | AI要約（Markdown の一部：箇条書き、**太字**、`code`） |
| `summary_model` | text | 要約を書いたモデル名。AI を使わず決めた場合は `rule`。古い行は空 |
| `created_at` | timestamptz | 記録した時刻（UTC） |

`summary` には、要約の代わりに次の定型文が入ることがあります。

| 値 | 意味 |
|---|---|
| `記録を開始しました` | 確認を始めたときの最初の保存。比べる相手がなく、更新ではない（以前は `初回取得: 監視を開始しました。…` という文言で、ダッシュボードは今もこれを同じものとして扱う） |
| `要約を生成できませんでした。` | Gemini が失敗した。以降の実行で作り直される |
| `差分を取得できなかったため要約できません。` | コミットが無い等で、作り直せない |
| `内容に実質的な変更はありません（表示の調整のみ）。` | 要約の結果、意味のある変更がなかった |

### `archive_queue` … Internet Archive への保存の予定と結果
| 列 | 内容 |
|---|---|
| `id` | 主キー |
| `site_slug`、`commit_hash` | どの更新に属するか（`updates` と結べる） |
| `url` | 保存する URL |
| `kind` | `page`（確認先のページ）または `attachment`（ページに載っていた文書） |
| `status` | `pending` / `done` / `failed` |
| `archive_url` | 保存後の Wayback の URL |
| `attempts`、`last_error` | 試行回数と、直近のエラー |
| `next_try_at` | 次に試す時刻 |
| `created_at` | 登録した時刻 |

一意制約は `(site_slug, commit_hash, url)` です。DDL は [`sql/schema.sql`](https://github.com/TanukiMa/g-i-t-app/blob/main/sql/schema.sql) にあります。

### 結合の例（SQL）
```sql
-- あるサイトの更新と、そのページの Wayback の URL
select u.created_at, u.summary, q.archive_url
from updates u
left join archive_queue q
  on q.site_slug = u.site_slug and q.commit_hash = u.commit_hash and q.kind = 'page' and q.status = 'done'
where u.site_slug = 'naika'
order by u.created_at desc;
```
