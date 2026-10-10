# Cloud Run + Firebase Hosting で動かす

GitHub Actions の `schedule` の代わりに、**Cloud Scheduler → Cloud Run Job** で `stalk` を毎時動かし、できあがった `public/` を **Firebase Hosting**（独自ドメイン）に公開します。

```text
Cloud Scheduler ──▶ Cloud Run Job (container, asia-northeast1)
                      0. g-i-t-app のコードを GitHub から取得（bootstrap.sh、APP_REF で固定可）
                      1. g-i-t-data を部分 clone（--filter=blob:none）
                      2. website_stalk.py（取得・要約・push・ダッシュボード生成）
                      3. git gc
                      4. firebase deploy --only hosting
```

> **注意:** この構成は、手元で `docker build` / `docker run` による動作確認をまだしていません。最初の実行は、Cloud Run のログを見ながら進めてください（特に「Firebase CLI がサービスアカウントで認証できるか」）。

構成ファイル: `container/Dockerfile` / `cloudbuild.yaml` / `container/entrypoint.sh` / `container/firebase.json` / `monitoring/*.json`

## 0. 変数（以降のコマンドで使う）

```bash
PROJECT=<GCP プロジェクト ID>
REGION=asia-northeast1
SA=g-i-t-runner
DOMAIN=git.example.com        # 公開するサブドメイン
```

## 1. 準備

```bash
gcloud config set project $PROJECT
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com firebasehosting.googleapis.com \
  monitoring.googleapis.com serviceusage.googleapis.com

# Firebase をこのプロジェクトに追加（コンソール https://console.firebase.google.com/ で「プロジェクトを追加」→ 既存の GCP プロジェクトを選ぶ）
# （CLI で `projects:addfirebase` が 403 PERMISSION_DENIED になるときは、コンソールの「プロジェクトを追加」で、
#  既存の GCP プロジェクトを選ぶ。Firebase の利用規約への同意が要る。Google Workspace の組織では、管理コンソールの
#  「追加の Google サービス」で Firebase が無効だと、組織のメンバーは追加できない）
# Hosting を使い始める（初回だけ）
npx firebase-tools init hosting --project $PROJECT   # 質問は、public=data/public、SPA=No、GitHub 連携=No、上書き=No
```

## 2. サービスアカウントと権限

```bash
gcloud iam service-accounts create $SA
for role in roles/secretmanager.secretAccessor roles/firebasehosting.admin roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$SA@$PROJECT.iam.gserviceaccount.com --role=$role
done
```

## 3. シークレット（Secret Manager）

値は対話で入力し、履歴に残さないようにします。

```bash
for name in GH_PAT GEMINI_API_KEY SUPABASE_URL SUPABASE_KEY WEBSITE_STALKER_FROM IA_ACCESS_KEY IA_SECRET_KEY; do
  read -rsp "$name: " v; echo
  printf '%s' "$v" | gcloud secrets create $name --data-file=-
done
# 任意: LLM_FALLBACK_KEY も同様に
```

## 4. イメージのビルド

```bash
gcloud artifacts repositories create git --repository-format=docker --location=$REGION
IMAGE=$REGION-docker.pkg.dev/$PROJECT/git/g-i-t-app:latest
# 初回は Rust のビルドで 10〜20 分かかる。_TARGET は、公開先のツールを選ぶ（下の表）
gcloud builds submit --config cloudbuild.yaml --substitutions _IMAGE=$IMAGE,_TARGET=firebase .
```

イメージは、**公開先ごとに 1 つ**です（`container/Dockerfile` の `--target`）。`github-pages` への公開は git だけなので、どのイメージでも使えます。

| `_TARGET` | 入るツール | 使える `DEPLOY_TARGETS` | 大きさ（実測の土台 478 MB に加算） |
|---|---|---|---|
| `firebase`（既定） | `firebase-tools` | `github-pages`、`firebase` | 約 720 MB |
| `cloudflare` | `wrangler` | `github-pages`、`cloudflare-pages` | 約 650 MB |
| `base` | なし | `github-pages` | 約 480 MB |

