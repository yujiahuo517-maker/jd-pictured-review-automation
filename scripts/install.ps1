param([switch]$Force)

$ErrorActionPreference = 'Stop'
$source = Split-Path -Parent $PSScriptRoot
$skillsHome = Join-Path $env:USERPROFILE '.codex\skills'
$destination = Join-Path $skillsHome 'jd-pictured-review-automation'

New-Item -ItemType Directory -Path $skillsHome -Force | Out-Null

if ((Test-Path -LiteralPath $destination) -and -not $Force) {
    Write-Host "目标已存在：$destination"
    Write-Host '如需覆盖，请在 PowerShell 执行：.\scripts\install.ps1 -Force'
    exit 2
}

if (Test-Path -LiteralPath $destination) {
    $backup = "$destination.backup-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Move-Item -LiteralPath $destination -Destination $backup
    Write-Host "旧版本已备份：$backup"
}

Copy-Item -LiteralPath $source -Destination $destination -Recurse
& (Join-Path $destination 'scripts\register-mcp.ps1')
Write-Host "安装完成：$destination"
Write-Host '请重启 Codex，然后使用 $jd-pictured-review-automation。'
