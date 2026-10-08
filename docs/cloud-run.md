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

構成ファイル: `Dockerfile` / `container/entrypoint.sh` / `container/firebase.json` / `monitoring/*.json`

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
gcloud builds submit --tag $IMAGE .      # 初回は Rust のビルドで 10 分ほどかかる
# 公開先に Firebase / Cloudflare を使うときだけ、そのツールを、イメージに入れる（既定は、入れない。入れると、約 250 MB / 約 170 MB 増える）:
#   gcloud builds submit --tag $IMAGE --substitutions=...  /  docker build --build-arg INSTALL_FIREBASE=1 --build-arg INSTALL_WRANGLER=1 .
```

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
  --set-env-vars FIREBASE_PROJECT=$PROJECT,SITE_BASE_URL=https://$DOMAIN/ \
  --set-secrets GH_PAT=GH_PAT:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_KEY=SUPABASE_KEY:latest,WEBSITE_STALKER_FROM=WEBSITE_STALKER_FROM:latest

gcloud run jobs execute g-i-t-stalk --region $REGION --wait     # 手動で 1 回
gcloud run jobs executions list --job g-i-t-stalk --region $REGION
```

- タイムアウトを **50 分**にしているのは、実行どうしが重ならないようにするためです（1 日 4 回なら、間隔は、4 時間以上あるので、もっと長くしても構いません）（重なると、同じ g-i-t-data に同時に push して競合します）。
- GA4 / Cloudflare のタグ、Gemini のモデル順、フォールバック LLM は、`--set-env-vars` に `GA_MEASUREMENT_ID` などを足して渡します（`docs/wiki/Configuration.md`）。

## 6. スケジュール（Cloud Scheduler）

```bash
PROJECT_NUMBER=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
gcloud run jobs add-iam-policy-binding g-i-t-stalk --region $REGION \
  --member=serviceAccount:$SA@$PROJECT.iam.gserviceaccount.com --role=roles/run.invoker

# 1 日 4 回: 朝 8:30、昼 12:30、夕 16:30、夜 22:00（日本時間）。1 本の cron では書けないので、2 つのジョブにする
URI="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_NUMBER/jobs/g-i-t-stalk:run"
gcloud scheduler jobs create http g-i-t-stalk-day --location $REGION \
  --schedule "30 8,12,16 * * *" --time-zone Asia/Tokyo \
  --http-method POST --uri "$URI" \
  --oauth-service-account-email $SA@$PROJECT.iam.gserviceaccount.com
gcloud scheduler jobs create http g-i-t-stalk-night --location $REGION \
  --schedule "0 22 * * *" --time-zone Asia/Tokyo \
  --http-method POST --uri "$URI" \
  --oauth-service-account-email $SA@$PROJECT.iam.gserviceaccount.com
```

スケジュールは、1 日 4 回（日本時間 8:30・12:30・16:30・22:00）です。**アーカイブ（`archive.yml`）は、Cloud Run に移さず、GitHub Actions のままにします**（約 50 分後の 4 回、JST 9:20・13:20・17:20・22:50）。1 件ごとに 15〜60 秒の待ちがあり、Cloud Run では、待ちも課金されるためです。GitHub Actions の `stalk.yml`（`cron` は UTC で書く）とは、別に、Cloud Scheduler 側で時刻帯を指定できます。Cloud Scheduler は、時刻どおりに起動します（GitHub の `schedule` のような遅れは、ありません）。

```bash
gh workflow disable stalk.yml --repo TanukiMa/g-i-t-app
```

## 6b. 公開先を選ぶ（`DEPLOY_TARGETS`）

同じイメージが、次の公開先のどれにでも（複数同時にも）デプロイできます。`--set-env-vars DEPLOY_TARGETS=firebase,cloudflare-pages` のように、カンマ区切りで指定します（既定は `github-pages`）。各公開先は独立に実行され、1 つ失敗しても残りは実行されます（終了コードは失敗になります）。

| 値 | 公開先 | 必要な設定 |
|---|---|---|
| `github-pages` | `g-i-t-data` の `gh-pages` ブランチ（従来どおり。1 コミットに保つ） | なし（`GH_PAT` を使う） |
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

## 8. 転送量の監視（無料枠は 1 日 360 MB）

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
- 窓は「直近 24 時間」の移動窓です（暦日ごとではありません）。しきい値は 300 MiB（314,572,800 バイト）です。
- 1 日の上限（360 MB）を超えたあとの扱いは、プランによります。有料（Blaze）にすると、超過分は従量課金になります。

## 9. アーカイブワーカー

同じイメージで `archive` も動かせます（`g-i-t-archive` というジョブを同様に作り、引数に `archive` を渡す）。ただし、1 件あたり約 75 秒を待つ処理なので、Cloud Run の課金には向きません。手元の Linux で `scripts/drain-archive-local.sh` を動かすほうを勧めます。

```bash
gcloud run jobs create g-i-t-archive --image $IMAGE --region $REGION --args=archive \
  --service-account $SA@$PROJECT.iam.gserviceaccount.com --max-retries 0 --task-timeout 1200s \
  --set-secrets SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_KEY=SUPABASE_KEY:latest,IA_ACCESS_KEY=IA_ACCESS_KEY:latest,IA_SECRET_KEY=IA_SECRET_KEY:latest
```

## 手元での確認

```bash
docker build -t g-i-t-app .
# 作業中のコードで試すときは、-v "$PWD:/app" を付ける（付けないと GitHub の main を取得する）
cp .env.sample .env      # 値を記入する（.env は git・docker の対象外）
docker run --rm --env-file .env g-i-t-app          # wslc でも同じ: wslc run --rm --env-file .env g-i-t-app
```

デプロイ（最後の手順）は、認証がないので失敗します。そこまでの clone・取得・push の動作を確認できます。**これは本物の g-i-t-data に push するので、確認用のリポジトリを `-e DATA_REPO=<owner>/<repo>` で指定してください。**
