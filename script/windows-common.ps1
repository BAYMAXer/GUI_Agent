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

function Import-AgentEnv {
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
    foreach ($envName in @('OSWORLD_DESKTOP_ENV_PATH', 'OSWORLD_EXAMPLES_DIR',
            'OSWORLD_VM_PATH', 'OSWORLD_DESKTOP_REQUIREMENTS', 'OSWORLD_MODEL_PRESETS', 'AGENTS_RESULTS_DIR')) {
        $envValue = [Environment]::GetEnvironmentVariable($envName, 'Process')
        if ($envValue -and -not [IO.Path]::IsPathRooted($envValue)) {
            [Environment]::SetEnvironmentVariable($envName, [IO.Path]::GetFullPath((Join-Path $AgentRoot $envValue)), 'Process')
        }
    }
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUNBUFFERED = '1'
    $env:OSWORLD_AGENT_ROOT = $AgentRoot
    if (-not $env:OSWORLD_RUNNER_PYTHON) { $env:OSWORLD_RUNNER_PYTHON = $AgentPython }
    if (-not $env:AGENTS_RESULTS_DIR) { $env:AGENTS_RESULTS_DIR = Join-Path $AgentRoot 'artifacts\viz' }
    if (-not $env:AGENTS_VIZ_PORT) { $env:AGENTS_VIZ_PORT = '8088' }
    if (-not $env:OSWORLD_SNAPSHOT_NAME) { $env:OSWORLD_SNAPSHOT_NAME = 'init_state' }
    if (-not $env:OSWORLD_BROWSER_CHANNEL) { $env:OSWORLD_BROWSER_CHANNEL = 'auto' }
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

function Find-AgentPython {
    $ErrorActionPreference = 'Continue'
    $candidates = @()
    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $detected = & $pyLauncher.Source -3.12 -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0) { $candidates += $detected }
    }
    $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
    $candidates += (Join-Path $env:ProgramFiles 'Python312\python.exe')
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand -and $pythonCommand.Source -notlike '*\Microsoft\WindowsApps\*') {
        $candidates += $pythonCommand.Source
    }
    foreach ($candidate in $candidates | Select-Object -Unique) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        if (Test-AgentPython $candidate 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) and sys.maxsize > 2**32 else 1)') { return $candidate }
    }
    return $null
}
