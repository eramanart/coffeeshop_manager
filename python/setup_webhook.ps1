# setup_webhook.ps1 — register the Telegram webhook for CoffeeManager-OS
# Run from coffee_agent\python\ after your server is reachable over HTTPS:
#
#   .\setup_webhook.ps1 -Url https://your.server.com
#
# For local testing with ngrok:
#   ngrok http 8000           (in a separate terminal)
#   .\setup_webhook.ps1 -Url https://xxxx.ngrok-free.app

param(
    [Parameter(Mandatory=$true)]
    [string]$Url
)

# Load token from settings.env two levels up
$envFile = Join-Path $PSScriptRoot "..\settings.env"
Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*TELEGRAM_BOT_TOKEN\s*=\s*(.+)') {
        $env:TELEGRAM_BOT_TOKEN = $Matches[1].Trim()
    }
}

if (-not $env:TELEGRAM_BOT_TOKEN) {
    Write-Error "TELEGRAM_BOT_TOKEN not found in settings.env"
    exit 1
}

$webhookUrl = "$($Url.TrimEnd('/'))/webhook"
$apiUrl     = "https://api.telegram.org/bot$($env:TELEGRAM_BOT_TOKEN)/setWebhook"
$body       = @{ url = $webhookUrl } | ConvertTo-Json

Write-Host "Registering webhook: $webhookUrl"

$response = Invoke-RestMethod -Uri $apiUrl -Method Post `
    -Body $body -ContentType "application/json"

if ($response.ok) {
    Write-Host "Webhook registered successfully."
    Write-Host "Description: $($response.description)"
} else {
    Write-Error "Failed: $($response | ConvertTo-Json)"
}

# Verify
$info = Invoke-RestMethod -Uri "https://api.telegram.org/bot$($env:TELEGRAM_BOT_TOKEN)/getWebhookInfo"
Write-Host "`nCurrent webhook info:"
Write-Host "  URL:             $($info.result.url)"
Write-Host "  Pending updates: $($info.result.pending_update_count)"
Write-Host "  Last error:      $($info.result.last_error_message)"
