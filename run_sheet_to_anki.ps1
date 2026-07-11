param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)

$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
. (Join-Path $ProjectRoot "scripts\python_env.ps1")

$VenvPython = Initialize-ProjectPythonEnvironment -ProjectRoot $ProjectRoot -Prefix "sheet-to-anki"
$MainScript = Join-Path $ProjectRoot "sheet_to_anki.py"

Write-BootstrapStep "sheet-to-anki" "Running sheet_to_anki.py"
Invoke-BootstrapChecked $VenvPython (@($MainScript) + $ScriptArgs)
