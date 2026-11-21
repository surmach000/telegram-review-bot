param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$resultPath = Join-Path $PSScriptRoot 'gitlab-check-result.txt'
'WAITING_FOR_TOKEN' | Set-Content -LiteralPath $resultPath -Encoding UTF8
Write-Host 'GitLab token check only. No Telegram messages or repository changes.'
Write-Host 'Enter a token with read_api and read_repository for the three EDO projects.'
$secureToken = Read-Host 'GitLab token (hidden)' -AsSecureString
try {
    $credential = New-Object System.Management.Automation.PSCredential('token', $secureToken)
    $env:REVIEW_GITLAB_TOKEN = $credential.GetNetworkCredential().Password
    & $Python '.\check-gitlab.py' | Tee-Object -FilePath $resultPath
    $checkExitCode = $LASTEXITCODE
    if ($checkExitCode -eq 0) {
        $secureToken | Export-Clixml -LiteralPath '.\gitlab-token.clixml'
        Write-Host 'Verified token saved using Windows DPAPI.'
    } else {
        Write-Host 'Token not saved. Check VPN, token validity, permissions and project membership.'
    }
} finally {
    Remove-Item Env:\REVIEW_GITLAB_TOKEN -ErrorAction SilentlyContinue
    $credential = $null
    $secureToken = $null
}