- **`DEPLOY_TARGETS` に、そのイメージに無い公開先を書くと、起動時にエラーで止まります**（実行の最後ではなく、最初に分かります）。
- 公開先を Firebase から Cloudflare に切り替えるときは、`_TARGET=cloudflare` でイメージを作り直し、ジョブのイメージと `DEPLOY_TARGETS` を更新します。両方に同時に出す期間は、2 つのイメージを別々のジョブで走らせるか、イメージに両方のツールを入れる必要があります（現状は、片方ずつです）。
- 大きさは展開後の値です。Artifact Registry の保存容量は、圧縮後で数えられるはずです（無料枠 0.5 GB。push のあとで `gcloud artifacts docker images list` で確認してください）。
- `gcloud builds submit` が送るのは、**`.gcloudignore` に従った 4 ファイルだけ**です（`cloudbuild.yaml`、`requirements.txt`、`container/Dockerfile`、`container/bootstrap.sh`）。`.dockerignore` は、送るファイルの選択には使われません。この指定が無いと、`client/target` など数 GB と、`.env`（実際のキー）まで送ってしまいます。確認: `gcloud meta list-files-for-upload .`
- バージョンを固定するときは、`--substitutions …,_FIREBASE_TOOLS_VERSION=<x.y.z>`（または `_WRANGLER_VERSION`）を足します。固定しないと、ビルドした日の最新版が入ります。

**イメージに入っているのは、ツールと依存だけです**（website-stalker、Node、Python の依存）。`scripts/`・`templates/`・`static/` などの g-i-t-app のコードは、**実行のたびに GitHub（`APP_REPO` の `APP_REF`）から取得します**。コードを変えたときは、push するだけで、次の実行から反映されます。イメージの作り直しが要るのは、`requirements.txt` か `Dockerfile` を変えたときだけです（`requirements.txt` が焼き込んだものと違うと、起動時にエラーで止まります）。

- `APP_REF`（既定 `main`）にはブランチ・タグ・コミットを指定できます。**壊れたコミットを push してしまったときは、再ビルドなしで戻せます。**
  `gcloud run jobs update g-i-t-stalk --region $REGION --update-env-vars APP_REF=<正常だったコミット>`
- `g-i-t-app` は public なので、取得にトークンは要りません（private にしたときは、`GH_PAT` に読み取り権限を足す改修が要ります）。

website-stalker のフォークを更新したときだけ、`--no-cache` で作り直します（イメージはフォークの HEAD を固定しません。固定するなら `--build-arg WS_REV=<SHA>`）。

## 5. Cloud Run Job

```bash
gcloud run jobs create g-i-t-stalk \
  --image $IMAGE --region $REGION --service-account $SA@$PROJECT.iam.gserviceaccount.com \
  --cpu 1 --memory 1Gi --max-retries 0 --task-timeout 3000s \
  --set-env-vars "^#^FIREBASE_PROJECT=$WEB#SITE_BASE_URL=https://$DOMAIN/#DEPLOY_TARGETS=github-pages,firebase#PWA_ENABLED=0#SUPABASE_URL=...#WEBSITE_STALKER_FROM=..." \
  --set-secrets GH_PAT=GH_PAT:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_KEY=SUPABASE_KEY:latest,WEBSITE_STALKER_FROM=WEBSITE_STALKER_FROM:latest

gcloud run jobs execute g-i-t-stalk --region $REGION --wait     # 手動で 1 回
gcloud run jobs executions list --job g-i-t-stalk --region $REGION
```

