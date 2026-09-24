$ErrorActionPreference = 'Stop'
$skillRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $skillRoot 'runtime\python\python.exe'
$env:PYTHONUTF8 = '1'
$env:BLACKLIGHT_HOME = Join-Path $skillRoot 'runtime\state'

if (-not (Test-Path -LiteralPath $python)) {
    throw "便携 Python 缺失：$python。请重新完整解压 ZIP。"
}

& $python -m blacklight.core.login
exit $LASTEXITCODE
