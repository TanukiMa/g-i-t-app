<#
.SYNOPSIS
  Registers the e-mail notification channel and the alert policies of monitoring/*.json through the Cloud Monitoring
  REST API (the gcloud "alpha"/"beta" components are not needed). Safe to run again: a channel or policy with the same
  display name is updated, not duplicated.

.EXAMPLE
  pwsh scripts/register-alerts.ps1 -Email mguuji+giiit@goudge.org -DryRun
  pwsh scripts/register-alerts.ps1 -Email mguuji+giiit@goudge.org
#>
param(
  [Parameter(Mandatory = $true)][string]$Email,
  [string]$Project = 'gi-i-it',
  [string]$ChannelName = 'G醫t',
  [string[]]$Policies = @('scheduler-alert.json', 'run-failed-alert.json', 'stalk-absent-alert.json', 'hosting-alert.json'),
  [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$api = "https://monitoring.googleapis.com/v3/projects/$Project"
$dir = Join-Path (Split-Path $PSScriptRoot -Parent) 'monitoring'

$token = (& gcloud auth print-access-token).Trim()
$headers = @{ Authorization = "Bearer $token"; 'x-goog-user-project' = $Project }

function Call($method, $url, $body) {
  $args = @{ Method = $method; Uri = $url; Headers = $headers; ContentType = 'application/json; charset=utf-8' }
  if ($null -ne $body) { $args.Body = [Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Depth 20)) }
  Invoke-RestMethod @args
}

# 1. Notification channel
$channels = (Call 'GET' "$api/notificationChannels" $null).notificationChannels
$channel = $channels | Where-Object { $_.displayName -eq $ChannelName -and $_.type -eq 'email' -and $_.labels.email_address -eq $Email } | Select-Object -First 1
if ($channel) {
  Write-Host "Channel exists: $($channel.name)"
} elseif ($DryRun) {
  Write-Host "DRY RUN: would create e-mail channel '$ChannelName' -> $Email"
} else {
  $channel = Call 'POST' "$api/notificationChannels" @{
    type = 'email'; displayName = $ChannelName; labels = @{ email_address = $Email }; enabled = $true }
  Write-Host "Channel created: $($channel.name)"
}

# 2. Alert policies
$existing = (Call 'GET' "$api/alertPolicies" $null).alertPolicies
foreach ($file in $Policies) {
  $policy = Get-Content (Join-Path $dir $file) -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
  if ($channel) { $policy.notificationChannels = @($channel.name) }
  $same = $existing | Where-Object { $_.displayName -eq $policy.displayName } | Select-Object -First 1
  if ($DryRun) {
    Write-Host ("DRY RUN: would " + $(if ($same) { 'update' } else { 'create' }) + " policy '$($policy.displayName)'")
  } elseif ($same) {
    $null = Call 'PATCH' "https://monitoring.googleapis.com/v3/$($same.name)" $policy
    Write-Host "Policy updated: $($policy.displayName)"
  } else {
    $null = Call 'POST' "$api/alertPolicies" $policy
    Write-Host "Policy created: $($policy.displayName)"
  }
}
Write-Host 'Done. Test: a failing scheduler attempt or the "Send test notification" button of the channel in the console.'
