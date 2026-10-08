$ErrorActionPreference = 'Stop'
$AgentRoot = Split-Path -Parent $PSScriptRoot
$AgentPython = Join-Path $AgentRoot '.venv\Scripts\python.exe'

function Invoke-AgentCommand {
    param([string]$Program, [string[]]$CommandArgs)
    & $Program @CommandArgs
    if ($LASTEXITCODE -ne 0) { throw "Command failed (exit $LASTEXITCODE): $Program" }
}

function Test-AgentPython {
    param([string]$Program, [string]$Code)
    $ErrorActionPreference = 'Continue'
    & $Program -c $Code 2>&1 | Out-Null
    return $LASTEXITCODE -eq 0
}

function Find-AgentUv {
    $minimum = [version](Get-Content -LiteralPath (Join-Path $AgentRoot '.uv-version') -Raw).Trim()
    function Test-CompatibleUv([string]$Program) {
        $versionText = & $Program --version 2>$null
        return $LASTEXITCODE -eq 0 -and $versionText -match '^uv (\d+\.\d+\.\d+)' -and
            [version]$Matches[1] -ge $minimum -and [version]$Matches[1] -lt [version]'1.0.0'
    }
    if ($env:OSWORLD_UV_EXE) {
        if (-not (Test-Path -LiteralPath $env:OSWORLD_UV_EXE -PathType Leaf)) {
            throw 'OSWORLD_UV_EXE must point to an existing uv.exe.'
        }
        $explicitUv = [IO.Path]::GetFullPath($env:OSWORLD_UV_EXE)
        if (-not (Test-CompatibleUv $explicitUv)) { throw "OSWORLD_UV_EXE requires uv >=$minimum and <1.0.0." }
        return $explicitUv
    }
    $localUv = Join-Path $AgentRoot '.tools\uv\uv.exe'
    if ((Test-Path -LiteralPath $localUv -PathType Leaf) -and (Test-CompatibleUv $localUv)) { return $localUv }
    $uvCommand = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($uvCommand -and (Test-CompatibleUv $uvCommand.Source)) { return $uvCommand.Source }
    foreach ($relative in @('.local\bin\uv.exe', '.cargo\bin\uv.exe')) {
        $candidate = Join-Path $env:USERPROFILE $relative
        if ((Test-Path -LiteralPath $candidate -PathType Leaf) -and (Test-CompatibleUv $candidate)) { return $candidate }
    }
    return $null
}

function Install-AgentUv {
    $existing = Find-AgentUv
    if ($existing) { return $existing }
    $version = (Get-Content -LiteralPath (Join-Path $AgentRoot '.uv-version') -Raw).Trim()
    if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid .uv-version.' }
    $installDir = Join-Path $AgentRoot '.tools\uv'
    New-Item -ItemType Directory -Path $installDir -Force | Out-Null
    $installer = Join-Path $installDir 'install.ps1'
    Write-Host "[setup] Installing uv $version into .tools\uv..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri "https://astral.sh/uv/$version/install.ps1" -OutFile $installer
    $previousInstall = $env:UV_UNMANAGED_INSTALL
    try {
        $env:UV_UNMANAGED_INSTALL = $installDir
        Invoke-AgentCommand 'powershell.exe' @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $installer) | Out-Host
    } finally { $env:UV_UNMANAGED_INSTALL = $previousInstall }
    $installed = Find-AgentUv
    if (-not $installed) { throw 'uv installation failed. Install uv from docs.astral.sh or set OSWORLD_UV_EXE.' }
    return $installed
}

function Get-AgentManagedPython {
    # Resolve the full patch version and use its real executable, avoiding Windows
    # minor-version junctions. Preserve any unrelated user Python installation.
    $ErrorActionPreference = 'Continue'
    $version = (Get-Content -LiteralPath (Join-Path $AgentRoot '.python-version') -Raw).Trim()
    $candidate = & $AgentUv python find --system --no-project --managed-python --no-python-downloads $version 2>$null
    if ($LASTEXITCODE -eq 0 -and $candidate -and
        (Test-AgentPython $candidate 'import sys; sys.exit(0 if sys.version_info[:2] == (3,12) and sys.maxsize > 2**32 else 1)')) {
        return [string]$candidate
    }
    $output = & $AgentUv python install $version --no-bin --no-registry 2>&1
    $installExit = $LASTEXITCODE
    foreach ($line in $output) { Write-Host ([string]$line) }
    if ($installExit -ne 0 -and (($output | Out-String) -notmatch 'Missing expected target directory for Python minor version link')) {
        throw "uv Python installation failed (exit $installExit)."
    }
    $candidate = & $AgentUv python find --system --no-project --managed-python --no-python-downloads $version 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $candidate -or
        -not (Test-AgentPython $candidate 'import sys; sys.exit(0 if sys.version_info[:2] == (3,12) and sys.maxsize > 2**32 else 1)')) {
        throw 'Managed Python is unavailable. See the Python troubleshooting section in docs/WINDOWS_AGENT_RUNBOOK.md.'
    }
    if ($installExit -ne 0) { Write-Host '[setup] Managed Python downloaded; using its full executable path without the minor-version junction.' }
    return [string]$candidate
}

