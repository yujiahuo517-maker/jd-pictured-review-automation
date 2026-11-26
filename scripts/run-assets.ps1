param(
    [Parameter(Mandatory=$true)][string]$Workspace,
    [ValidateSet('prepare','probe','login','run','report')][string]$Mode = 'run',
    [string]$RuntimeRoot = '',
    [int]$Limit = 0
)
$ErrorActionPreference = 'Stop'
if (-not $RuntimeRoot) {
    $localAssetRoot = Split-Path -Parent $PSScriptRoot
    if (Test-Path -LiteralPath (Join-Path $localAssetRoot 'runtime\python\python.exe')) {
        $RuntimeRoot = $localAssetRoot
    } else {
        $RuntimeRoot = Join-Path $env:USERPROFILE '.joycode\skills\jd-pictured-review-automation'
    }
}
$assetPython = Join-Path $RuntimeRoot 'runtime\python\python.exe'
if (-not (Test-Path -LiteralPath $assetPython)) { throw 'Portable Python is missing. Install the original runtime first.' }
$env:PYTHONUTF8 = '1'
& $assetPython -X utf8 -u (Join-Path $PSScriptRoot 'maintain_assets.py') $Mode --workspace $Workspace --runtime-root $RuntimeRoot --limit $Limit
exit $LASTEXITCODE
