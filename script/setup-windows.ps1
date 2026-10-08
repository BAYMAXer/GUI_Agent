param(
    [ValidateSet('browser', 'computer', 'desktop')][string]$Profile = 'browser',
    [string]$Python = '',
    [switch]$SkipSmoke,
    [switch]$RecreateVenv,
    [switch]$IsolatedComputerBrowser
)
. (Join-Path $PSScriptRoot 'windows-common.ps1')
try {
    if (-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess -or
        $env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') {
        throw 'This setup targets Windows x64. Use a 64-bit PowerShell process on an x64 machine.'
    }
    Set-Location -LiteralPath $AgentRoot
    if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
    if (-not (Test-Path -LiteralPath 'config\model_presets.local.yaml')) {
        Copy-Item -LiteralPath 'config\model_presets.yaml' -Destination 'config\model_presets.local.yaml'
    }
    Import-AgentEnv -IgnoreComputerSession:$IsolatedComputerBrowser
    if ($Profile -eq 'desktop' -and (-not $env:OSWORLD_DESKTOP_ENV_PATH -or
        -not (Test-Path -LiteralPath (Join-Path $env:OSWORLD_DESKTOP_ENV_PATH 'desktop_env') -PathType Container))) {
        throw 'Desktop mode requires an external OSWorld checkout. Configure .env; see docs/WINDOWS_AGENT_RUNBOOK.md.'
    }
    $venvDir = [IO.Path]::GetFullPath((Join-Path $AgentRoot '.venv'))
    if (Test-Path -LiteralPath $venvDir) {
        if ($RecreateVenv) {
            $backupDir = [IO.Path]::GetFullPath((Join-Path $AgentRoot ('.venv.backup-' + [Guid]::NewGuid().ToString('N'))))
            $workspacePrefix = [IO.Path]::GetFullPath($AgentRoot).TrimEnd('\') + '\'
            if (-not $venvDir.StartsWith($workspacePrefix, [StringComparison]::OrdinalIgnoreCase) -or
                -not $backupDir.StartsWith($workspacePrefix, [StringComparison]::OrdinalIgnoreCase) -or
                (Get-Item -LiteralPath $venvDir).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Refusing to move a venv outside this checkout or a linked venv.'
            }
            Move-Item -LiteralPath $venvDir -Destination $backupDir
            Write-Host "[setup] Preserved old venv at $backupDir"
        } else {
            $valid = Test-AgentPython $AgentPython 'import sys; sys.exit(0 if sys.version_info[:2] == (3,12) and sys.maxsize > 2**32 else 1)'
            $cfg = Join-Path $venvDir 'pyvenv.cfg'
            $shared = (Test-Path -LiteralPath $cfg) -and ((Get-Content -LiteralPath $cfg -Raw) -match '(?im)^include-system-site-packages\s*=\s*true')
            if (-not $valid -or $shared) { throw 'Existing .venv is unusable or shares system packages. Run setup.cmd -RecreateVenv to preserve it and create an isolated environment.' }
        }
    }
    $AgentUv = Install-AgentUv
    Invoke-AgentCommand $AgentUv @('--version')
    if (-not $Python) { $Python = Get-AgentManagedPython }
    $syncArgs = @('sync', '--locked', '--python', $Python, '--no-python-downloads')
    # Retain separately installed OSWorld dependencies in an existing desktop venv.
    if ($Profile -eq 'desktop') { $syncArgs += '--inexact' }
    Write-Host '[setup] uv sync: Python 3.12, locked dependencies and editable project...'
    Invoke-AgentCommand $AgentUv $syncArgs
    if (-not (Test-AgentPython $AgentPython 'import sys; sys.exit(0 if sys.version_info[:2] == (3,12) and sys.maxsize > 2**32 else 1)')) {
        throw 'Expected an x64 Python 3.12 environment.'
    }
    if ($Profile -eq 'desktop') {
        $requirements = $env:OSWORLD_DESKTOP_REQUIREMENTS
        if (-not $requirements) { $requirements = Join-Path $env:OSWORLD_DESKTOP_ENV_PATH 'requirements.txt' }
        if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) { throw 'External Windows requirements file is missing; set OSWORLD_DESKTOP_REQUIREMENTS.' }
        $constraints = Join-Path $AgentRoot '.tools\desktop-constraints.txt'
        New-Item -ItemType Directory -Path (Split-Path -Parent $constraints) -Force | Out-Null
        Invoke-AgentCommand $AgentUv @('export', '--locked', '--no-emit-project', '--no-hashes', '--no-dev', '--output-file', $constraints)
        Push-Location -LiteralPath $env:OSWORLD_DESKTOP_ENV_PATH
        try { Invoke-AgentCommand $AgentUv @('pip', 'install', '--python', $AgentPython, '-r', $requirements, '-c', $constraints) }
        finally { Pop-Location }
    }
    Invoke-AgentCommand $AgentUv @('pip', 'check', '--python', $AgentPython)
    $needsChromium = $Profile -ne 'computer' -or
        ($env:COMPUTER_BROWSER_CHANNEL -eq 'chromium' -and
         -not $env:COMPUTER_BROWSER_EXECUTABLE -and -not $env:COMPUTER_CDP_ENDPOINT)
    if ($needsChromium) { Invoke-AgentModule 'playwright' @('install', 'chromium') }
    if ($Profile -ne 'computer' -and -not $SkipSmoke) {
        Invoke-AgentModule 'osworld_agent.script.smoke_browser_windows' @('--channel', $env:OSWORLD_BROWSER_CHANNEL)
    }
    if ($Profile -eq 'desktop') { Invoke-AgentModule 'osworld_agent.script.doctor' @('--desktop') }
    if ($Profile -eq 'computer') { Invoke-AgentModule 'osworld_agent.script.doctor' @('--computer') }
    @{fingerprint = Get-AgentEnvironmentFingerprint; profile = $Profile} | ConvertTo-Json |
        Set-Content -LiteralPath (Join-Path $venvDir '.osworld-uv-ready.json') -Encoding ASCII
    switch ($Profile) {
        'computer' {
            Write-Host '[setup] Ready. Desktop checks do not inject input. Run run.cmd -Mode computer-smoke for the scripted fixture; configure .env, then run run.cmd -Mode computer-acceptance for model acceptance.'
        }
        'desktop' {
            Write-Host '[setup] Ready. Configure .env, then run run.cmd -Mode vm -TaskId <task-id>, or run.cmd -Mode viz. See docs/WINDOWS_AGENT_RUNBOOK.md.'
        }
        default {
            Write-Host '[setup] Ready. Configure .env, then run run.cmd -Mode acceptance. See docs/WINDOWS_AGENT_RUNBOOK.md.'
        }
    }
    exit 0
} catch {
    Write-Host ("[setup] ERROR: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