- タイムアウトを **50 分**にしているのは、実行どうしが重ならないようにするためです（1 日 3 回なら、間隔は、4 時間以上あるので、もっと長くしても構いません）（重なると、同じ g-i-t-data に同時に push して競合します）。
- 環境変数の値にカンマがあるので（`DEPLOY_TARGETS=github-pages,firebase`）、`--set-env-vars` の先頭に **`^#^`**（区切りを `#` に変える記法（`@` は、`WEBSITE_STALKER_FROM` のメールアドレスと衝突するので使えない））を付けています。付けないと、`firebase` が別の変数として解釈されてエラーになります。
- **`DEPLOY_TARGETS` を指定しないと、既定の `github-pages` だけに出て、Firebase には何も出ません。** 移行の間は `github-pages,firebase` の両方に出し、切り替えが済んだら `firebase` だけにします（6b）。
- GA4 / Cloudflare のタグ、Gemini のモデル順、フォールバック LLM は、`--set-env-vars` に `GA_MEASUREMENT_ID` などを足して渡します（`docs/wiki/Configuration.md`）。値にカンマが入るもの（`GEMINI_MODELS` など）は、`--set-env-vars "^#^GEMINI_MODELS=a,b,c#KEY=…"` のように区切り文字を変えます。
- 最初は `--memory 1Gi` で始め、実行のログとメトリクスで足りなければ `2Gi` にします（実測はしていません）。

## 5b. そのほかのジョブ（同じイメージ、引数だけ違う）

**`scripts/cloud-run-jobs.ps1` で、`g-i-t-stalk`、`g-i-t-remake-dashboard`、`g-i-t-resummarize` を、1 つの設定から作る・更新できます**（`SITE_BASE_URL` や `DEPLOY_TARGETS` がジョブごとにずれるのを防ぎます）。`pwsh scripts/cloud-run-jobs.ps1 -DryRun` で内容を確認してから、`-DryRun` を外して実行します。独自ドメインに移すときは、`-SiteBaseUrl https://giiit.goudge.org/ -DeployTargets github-pages-redirect,firebase` を付けて、もう一度実行すれば、全ジョブが揃って更新されます。`--set-env-vars` は一覧を丸ごと置き換えるので、追加の環境変数（`GA_MEASUREMENT_ID` など）は `-ExtraEnv "KEY=値"` で渡してください。以下は、手で作る場合のコマンドです。

```bash
# ダッシュボードの見た目だけ直したとき。取得・git の commit と push・AI は動かない（GH_PAT は clone 用）
gcloud run jobs create g-i-t-remake-dashboard \
  --image $IMAGE --region $REGION --service-account $SA@$PROJECT.iam.gserviceaccount.com --args=remake-dashboard \
  --cpu 1 --memory 1Gi --max-retries 0 --task-timeout 900s \
  --set-env-vars "^#^FIREBASE_PROJECT=$WEB#SITE_BASE_URL=https://$DOMAIN/#DEPLOY_TARGETS=github-pages,firebase#PWA_ENABLED=0#SUPABASE_URL=...#WEBSITE_STALKER_FROM=..." \
  --set-secrets GH_PAT=GH_PAT:latest,SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_KEY=SUPABASE_KEY:latest

# 保存済みの要約を作り直す（引数は実行のたびに渡す）
gcloud run jobs create g-i-t-resummarize \
  --image $IMAGE --region $REGION --service-account $SA@$PROJECT.iam.gserviceaccount.com --args=resummarize \
  --cpu 1 --memory 1Gi --max-retries 0 --task-timeout 1800s \
  --set-secrets GH_PAT=GH_PAT:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_KEY=SUPABASE_KEY:latest
gcloud run jobs execute g-i-t-resummarize --region $REGION --wait --args=resummarize,--dry-run
gcloud run jobs execute g-i-t-resummarize --region $REGION --wait --args=resummarize,--limit,50

# 論文用の実験（RAW_EXPERIMENT=1 の間だけ。通常は作らない）
gcloud run jobs create g-i-t-raw \
  --image $IMAGE --region $REGION --service-account $SA@$PROJECT.iam.gserviceaccount.com --args=raw \
  --cpu 1 --memory 1Gi --max-retries 0 --task-timeout 3000s \
  --set-env-vars RAW_EXPERIMENT=1 \
  --set-secrets GH_PAT=GH_PAT:latest,WEBSITE_STALKER_FROM=WEBSITE_STALKER_FROM:latest
```

