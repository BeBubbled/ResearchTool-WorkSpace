$ErrorActionPreference = "Stop"

function Write-BootstrapStep {
    param(
        [string]$Prefix,
        [string]$Message
    )
    Write-Host "[$Prefix] $Message"
}

function Invoke-BootstrapChecked {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    & $FilePath @Arguments | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

function Test-BootstrapPythonModule {
    param(
        [string]$Python,
        [string]$ModuleName
    )

    & $Python -c "import $ModuleName" 2>$null
    return $LASTEXITCODE -eq 0
}

function Ensure-BootstrapPip {
    param(
        [string]$VenvPython,
        [string]$Prefix
    )

    if (Test-BootstrapPythonModule $VenvPython "pip") {
        return
    }

    Write-BootstrapStep $Prefix "Project .venv is missing pip. Repairing it with ensurepip."
    Invoke-BootstrapChecked $VenvPython @("-m", "ensurepip", "--upgrade")

    if (-not (Test-BootstrapPythonModule $VenvPython "pip")) {
        throw "Could not repair pip in project .venv. Delete the .venv folder and rerun this script."
    }
}

function Get-BootstrapSystemPython {
    $pyLauncher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        & $pyLauncher.Source -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return @{
                FilePath = $pyLauncher.Source
                Arguments = @("-3")
            }
        }
    }

    $python = Get-Command "python" -ErrorAction SilentlyContinue
    if ($python) {
        & $python.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return @{
                FilePath = $python.Source
                Arguments = @()
            }
        }
    }

    return $null
}

function Install-BootstrapPython {
    param([string]$Prefix)

    $winget = Get-Command "winget" -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "Python 3.10+ was not found, and winget is unavailable. Install Python from https://www.python.org/downloads/ and rerun this script."
    }

    Write-BootstrapStep $Prefix "Python 3.10+ not found. Installing Python 3.12 with winget."
    Invoke-BootstrapChecked $winget.Source @(
        "install",
        "--id",
        "Python.Python.3.12",
        "--exact",
        "--source",
        "winget",
        "--accept-package-agreements",
        "--accept-source-agreements"
    )

    $pythonInfo = Get-BootstrapSystemPython
    if (-not $pythonInfo) {
        throw "Python installation finished, but Python is still not available in this shell. Open a new PowerShell window and rerun this script."
    }

    return $pythonInfo
}

function New-BootstrapVenv {
    param(
        [string]$ProjectRoot,
        [string]$VenvDir,
        [string]$Prefix
    )

    $pythonInfo = Get-BootstrapSystemPython
    if (-not $pythonInfo) {
        $pythonInfo = Install-BootstrapPython $Prefix
    }

    Write-BootstrapStep $Prefix "Using system Python only to create an isolated project environment."
    Write-BootstrapStep $Prefix "Creating project Python environment at $VenvDir"
    Invoke-BootstrapChecked $pythonInfo.FilePath ($pythonInfo.Arguments + @("-m", "venv", $VenvDir))
}

function Get-BootstrapRequirementsHash {
    param([string]$Requirements)

    if (-not (Test-Path -LiteralPath $Requirements)) {
        throw "Missing requirements file: $Requirements"
    }

    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Requirements).Hash
}

function Assert-BootstrapVenvPython {
    param(
        [string]$ProjectRoot,
        [string]$VenvPython
    )

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        throw "Project virtual environment Python was not found: $VenvPython"
    }

    $root = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $pythonPath = (Resolve-Path -LiteralPath $VenvPython).Path
    if (-not $pythonPath.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to install dependencies outside the project environment: $pythonPath"
    }
}

function Sync-BootstrapDependencies {
    param(
        [string]$ProjectRoot,
        [string]$VenvPython,
        [string]$Requirements,
        [string]$RequirementsStamp,
        [string]$Prefix
    )

    Assert-BootstrapVenvPython $ProjectRoot $VenvPython

    $currentHash = Get-BootstrapRequirementsHash $Requirements
    $installedHash = $null

    if (Test-Path -LiteralPath $RequirementsStamp) {
        $installedHash = (Get-Content -LiteralPath $RequirementsStamp -Raw).Trim()
    }

    if ($currentHash -eq $installedHash) {
        Write-BootstrapStep $Prefix "Dependencies already installed in project .venv."
        return
    }

    Ensure-BootstrapPip $VenvPython $Prefix
    Write-BootstrapStep $Prefix "Installing dependencies into project .venv only; system Python packages will not be changed."
    Invoke-BootstrapChecked $VenvPython @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-BootstrapChecked $VenvPython @("-m", "pip", "install", "-r", $Requirements)
    Set-Content -LiteralPath $RequirementsStamp -Value $currentHash -Encoding ASCII
}

function Initialize-ProjectPythonEnvironment {
    param(
        [string]$ProjectRoot,
        [string]$Prefix = "python-env"
    )

    $VenvDir = Join-Path $ProjectRoot ".venv"
    $VenvPython = Join-Path $VenvDir "Scripts\python.exe"
    $Requirements = Join-Path $ProjectRoot "requirements.txt"
    $RequirementsStamp = Join-Path $VenvDir ".requirements.sha256"

    Write-BootstrapStep $Prefix "Python dependencies are isolated in project .venv."

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        New-BootstrapVenv $ProjectRoot $VenvDir $Prefix
    }
    else {
        Write-BootstrapStep $Prefix "Using existing project Python environment."
    }

    Sync-BootstrapDependencies $ProjectRoot $VenvPython $Requirements $RequirementsStamp $Prefix
    return $VenvPython
}
