param([int]$Port = 8765, [switch]$ResetDemo)
$ErrorActionPreference = 'Stop'
$taskPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskPython = (Get-Command python -ErrorAction Stop).Source
}
Push-Location $PSScriptRoot
try {
    if ($ResetDemo) {
        & $taskPython (Join-Path $PSScriptRoot 'server.py') --port $Port --reset
    } else {
        & $taskPython (Join-Path $PSScriptRoot 'server.py') --port $Port
    }
} finally {
    Pop-Location
}
