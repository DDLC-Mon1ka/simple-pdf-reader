$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath ".venv")) {
    python -m venv ".venv"
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $python -m pip install --upgrade pip
& $python -m pip install -r "requirements.txt"

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "PDFReader" `
    --icon "pdf_reader.ico" `
    --add-data "pdf_reader.ico;." `
    --collect-all "pypdfium2" `
    --collect-all "tkinterdnd2" `
    --collect-all "cryptography" `
    "app.py"

Write-Host "Build complete: dist\PDFReader.exe"
