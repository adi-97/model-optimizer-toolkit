param(
    [switch]$Classifier,
    [switch]$Train,
    [switch]$User
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:USE_TF = "0"
if (-not (Test-Path .venv)) {
    $python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
    & $python -c "import sys; sys.exit(sys.version_info < (3, 11))" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "adtok needs Python 3.11+; $python is older or missing."
        Write-Host "point PYTHON at a newer interpreter, e.g. `$env:PYTHON = 'C:\Python312\python.exe'; .\install.ps1"
        exit 1
    }
    & $python -m venv .venv
}
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $py -m pip install -q --upgrade pip
if ($Classifier -or $Train) {
    if (Test-Path ..\laya-model\pyproject.toml) { & $py -m pip install -q -e ..\laya-model }
    & $py -m pip install -q -e ".[classifier]"
} else {
    & $py -m pip install -q -e .
}
if ($Train -and -not (Test-Path models\tier-full-v1\model.safetensors)) {
    & $py scripts\build_dataset.py
    & $py -m laya.train_cli --data data\train.jsonl --eval-data data\eval.jsonl --base english --epochs 6 `
        --micro-batch 8 --grad-accum 1 --out models\tier-full-v1
}
if ($User) {
    $set = @()
    if (-not (Test-Path models\tier-full-v1\model.safetensors)) { $set = @("--set", "routing.classifier=false") }
    & $py -m adtok.cli init --user @set
}
Write-Host "adtok is installed: $PSScriptRoot\.venv\Scripts\adtok.exe"
if (-not $User) { Write-Host "next: .venv\Scripts\adtok init --user   (every project)  or  .venv\Scripts\adtok init <project>" }
