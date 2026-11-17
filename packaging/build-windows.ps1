# Builds a standalone Uncle Virgil for Windows in dist\Uncle Virgil\ (no Python needed to run it).
# Run from the repository folder in PowerShell:  .\packaging\build-windows.ps1
$ErrorActionPreference = "Stop"
python -m pip install -e . "pyinstaller>=6"
python -m PyInstaller --noconfirm --clean --windowed --name "Uncle Virgil" `
    --collect-all sherpa_onnx `
    --collect-data uncle_virgil `
    --collect-all webview `
    --collect-submodules desktop_notifier `
    --collect-submodules uvicorn `
    --copy-metadata keyring `
    packaging/launcher.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host "Built: dist\Uncle Virgil\Uncle Virgil.exe"
