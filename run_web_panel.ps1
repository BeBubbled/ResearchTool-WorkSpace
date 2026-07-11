param(
    [switch]$NoPause,
    [switch]$NoBrowser,
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

function Pause-BeforeExit {
    param([string]$Message = "Press Enter to close this window.")

    if (-not $NoPause -and [Environment]::UserInteractive) {
        Read-Host $Message | Out-Null
    }
}

try {
    $ProjectRoot = $PSScriptRoot
    . (Join-Path $ProjectRoot "scripts\python_env.ps1")

    $VenvPython = Initialize-ProjectPythonEnvironment -ProjectRoot $ProjectRoot -Prefix "web-panel"
    $MainScript = Join-Path $ProjectRoot "web_panel.py"

    $env:WEB_PANEL_PORT = [string]$Port
    $env:WEB_PANEL_OPEN_BROWSER = if ($NoBrowser) { "0" } else { "1" }

    Write-BootstrapStep "web-panel" "Starting local web panel on 127.0.0.1."
    Write-BootstrapStep "web-panel" "Your browser will open automatically when the panel is ready."

    Invoke-BootstrapChecked $VenvPython @($MainScript)
}
catch {
    Write-Host ""
    Write-Host "[web-panel] Failed to start:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Pause-BeforeExit "Press Enter to close this window."
    exit 1
}

Remove-Item Env:\WEB_PANEL_PORT -ErrorAction SilentlyContinue
Remove-Item Env:\WEB_PANEL_OPEN_BROWSER -ErrorAction SilentlyContinue

Pause-BeforeExit "Web panel stopped. Press Enter to close this window."
