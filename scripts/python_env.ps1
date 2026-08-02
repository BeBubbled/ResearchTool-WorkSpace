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

function Test-BootstrapPythonExecutable {
    param([string]$Python)

    if (-not (Test-Path -LiteralPath $Python)) {
        return $false
    }

    try {
        $probe = & $Python -c "import sys; print('CODEX_PROJECT_PYTHON_OK' if sys.version_info >= (3, 10) else '')" 2>$null
        return $LASTEXITCODE -eq 0 -and $probe -contains "CODEX_PROJECT_PYTHON_OK"
    }
    catch {
        return $false
    }
}

function Test-BootstrapCommand {
    param([string]$Name)

    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Add-BootstrapFfmpegToPath {
    # winget's portable links are not always added to the PATH of the current
    # PowerShell process. Locate the installed binary and expose it to the
    # launcher and its Python child process without changing the user's PATH.
    $roots = @(
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages")
    )

    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) {
            continue
        }

        $binary = Get-ChildItem -LiteralPath $root -Filter "ffmpeg.exe" -File -Recurse -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($binary) {
            $binaryDir = $binary.Directory.FullName
            if (-not (($env:Path -split ';') -contains $binaryDir)) {
                $env:Path = "$binaryDir;$env:Path"
            }
            return $true
        }
    }

    return $false
}

function Ensure-BootstrapFfmpeg {
    param([string]$Prefix = "ffmpeg")

    if ((Test-BootstrapCommand "ffmpeg") -and (Test-BootstrapCommand "ffprobe")) {
        Write-BootstrapStep $Prefix "FFmpeg and FFprobe are available."
        return
    }

    $winget = Get-Command "winget" -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "FFmpeg and FFprobe are required by video tools. winget is unavailable; install FFmpeg manually, add its bin folder to PATH, and rerun this launcher."
    }

    Write-BootstrapStep $Prefix "FFmpeg/FFprobe not found. Installing the user-scoped Gyan.FFmpeg.Shared package with winget."
    Invoke-BootstrapChecked $winget.Source @(
        "install",
        "--id",
        "Gyan.FFmpeg.Shared",
        "--exact",
        "--source",
        "winget",
        "--accept-package-agreements",
        "--accept-source-agreements"
    )

    if (-not (Test-BootstrapCommand "ffmpeg")) {
        Add-BootstrapFfmpegToPath | Out-Null
    }

    if (-not ((Test-BootstrapCommand "ffmpeg") -and (Test-BootstrapCommand "ffprobe"))) {
        throw "FFmpeg was installed but is not available in this PowerShell session. Close this window, open a new one, and rerun the launcher."
    }

    Write-BootstrapStep $Prefix "FFmpeg and FFprobe are ready."
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

function Get-BootstrapGo {
    param(
        [string]$ProjectRoot,
        [string]$Prefix = "translation-backend"
    )

    $go = Get-Command "go" -ErrorAction SilentlyContinue
    if ($go) {
        return $go.Source
    }

    $installedGo = Join-Path $env:ProgramFiles "Go\bin\go.exe"
    if (Test-Path -LiteralPath $installedGo) {
        return $installedGo
    }

    $toolchainRoot = Join-Path $ProjectRoot ".runtime\toolchains"
    $localCandidates = @(
        (Join-Path $toolchainRoot "go1.26.5\go\bin\go.exe"),
        (Join-Path $toolchainRoot "go-complete\go\bin\go.exe")
    )
    foreach ($candidate in $localCandidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    $version = "1.26.5"
    $archive = Join-Path $toolchainRoot "go${version}.windows-amd64.zip"
    $destination = Join-Path $toolchainRoot "go${version}"
    $portableGo = Join-Path $destination "go\bin\go.exe"
    New-Item -ItemType Directory -Path $toolchainRoot -Force | Out-Null
    if (-not (Test-Path -LiteralPath $archive)) {
        Write-BootstrapStep $Prefix "Downloading the project-local Go $version toolchain."
        Invoke-WebRequest -Uri "https://go.dev/dl/go${version}.windows-amd64.zip" -OutFile $archive -UseBasicParsing
    }
    $expectedHash = "97e6b2a833b6d89f9ff17d25419ac0a7e3b482a044e9ab18cdef834bd834fd38"
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw "Downloaded Go archive checksum mismatch. Expected $expectedHash but received $actualHash."
    }
    if (-not (Test-Path -LiteralPath $portableGo)) {
        if (Test-Path -LiteralPath $destination) {
            throw "The project-local Go directory is incomplete: $destination. Remove only that directory and rerun the launcher."
        }
        Write-BootstrapStep $Prefix "Extracting the project-local Go toolchain."
        New-Item -ItemType Directory -Path $destination | Out-Null
        tar.exe -xf $archive -C $destination
    }
    if (-not (Test-Path -LiteralPath $portableGo)) {
        throw "The project-local Go toolchain is incomplete: $portableGo"
    }
    return $portableGo
}