### 吸収された更新の回収（`g-i-t-recover`）: 手元に Python も Gemini のキーも要りません
Re-baseline のコミットに吸収された更新（`jami` の 10/7 など）を、Cloud Run で回収します。`gcloud` だけあれば、Windows でも macOS でも、出先でも実行できます（キーは Secret Manager のものを使います）。

```bash
# ジョブを 1 回だけ作る（pwsh が無い環境では、下の gcloud 版）
pwsh scripts/cloud-run-jobs.ps1 -Jobs recover
gcloud run jobs create g-i-t-recover --image $IMAGE --region $REGION --service-account $SA@$PROJECT.iam.gserviceaccount.com   --args recover --cpu 1 --memory 1Gi --max-retries 0 --task-timeout 2400s   --set-env-vars "^#^SUPABASE_URL=<URL>" --set-secrets GH_PAT=GH_PAT:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,SUPABASE_KEY=SUPABASE_KEY:latest

# 一覧を見る（何も変更しない）
gcloud run jobs execute g-i-t-recover --region $REGION --wait --args=recover,--dry-run
# 5 件だけ実行する → 結果を見て、問題なければ --limit を外して残りを実行する
gcloud run jobs execute g-i-t-recover --region $REGION --wait --args=recover,--limit,5
# 値にカンマがあるオプション（--exclude a,b）は、区切りを # にする
gcloud run jobs execute g-i-t-recover --region $REGION --wait --args="^#^recover#--exclude#jsmez,digitalmeddx#--limit#5"
# 差分ページを公開する（ダッシュボードを作り直して配信）
gcloud run jobs execute g-i-t-remake-dashboard --region $REGION --wait
# ログ
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="g-i-t-recover"' --limit 80 --order=desc --format="value(textPayload)"
```
- 要約のモデルは、既定で `gemini-3.5-flash-lite` です（`--model` で変更）。`created_at` は、コミットの時刻です。
- 差分ページは、ジョブが `g-i-t-data` にコミットして push します（コミット名: `Recover diff pages (N)`）。**定時実行（7:30・12:30・16:30）の最中は避けてください**（同時に push すると競合しますが、ジョブは自動で再試行します）。
- コードは起動のたびに GitHub の `main` から取得されるので、**先に `main` に push** してください（イメージの作り直しは不要です）。

`--args` の指定は、ジョブの既定の引数になります。実行時に変えるときは、`gcloud run jobs execute … --args=…` を使います。

## 6. スケジュール（Cloud Scheduler）

```bash
PROJECT_NUMBER=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
gcloud run jobs add-iam-policy-binding g-i-t-stalk --region $REGION \
  --member=serviceAccount:$SA@$PROJECT.iam.gserviceaccount.com --role=roles/run.invoker

# 1 日 3 回: 朝 7:30、昼 12:30、夕 16:30（日本時間）。夜間は動かさない。ジョブは 1 つ（無料枠は 3 ジョブまで）
URI="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_NUMBER/jobs/g-i-t-stalk:run"
gcloud scheduler jobs create http g-i-t-stalk --location $REGION \
  --schedule "30 7,12,16 * * *" --time-zone Asia/Tokyo \
  --http-method POST --uri "$URI" \
  --oauth-service-account-email $SA@$PROJECT.iam.gserviceaccount.com
```

`feeds/status.json` の実行時刻は、ジョブの環境変数 `STALK_RUNS`（例: `07:30,12:30,16:30`）か、`scripts/common.py` の既定値です。Cloud Scheduler の時刻と合わせてください。

