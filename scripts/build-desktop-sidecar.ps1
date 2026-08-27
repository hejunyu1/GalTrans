$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$requirementsPath = Join-Path $projectRoot "requirements-build.txt"
$sourcePath = Join-Path $projectRoot "src\galtrans\desktop_bridge.py"
$sourceRoot = Join-Path $projectRoot "src"
$binaryDirectory = Join-Path $projectRoot "desktop\src-tauri\binaries"

$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    Write-Error "未找到项目虚拟环境。请先在项目根目录创建 .venv。"
    exit 1
}

$versionLine = Get-Content -LiteralPath $requirementsPath |
    Where-Object { $_ -match '^pyinstaller==([0-9]+(?:\.[0-9]+)*)$' }
if (@($versionLine).Count -ne 1) {
    Write-Error "requirements-build.txt 必须固定一个 PyInstaller 版本。"
    exit 1
}
$expectedVersion = [regex]::Match($versionLine, '==(.+)$').Groups[1].Value
$ErrorActionPreference = "Continue"
$actualVersion = (& $pythonPath -m PyInstaller --version 2>$null | Out-String).Trim()
$versionExitCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($versionExitCode -ne 0 -or $actualVersion -ne $expectedVersion) {
    Write-Error (
        "需要 PyInstaller {0}。请运行：.\.venv\Scripts\python.exe -m pip install -r .\requirements-build.txt" -f
        $expectedVersion
    )
    exit 1
}

$rustCommand = Get-Command rustc.exe -ErrorAction SilentlyContinue
if ($null -eq $rustCommand) {
    $rustPath = Join-Path $env:USERPROFILE ".cargo\bin\rustc.exe"
}
else {
    $rustPath = $rustCommand.Source
}
if (-not (Test-Path -LiteralPath $rustPath -PathType Leaf)) {
    Write-Error "未找到 Rust 编译器，无法确定 Tauri sidecar 目标三元组。"
    exit 1
}

$targetTriple = (& $rustPath --print host-tuple | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $targetTriple -notmatch '^[A-Za-z0-9_.-]+-windows-msvc$') {
    Write-Error "当前只支持在 Windows MSVC 主机上生成 sidecar：$targetTriple"
    exit 1
}

$systemTempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$buildRoot = Join-Path $systemTempRoot ("galtrans-sidecar-build-" + [guid]::NewGuid().ToString("N"))
$resolvedBuildRoot = [System.IO.Path]::GetFullPath($buildRoot)
if (-not $resolvedBuildRoot.StartsWith($systemTempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-Error "sidecar 临时构建目录不在系统临时目录内。"
    exit 1
}

try {
    $distPath = Join-Path $resolvedBuildRoot "dist"
    $workPath = Join-Path $resolvedBuildRoot "work"
    $specPath = Join-Path $resolvedBuildRoot "spec"
    New-Item -ItemType Directory -Path $distPath, $workPath, $specPath -Force | Out-Null

    $ErrorActionPreference = "Continue"
    & $pythonPath -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --console `
        --noupx `
        --name "galtrans-backend" `
        --paths $sourceRoot `
        --distpath $distPath `
        --workpath $workPath `
        --specpath $specPath `
        $sourcePath
    $installerExitCode = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($installerExitCode -ne 0) {
        Write-Error "PyInstaller 未能生成 GalTrans sidecar。"
        exit 1
    }

    $builtBinary = Join-Path $distPath "galtrans-backend.exe"
    if (-not (Test-Path -LiteralPath $builtBinary -PathType Leaf)) {
        Write-Error "PyInstaller 没有生成预期的 sidecar 文件。"
        exit 1
    }

    $smokeSecret = "galtrans-sidecar-smoke-secret"
    $smokeRequest = @{
        schema_version = 2
        operation = "translate"
        sdk_path = Join-Path $resolvedBuildRoot "missing-sdk"
        project_path = Join-Path $resolvedBuildRoot "missing-project"
        output_path = Join-Path $resolvedBuildRoot "unused-output"
        endpoint = "https://provider.invalid/v1/chat/completions"
        model = "sidecar-smoke-test"
        api_key = $smokeSecret
    } | ConvertTo-Json -Compress
    $smokeLines = @($smokeRequest | & $builtBinary 2>&1)
    $smokeExitCode = $LASTEXITCODE
    $smokeText = ($smokeLines | ForEach-Object { $_.ToString() }) -join "`n"
    if ($smokeExitCode -ne 1 -or $smokeLines.Count -lt 1) {
        $safeSmokeText = $smokeText.Replace($smokeSecret, "[凭据已隐藏]")
        if ($safeSmokeText.Length -gt 1000) {
            $safeSmokeText = $safeSmokeText.Substring(0, 1000) + "[已截断]"
        }
        Write-Error (
            "sidecar 冒烟测试没有按关闭式失败协议退出：exit={0}, lines={1}, output={2}" -f
            $smokeExitCode,
            $smokeLines.Count,
            $safeSmokeText
        )
        exit 1
    }
    if ($smokeText.Contains($smokeSecret)) {
        Write-Error "sidecar 冒烟测试泄露了 API key。"
        exit 1
    }
    try {
        $smokeEvents = @($smokeLines | ForEach-Object { $_.ToString() | ConvertFrom-Json })
    }
    catch {
        Write-Error "sidecar 冒烟测试没有返回有效 JSONL。"
        exit 1
    }
    $progressEvents = @($smokeEvents | Select-Object -SkipLast 1)
    $smokeEvent = $smokeEvents[-1]
    if (
        $smokeEvent.schema_version -ne 2 -or
        $smokeEvent.type -ne "failed" -or
        [string]::IsNullOrWhiteSpace($smokeEvent.message) -or
        @($progressEvents | Where-Object {
            $_.schema_version -ne 2 -or $_.type -ne "progress"
        }).Count -ne 0
    ) {
        Write-Error "sidecar 冒烟测试返回了无效终态。"
        exit 1
    }

    $compatibilityRoot = Join-Path $resolvedBuildRoot "compatibility-smoke"
    $compatibilityGame = Join-Path $compatibilityRoot "game"
    New-Item -ItemType Directory -Path $compatibilityGame -Force | Out-Null
    [System.IO.File]::WriteAllText(
        (Join-Path $compatibilityGame "script.rpy"),
        "label start:`n    pass`n",
        [System.Text.UTF8Encoding]::new($false)
    )
    $compatibilityRequest = @{
        schema_version = 2
        operation = "inspect_renpy_compatibility"
        project_path = $compatibilityRoot
    } | ConvertTo-Json -Compress
    $compatibilityLines = @($compatibilityRequest | & $builtBinary 2>&1)
    $compatibilityExitCode = $LASTEXITCODE
    try {
        $compatibilityEvents = @(
            $compatibilityLines | ForEach-Object { $_.ToString() | ConvertFrom-Json }
        )
    }
    catch {
        Write-Error "sidecar 兼容性冒烟测试没有返回有效 JSONL。"
        exit 1
    }
    if (
        $compatibilityExitCode -ne 0 -or
        $compatibilityEvents.Count -ne 1 -or
        $compatibilityEvents[0].schema_version -ne 2 -or
        $compatibilityEvents[0].type -ne "compatibility_report" -or
        $compatibilityEvents[0].report.schema_version -ne 1 -or
        $compatibilityEvents[0].report.status -ne "source_ready" -or
        $compatibilityEvents[0].report.can_translate_now -ne $true
    ) {
        Write-Error "sidecar 兼容性冒烟测试没有返回可用的关闭式报告。"
        exit 1
    }

    New-Item -ItemType Directory -Path $binaryDirectory -Force | Out-Null
    $destination = Join-Path $binaryDirectory ("galtrans-backend-{0}.exe" -f $targetTriple)
    Copy-Item -LiteralPath $builtBinary -Destination $destination -Force
    Write-Output "已生成并验证 Tauri sidecar：$destination"
}
finally {
    if (
        (Test-Path -LiteralPath $resolvedBuildRoot) -and
        $resolvedBuildRoot.StartsWith($systemTempRoot, [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        Remove-Item -LiteralPath $resolvedBuildRoot -Recurse -Force
    }
}
