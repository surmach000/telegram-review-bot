param(
    [ValidateSet('run', 'stop', 'discover', 'queue', 'deliveries', 'retry-delivery', 'ack-delivery', 'prepare-docs')]
    [string]$Action = 'run',
    [string]$Python = 'python',
    [int]$DeliveryId = 0,
    [long]$MessageId = 0
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
try {
    if (Test-Path -LiteralPath '.\telegram-token.clixml') {
            $secureValue = Import-Clixml -LiteralPath '.\telegram-token.clixml'
            $credential = New-Object System.Management.Automation.PSCredential('token', $secureValue)
            $env:REVIEW_TELEGRAM_TOKEN = $credential.GetNetworkCredential().Password
    }
    $arguments = @('-m', 'reviewbot', $Action)
    if ($DeliveryId -gt 0) { $arguments += @('--id', $DeliveryId) }
    if ($MessageId -gt 0) { $arguments += @('--message-id', $MessageId) }
    & $Python @arguments
    exit $LASTEXITCODE
} finally {
    Remove-Item Env:\REVIEW_TELEGRAM_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\REVIEW_GITLAB_TOKEN -ErrorAction SilentlyContinue
}
