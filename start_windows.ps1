$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = "1"
$venvPython = Join-Path (Join-Path $PSScriptRoot ".venv") "Scripts/python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "尚未安装本地环境，请先运行 install_windows.ps1。"
}
& $venvPython (Join-Path $PSScriptRoot "launch.py")
exit $LASTEXITCODE
