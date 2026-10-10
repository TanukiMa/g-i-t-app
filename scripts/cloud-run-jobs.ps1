# Creates or updates the Cloud Run jobs of G-I-T from ONE set of settings, so that they cannot drift apart
# (the dashboard that remake-dashboard builds must use the same SITE_BASE_URL, DEPLOY_TARGETS, STALK_RUNS ... as stalk).
#
#   pwsh scripts/cloud-run-jobs.ps1 -DryRun                       # shows what would be done, changes nothing
#   pwsh scripts/cloud-run-jobs.ps1                               # stalk + remake-dashboard (create, or update when they exist)
#   pwsh scripts/cloud-run-jobs.ps1 -Jobs resummarize             # one job
#   pwsh scripts/cloud-run-jobs.ps1 -SiteBaseUrl https://giiit.goudge.org/ -DeployTargets github-pages-redirect,firebase
#
# Jobs (the image's default command is "stalk"; the others pass the mode as the argument):
#   g-i-t-stalk             the full pipeline (started by Cloud Scheduler)
#   g-i-t-remake-dashboard  rebuild and publish the dashboard only: no fetching, no git push, no AI
#   g-i-t-resummarize       regenerate stored summaries (give the options at run time:
#                           gcloud run jobs execute g-i-t-resummarize --args="resummarize,--dry-run" ...)
#   g-i-t-recover           recover the updates swallowed by Re-baseline commits (pushes diff pages to g-i-t-data;
#                           gcloud run jobs execute g-i-t-recover --args="recover,--dry-run" ...)
# SUPABASE_URL and WEBSITE_STALKER_FROM are read from the .env file (never printed). The secrets GH_PAT, GEMINI_API_KEY and
# SUPABASE_KEY must exist in Secret Manager of the project. NOTE: --set-env-vars REPLACES the whole list of a job; put every
# extra variable you want to keep (GA_MEASUREMENT_ID ...) into -ExtraEnv.
param(
    [string]$Project = 'gi-i-it',
    [string]$Region = 'asia-northeast1',
    [string]$WebProject = 'g-i-t-web',
    [string]$ServiceAccount = 'g-i-t-runner',
    [string]$ImageTag = 'firebase',
    [string]$SiteBaseUrl = 'https://tanukima.github.io/g-i-t-data/',
    [string[]]$DeployTargets = @('github-pages', 'firebase'),
    [string]$StalkRuns = '07:30,12:30,16:30',
    [string]$PwaEnabled = '0',
    [string]$PwaPassphraseHash = '',
    [string[]]$ExtraEnv = @(),
    [string[]]$Jobs = @('stalk', 'remake-dashboard'),
    [string]$EnvFile = '.env',
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
# "-File" hands a comma list over as ONE string ("a,b"): split it here
$Jobs = @($Jobs | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
$DeployTargets = @($DeployTargets | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
foreach ($j in $Jobs) { if ($j -notin 'stalk', 'remake-dashboard', 'resummarize', 'recover') { throw "unknown job '$j' (stalk, remake-dashboard, resummarize, recover)" } }

function Read-EnvValue([string]$name) {
    if (-not (Test-Path $EnvFile)) { throw "$EnvFile not found (run from the repository root, or pass -EnvFile)" }
    $line = Get-Content $EnvFile | Where-Object { $_ -match "^$name=" } | Select-Object -First 1
    if (-not $line) { throw "$name is not set in $EnvFile" }
    return ($line -split '=', 2)[1]
}

$image = "$Region-docker.pkg.dev/$Project/git/g-i-t-app:$ImageTag"
$sa = "$ServiceAccount@$Project.iam.gserviceaccount.com"
$supabaseUrl = Read-EnvValue 'SUPABASE_URL'

# "^#^" makes # the separator of the list, because the values contain commas (DEPLOY_TARGETS) and @ (the e-mail address)
function New-EnvArg([bool]$withFrom) {
    $pairs = @(
        "FIREBASE_PROJECT=$WebProject",
        "SITE_BASE_URL=$SiteBaseUrl",
        "DEPLOY_TARGETS=$($DeployTargets -join ',')",
        "STALK_RUNS=$StalkRuns",
        "PWA_ENABLED=$PwaEnabled",
        "SUPABASE_URL=$supabaseUrl"
    )
    if ($PwaPassphraseHash) { $pairs += "PWA_PASSPHRASE_HASH=$PwaPassphraseHash" }
    if ($withFrom) { $pairs += "WEBSITE_STALKER_FROM=$(Read-EnvValue 'WEBSITE_STALKER_FROM')" }
    $pairs += $ExtraEnv
    return '^#^' + ($pairs -join '#')
}

$definitions = @{
    'stalk' = @{ Name = 'g-i-t-stalk'; Args = $null; Timeout = '3000s'; From = $true
                 Secrets = 'GH_PAT=GH_PAT:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,SUPABASE_KEY=SUPABASE_KEY:latest' }
    'remake-dashboard' = @{ Name = 'g-i-t-remake-dashboard'; Args = 'remake-dashboard'; Timeout = '900s'; From = $false
                 Secrets = 'GH_PAT=GH_PAT:latest,SUPABASE_KEY=SUPABASE_KEY:latest' }
    'resummarize' = @{ Name = 'g-i-t-resummarize'; Args = 'resummarize'; Timeout = '1800s'; From = $false
                 Secrets = 'GH_PAT=GH_PAT:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,SUPABASE_KEY=SUPABASE_KEY:latest' }
    'recover' = @{ Name = 'g-i-t-recover'; Args = 'recover'; Timeout = '2400s'; From = $false
                 Secrets = 'GH_PAT=GH_PAT:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,SUPABASE_KEY=SUPABASE_KEY:latest' }
}

foreach ($key in $Jobs) {
    $d = $definitions[$key]
    gcloud run jobs describe $d.Name --region $Region --project $Project --format 'value(name)' 2>$null | Out-Null
    $exists = ($LASTEXITCODE -eq 0)
    $verb = if ($exists) { 'update' } else { 'create' }
    $cli = @('run', 'jobs', $verb, $d.Name, '--image', $image, '--region', $Region, '--project', $Project,
             '--service-account', $sa, '--cpu', '1', '--memory', '1Gi', '--max-retries', '0', '--task-timeout', $d.Timeout,
             '--set-env-vars', (New-EnvArg $d.From), '--set-secrets', $d.Secrets)
    if ($d.Args) { $cli += @('--args', $d.Args) }
    $shown = ($cli | ForEach-Object { if ($_ -like '^#^*') { '"<env: ' + ($_.Split('#').Count - 1) + ' variables, values not shown>"' } else { $_ } }) -join ' '
    Write-Host "[$verb] gcloud $shown"
    if (-not $DryRun) {
        & gcloud @cli
        if ($LASTEXITCODE -ne 0) { throw "gcloud run jobs $verb $($d.Name) failed (exit $LASTEXITCODE)" }
    }
}
if ($DryRun) { Write-Host 'Dry run: nothing was changed.' }