スケジュールは、1 日 3 回（日本時間 7:30・12:30・16:30）です。Cloud Scheduler の無料枠は「ジョブが 3 つまで」で、実行回数の上限ではありません。**アーカイブ（`archive.yml`）は、Cloud Run に移さず、GitHub Actions のままにします**（約 50 分後の 3 回、JST 8:20・13:20・17:20）。1 件ごとに 15〜60 秒の待ちがあり、Cloud Run では、待ちも課金されるためです。GitHub Actions の `stalk.yml`（`cron` は UTC で書く）とは、別に、Cloud Scheduler 側で時刻帯を指定できます。Cloud Scheduler は、時刻どおりに起動します（GitHub の `schedule` のような遅れは、ありません）。

```bash
gh workflow disable stalk.yml --repo TanukiMa/g-i-t-app
```

## 6b. 公開先を選ぶ（`DEPLOY_TARGETS`）

同じイメージが、次の公開先のどれにでも（複数同時にも）デプロイできます。`DEPLOY_TARGETS=firebase,cloudflare-pages` のように、カンマ区切りで指定します（値にカンマが入るので、`--set-env-vars "^#^DEPLOY_TARGETS=firebase,cloudflare-pages#KEY=値"` のように、区切り文字を `@` に変えて渡します）（既定は `github-pages`）。各公開先は独立に実行され、1 つ失敗しても残りは実行されます（終了コードは失敗になります）。

| 値 | 公開先 | 必要な設定 |
|---|---|---|
| `github-pages` | `g-i-t-data` の `gh-pages` ブランチ（従来どおり。1 コミットに保つ） | なし（`GH_PAT` を使う） |
| `github-pages-redirect` | 同じ `gh-pages` ブランチを、**新しい場所へ転送するページ**にする（独自ドメインへの移行後の旧 URL 用） | `SITE_BASE_URL` に**新しいアドレス**を設定する（`github.io` を指すとエラー） |
| `firebase` | Firebase Hosting | `FIREBASE_PROJECT`（サービスアカウントに `roles/firebasehosting.admin`） |
| `cloudflare-pages` | Cloudflare Pages（`wrangler pages deploy`） | `CLOUDFLARE_API_TOKEN`（Pages の編集権限のみ）、`CLOUDFLARE_ACCOUNT_ID`、`CLOUDFLARE_PAGES_PROJECT`（先に `wrangler pages project create <名前>` で作成） |

- **移行のとき:** 切り替え先を `DEPLOY_TARGETS` に足して、しばらく両方に出し、`xxx.web.app` / `xxx.pages.dev` で表示を確認してから、DNS（Route 53）の向き先を切り替えるのが安全です。
- **Cloudflare は Pages を使います**（Workers の静的アセットではなく）。DNS が Route 53 のままだと、Workers のカスタムドメインは使えない見込みです（ゾーンが Cloudflare にある必要）。Pages なら、サブドメインへの CNAME だけで外部 DNS のまま使えます。
- Cloudflare Pages の無料プランには、**1 デプロイあたり 20,000 ファイルまで**の上限があります（要確認）。差分ページが増え続けるので、将来は上限に近づくおそれがあります。
- **DNS のラウンドロビンで 2 つの公開先に負荷を分けることは、お勧めしません。** それぞれが、自分の証明書（Let's Encrypt など）を、そのドメインへのアクセスで検証して発行・更新するため、問い合わせがもう片方に振られると、検証に失敗します。また、Firebase は A レコード、Cloudflare Pages は CNAME を求めるので、同じ名前に並べられません。負荷分散が目的なら、Cloudflare を手前に置く（DNS を Cloudflare に移す）ほうが確実です。

## 7. 独自ドメイン（サブドメイン）

1. Firebase コンソール → Hosting → 「カスタムドメインを追加」→ `$DOMAIN`。
2. 表示された **TXT**（所有確認）と **A**（または表示された種類）のレコードを、**Route 53** のホストゾーンに追加。
3. SSL 証明書は、検証後に自動で発行されます（数分〜数時間かかることがあります）。

