param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (Test-Path -LiteralPath '.\config.local.json') {
    throw 'config.local.json already exists. Edit it explicitly; setup will not overwrite it.'
}
$botToken = Read-Host 'Token from BotFather (hidden)' -AsSecureString
# DPAPI encryption: readable only under this Windows account on this machine.
$botToken | Export-Clixml -LiteralPath '.\telegram-token.clixml'
$config = Get-Content -LiteralPath '.\config.example.json' -Raw -Encoding UTF8 | ConvertFrom-Json
Write-Host 'Ask the colleague to send /start to the new bot. Keep this setup window open.'
Read-Host 'Press Enter after the colleague has sent /start' | Out-Null
& powershell -NoProfile -ExecutionPolicy Bypass -File '.\start-windows.ps1' -Action discover -Python $Python
if ($LASTEXITCODE -ne 0) { throw 'Could not discover the colleague ID. Tokens are saved; retry discover explicitly.' }
$config.allowed_user_id = [long](Read-Host 'Numeric Telegram user ID of the permitted colleague')
if ($config.allowed_user_id -le 0) { throw 'A positive numeric Telegram user ID is required.' }
$config.codex_command = (Get-Command codex -ErrorAction Stop).Source
$config | Add-Member -NotePropertyName accept_after -NotePropertyValue ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
New-Item -ItemType Directory -Path '.\documentation' -Force | Out-Null
$config | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath '.\config.local.json' -Encoding UTF8
& $Python -m reviewbot prepare-docs
if ($LASTEXITCODE -ne 0) { throw 'Documentation setup failed. Check paths in config.local.json.' }
& $Python '.\prepare-test-cache.py'
if ($LASTEXITCODE -ne 0) { throw 'Maven artifact cache preparation failed.' }
Write-Host 'Prepared. Nothing is running. Start with .\start-windows.ps1'
