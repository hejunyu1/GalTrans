$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"

[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    Write-Error "未找到项目虚拟环境。请先在项目根目录创建 .venv。"
    exit 1
}

Push-Location $projectRoot
try {
    $env:PYTHONPATH = Join-Path $projectRoot "src"
    & $pythonPath -m unittest discover -s tests -t . -v
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