公開後は、次を直します。

- `SITE_BASE_URL` は 5 で設定済みです（Atom の絶対 URL）。
- GA4 のデータストリームの URL を、新しいドメインに変える（使っている場合）。
- **フォローの選択（`localStorage`）はドメインが変わると引き継がれません。** 利用者に「フォロー共有」の `?follow=` リンクで書き出してもらうよう、案内してください。

## 8. 転送量の監視（無料枠は月 10 GB）

```bash
# ダッシュボード
gcloud monitoring dashboards create --config-from-file=monitoring/hosting-dashboard.json

# 通知先（メール）
gcloud beta monitoring channels create --display-name="G醫t" --type=email \
  --channel-labels=email_address=<あなたのメールアドレス>
gcloud beta monitoring channels list --format='value(name)'     # projects/.../notificationChannels/NNN

# アラート（直近 24 時間の送信量が 300 MB 超）
gcloud alpha monitoring policies create --policy-from-file=monitoring/hosting-alert.json \
  --notification-channels=<上の name>
```

- 指標は `firebasehosting.googleapis.com/network/sent_bytes_count`（リソース `firebase_domain`）です。**初めに、Cloud Monitoring の Metrics Explorer で、この指標が出ていることを確認してください。** 出ていなければ、リソースの種類やフィルタを直す必要があります。
- **無料枠は「月 10 GB」です**（公式の料金ページ <https://firebase.google.com/docs/hosting/usage-quotas-pricing> で確認。以前の「1 日 360 MB」という数字は古い、または誤りでした）。ストレージも 10 GB まで無料です。
- アラートは「直近 24 時間」の移動窓で、しきい値は 300 MiB（314,572,800 バイト）です。月 10 GB の日割りは約 333 MB なので、**このペースが続くと月の枠を超える、という早期の警告**として使います。月の累計は、Firebase コンソールの Hosting > 使用状況で見ます。
- 超えたあとの扱い: **Spark（請求先なし）は、猶予期間のあと、翌月までサイトが無効になります**（課金はされません）。**Blaze（請求先あり）は、超過分が 1 GB あたり $0.15、ストレージは超過分が 1 GB あたり月 $0.026 です**。止まる代わりに課金されます。

## 9. アーカイブワーカー

同じイメージで `archive` も動かせます（`g-i-t-archive` というジョブを同様に作り、引数に `archive` を渡す）。ただし、1 件あたり約 75 秒を待つ処理なので、Cloud Run の課金には向きません。手元の Linux で `scripts/drain-archive-local.sh` を動かすほうを勧めます。

```bash
gcloud run jobs create g-i-t-archive --image $IMAGE --region $REGION --args=archive \
  --service-account $SA@$PROJECT.iam.gserviceaccount.com --max-retries 0 --task-timeout 1200s \
  --set-secrets SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_KEY=SUPABASE_KEY:latest,IA_ACCESS_KEY=IA_ACCESS_KEY:latest,IA_SECRET_KEY=IA_SECRET_KEY:latest
```

## 手元での確認

```bash
# リポジトリのルートで実行する（container/ の中だと requirements.txt が見つからない）。どこからでも: pwsh container/build.ps1 firebase
docker build -f container/Dockerfile --target firebase -t g-i-t-app:firebase .      # Cloudflare 版は --target cloudflare
# 作業中のコードで試すときは、-v "$PWD:/app" を付ける（付けないと GitHub の main を取得する）
cp .env.sample .env      # 値を記入する（.env は git・docker の対象外）
docker run --rm --env-file .env g-i-t-app:firebase     # wslc でも同じ: wslc build -f … / wslc run --rm --env-file .env g-i-t-app:firebase
```

デプロイ（最後の手順）は、認証がないので失敗します。そこまでの clone・取得・push の動作を確認できます。**これは本物の g-i-t-data に push するので、確認用のリポジトリを `-e DATA_REPO=<owner>/<repo>` で指定してください。**
