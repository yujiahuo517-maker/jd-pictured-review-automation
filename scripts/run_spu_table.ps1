param(
    [string]$Excel = "$env:USERPROFILE\Desktop\SPU.xlsx",
    [string]$WorkDir = "$env:USERPROFILE\Desktop\jd-pictured-review-run",
    [int]$DisplayLimit = 5,
    [int]$SpuLimit = 100
)

$ErrorActionPreference = 'Stop'
$skillRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $skillRoot 'runtime\python\python.exe'
$entry = Join-Path $PSScriptRoot 'run_spu_table.py'
$env:PYTHONUTF8 = '1'
$env:BLACKLIGHT_HOME = Join-Path $skillRoot 'runtime\state'

if (-not (Test-Path -LiteralPath $python)) {
    throw "便携 Python 缺失：$python。请重新完整解压 ZIP。"
}
if (-not (Test-Path -LiteralPath $Excel)) {
    throw "SPU 表不存在：$Excel。请把文件放到桌面并命名为 SPU.xlsx，或用 -Excel 指定路径。"
}

& $python $entry --xlsx $Excel --work-dir $WorkDir --display-limit $DisplayLimit --spu-limit $SpuLimit
exit $LASTEXITCODE
