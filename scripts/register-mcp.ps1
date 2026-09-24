$ErrorActionPreference = 'Stop'
$skillRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $skillRoot 'runtime\python\python.exe'
$state = Join-Path $skillRoot 'runtime\state'
$server = Join-Path $PSScriptRoot 'mcp_server.py'

if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    Write-Warning '未找到 codex 命令，跳过 MCP 注册；仍可使用 scripts\run.ps1。'
    exit 0
}

& codex mcp remove blacklight-osw 2>$null
& codex mcp add blacklight-osw --env "BLACKLIGHT_HOME=$state" --env 'PYTHONUTF8=1' --env 'PYTHONIOENCODING=utf-8' -- $python $server
if ($LASTEXITCODE -ne 0) {
    throw 'blacklight-osw MCP 注册失败。'
}

Write-Host 'blacklight-osw MCP 注册完成。重启 Codex 后生效。'
