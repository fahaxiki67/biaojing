$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = "1"

$pythonCommand = $null
$pythonArgs = @()
if ($env:BIAOJING_PYTHON) {
    $pythonCommand = $env:BIAOJING_PYTHON
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonCommand = "py"
    $pythonArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonCommand = "python"
}
if (-not $pythonCommand) {
    throw "未找到 Python 3.10+。请先安装 Python，再运行本脚本。"
}
$pythonVersion = & $pythonCommand @pythonArgs -c "import sys; print('ok' if sys.version_info >= (3, 10) else 'old')"
if ($LASTEXITCODE -ne 0 -or $pythonVersion.Trim() -ne "ok") {
    throw "需要 Python 3.10 或更高版本。可设置 BIAOJING_PYTHON 指定解释器。"
}

$venvPython = Join-Path (Join-Path $PSScriptRoot ".venv") "Scripts/python.exe"
& $pythonCommand @pythonArgs -m venv (Join-Path $PSScriptRoot ".venv")
if ($LASTEXITCODE -ne 0) { throw "创建本地 Python 环境失败。" }
& $venvPython -m pip install -r (Join-Path $PSScriptRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败。" }
& $venvPython (Join-Path $PSScriptRoot "launch.py") --check
if ($LASTEXITCODE -ne 0) { throw "环境检查失败。" }
Write-Output "依赖安装完成。运行 start_windows.ps1 启动；扫描件 OCR 还需安装 Tesseract 和 chi_sim 中文模型。"
