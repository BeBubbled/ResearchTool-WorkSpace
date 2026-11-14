param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)

$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
. (Join-Path $ProjectRoot "scripts\python_env.ps1")

$VenvPython = Initialize-ProjectPythonEnvironment -ProjectRoot $ProjectRoot -Prefix "sheet-to-anki"
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$ProjectRoot$([IO.Path]::PathSeparator)$env:PYTHONPATH" } else { $ProjectRoot }

Write-BootstrapStep "sheet-to-anki" "Running sheet_to_anki.py"
Invoke-BootstrapChecked $VenvPython (@("-m", "researchtool.sheet_to_anki") + $ScriptArgs)
