$taskRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$ollamaExe = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
if (!(Test-Path -LiteralPath $ollamaExe)) { throw 'Install Ollama from ollama.com first.' }
$env:OLLAMA_HOST = '127.0.0.1:11435'
$env:OLLAMA_MODELS = Join-Path $taskRoot 'runtime\models\ollama'
$env:OLLAMA_NUM_PARALLEL = '1'
$env:OLLAMA_MAX_LOADED_MODELS = '1'
$env:OLLAMA_CONTEXT_LENGTH = '8192'
New-Item -ItemType Directory -Force -Path $env:OLLAMA_MODELS | Out-Null
try { Invoke-RestMethod 'http://127.0.0.1:11435/api/tags' -TimeoutSec 3 | Out-Null }
catch {
    Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskRoot 'runtime\ollama-embedding.out.log') -RedirectStandardError (Join-Path $taskRoot 'runtime\ollama-embedding.err.log')
}
Write-Output 'Models at 127.0.0.1:11435. On first setup run: ollama pull qwen3-embedding:0.6b; ollama pull qwen3.5:4b (with OLLAMA_HOST/OLLAMA_MODELS above).'