function Get-AgentEnvironmentFingerprint {
    $parts = foreach ($name in @('pyproject.toml', 'uv.lock', 'requirements-browser.txt', '.python-version', '.uv-version')) {
        (Get-FileHash -LiteralPath (Join-Path $AgentRoot $name) -Algorithm SHA256).Hash
    }
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes(($parts -join ':')))).Replace('-', '') }
    finally { $hash.Dispose() }
}

function Invoke-AgentModule {
    param([string]$Module, [string[]]$ModuleArgs)
    Invoke-AgentCommand $AgentUv (@('run', '--locked', '--no-sync', '--no-env-file', '-m', $Module) + $ModuleArgs)
}

function Import-AgentEnv {
    param([switch]$IgnoreComputerSession)
    $envFile = Join-Path $AgentRoot '.env'
    if (Test-Path -LiteralPath $envFile) {
        foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8) {
            $entry = $line.Trim()
            if (-not $entry -or $entry.StartsWith('#')) { continue }
            if ($entry -notmatch '^([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
                throw 'Invalid .env entry. Use NAME=value, one setting per line.'
            }
            $envName = $Matches[1]
            $envValue = $Matches[2].Trim()
            if ($envValue.Length -ge 2 -and
                (($envValue.StartsWith('"') -and $envValue.EndsWith('"')) -or
                 ($envValue.StartsWith("'") -and $envValue.EndsWith("'")))) {
                $envValue = $envValue.Substring(1, $envValue.Length - 2)
            }
            # Shell environment takes precedence. Empty template values do not erase it.
            if ($envValue -and -not [Environment]::GetEnvironmentVariable($envName, 'Process')) {
                [Environment]::SetEnvironmentVariable($envName, $envValue, 'Process')
            }
        }
    }
    # Compatibility names live in configuration, not in launcher logic.
    $aliasFile = Join-Path $AgentRoot 'config\environment_aliases.json'
    $aliases = Get-Content -LiteralPath $aliasFile -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($setting in $aliases.PSObject.Properties) {
        if ([Environment]::GetEnvironmentVariable($setting.Name, 'Process')) { continue }
        foreach ($legacyName in $setting.Value) {
            $legacyValue = [Environment]::GetEnvironmentVariable($legacyName, 'Process')
            if ($legacyValue) {
                [Environment]::SetEnvironmentVariable($setting.Name, $legacyValue, 'Process')
                break
            }
        }
    }
    if ($IgnoreComputerSession) {
        # Fixtures own fresh browser state; never inherit a user's logged-in session.
        foreach ($envName in @('COMPUTER_BROWSER_PROFILE_DIR', 'COMPUTER_CDP_ENDPOINT', 'COMPUTER_DOWNLOAD_DIR')) {
            [Environment]::SetEnvironmentVariable($envName, $null, 'Process')
        }
    }
    foreach ($envName in @('OSWORLD_DESKTOP_ENV_PATH', 'OSWORLD_EXAMPLES_DIR',
            'OSWORLD_VM_PATH', 'OSWORLD_DESKTOP_REQUIREMENTS', 'OSWORLD_MODEL_PRESETS', 'AGENTS_RESULTS_DIR',
            'OSWORLD_RUNNER_PYTHON', 'PLAN_TOKENIZER_PATH', 'PLAN_PROCESSOR_PATH',
            'GROUNDING_TOKENIZER_PATH', 'GROUNDING_PROCESSOR_PATH', 'COMPUTER_BROWSER_EXECUTABLE',
            'COMPUTER_BROWSER_PROFILE_DIR', 'COMPUTER_DOWNLOAD_DIR')) {
        $envValue = [Environment]::GetEnvironmentVariable($envName, 'Process')
        if ($envValue -and -not [IO.Path]::IsPathRooted($envValue)) {
            [Environment]::SetEnvironmentVariable($envName, [IO.Path]::GetFullPath((Join-Path $AgentRoot $envValue)), 'Process')
        }
    }
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUNBUFFERED = '1'
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $AgentRoot '.venv'
    $env:UV_NO_ENV_FILE = '1'
    if (-not $env:UV_LINK_MODE) { $env:UV_LINK_MODE = 'copy' }
    $env:OSWORLD_AGENT_ROOT = $AgentRoot
    if (-not $env:OSWORLD_RUNNER_PYTHON) { $env:OSWORLD_RUNNER_PYTHON = $AgentPython }
    if (-not $env:AGENTS_RESULTS_DIR) { $env:AGENTS_RESULTS_DIR = Join-Path $AgentRoot 'artifacts\viz' }
    if (-not $env:AGENTS_VIZ_PORT) { $env:AGENTS_VIZ_PORT = '8088' }
    if (-not $env:OSWORLD_SNAPSHOT_NAME) { $env:OSWORLD_SNAPSHOT_NAME = 'init_state' }
    if (-not $env:OSWORLD_BROWSER_CHANNEL) { $env:OSWORLD_BROWSER_CHANNEL = 'auto' }
    if (-not $env:COMPUTER_BROWSER_CHANNEL) { $env:COMPUTER_BROWSER_CHANNEL = 'auto' }
    # OSWorld must inherit vmrun's location even when VMware did not update PATH.
    foreach ($installRoot in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if (-not $installRoot) { continue }
        $vmwareDir = Join-Path $installRoot 'VMware\VMware Workstation'
        if (Test-Path -LiteralPath (Join-Path $vmwareDir 'vmrun.exe')) {
            $env:PATH = $vmwareDir + [IO.Path]::PathSeparator + $env:PATH
            break
        }
    }
}