function Ensure-BootstrapTranslationBackend {
    param(
        [string]$ProjectRoot,
        [string]$Prefix = "translation-backend"
    )

    $sourceDir = Join-Path $ProjectRoot "translation_backend"
    $runtimeDir = Join-Path $ProjectRoot ".runtime\translation-backend"
    $binary = Join-Path $runtimeDir "ai-markdown-translator.exe"
    if (-not (Test-Path -LiteralPath (Join-Path $sourceDir "go.mod"))) {
        throw "AI-Markdown-Translator backend source is missing: $sourceDir"
    }

    $mustBuild = -not (Test-Path -LiteralPath $binary)
    if (-not $mustBuild) {
        $binaryTime = (Get-Item -LiteralPath $binary).LastWriteTimeUtc
        $newerSource = Get-ChildItem -LiteralPath $sourceDir -Recurse -File |
            Where-Object { $_.Name -in @("go.mod", "go.sum") -or $_.Extension -eq ".go" } |
            Where-Object { $_.LastWriteTimeUtc -gt $binaryTime } |
            Select-Object -First 1
        $mustBuild = $null -ne $newerSource
    }
    if (-not $mustBuild) {
        Write-BootstrapStep $Prefix "AI-Markdown-Translator backend is already built."
        return $binary
    }

    $go = Get-BootstrapGo -ProjectRoot $ProjectRoot -Prefix $Prefix
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    $env:GOCACHE = (New-Item -ItemType Directory -Path (Join-Path $ProjectRoot ".runtime\go-cache") -Force).FullName
    $env:GOPATH = (New-Item -ItemType Directory -Path (Join-Path $ProjectRoot ".runtime\go-path") -Force).FullName
    $env:GOMODCACHE = (New-Item -ItemType Directory -Path (Join-Path $ProjectRoot ".runtime\go-mod-cache") -Force).FullName
    $env:GOTOOLCHAIN = "local"
    Write-BootstrapStep $Prefix "Building the backend-only AI-Markdown-Translator adapter."
    Push-Location $sourceDir
    try {
        Invoke-BootstrapChecked $go @("build", "-trimpath", "-o", $binary, ".\cmd\translator")
    }
    finally {
        Pop-Location
    }
    if (-not (Test-Path -LiteralPath $binary)) {
        throw "The AI-Markdown-Translator backend build did not produce $binary"
    }
    return $binary
}

function Move-BootstrapInvalidVenv {
    param(
        [string]$ProjectRoot,
        [string]$VenvDir,
        [string]$Prefix
    )

    $root = (Resolve-Path -LiteralPath $ProjectRoot).Path.TrimEnd('\')
    $resolvedVenv = (Resolve-Path -LiteralPath $VenvDir).Path.TrimEnd('\')
    $expectedVenv = (Join-Path $root ".venv").TrimEnd('\')
    if (-not $resolvedVenv.Equals($expectedVenv, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to move an invalid environment outside the project .venv: $resolvedVenv"
    }

    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupDir = Join-Path $root ".venv.invalid-$timestamp"
    Write-BootstrapStep $Prefix "Existing project .venv cannot run. Preserving it at $backupDir"
    Move-Item -LiteralPath $resolvedVenv -Destination $backupDir
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

    if ((Test-Path -LiteralPath $VenvDir) -and -not (Test-BootstrapPythonExecutable $VenvPython)) {
        Move-BootstrapInvalidVenv $ProjectRoot $VenvDir $Prefix
    }

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        New-BootstrapVenv $ProjectRoot $VenvDir $Prefix
    }
    else {
        Write-BootstrapStep $Prefix "Using existing project Python environment."
    }

    Sync-BootstrapDependencies $ProjectRoot $VenvPython $Requirements $RequirementsStamp $Prefix
    return $VenvPython
}
