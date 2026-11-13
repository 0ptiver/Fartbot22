# Copy Nova's notes (vault\Nova in this repo) into the Obsidian vault.
#   powershell -ExecutionPolicy Bypass -File scripts\vault.ps1
# Only Nova's own notes are replaced; anything else in the vault folder is left alone.
param([string]$Target = "$env:USERPROFILE\ObsidianClaude\Nova")
$ErrorActionPreference = "Stop"
$source = Join-Path (Split-Path $PSScriptRoot -Parent) "vault\Nova"
New-Item -ItemType Directory -Force -Path $Target | Out-Null
Copy-Item -Path (Join-Path $source "*") -Destination $Target -Recurse -Force
$count = (Get-ChildItem -Path $source -Recurse -Filter *.md).Count
Write-Host "Copied $count Nova notes to $Target. Open 'Nova Home' in Obsidian to start." -ForegroundColor Green
