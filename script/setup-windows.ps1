param(
    [ValidateSet('browser', 'desktop')][string]$Profile = 'browser',
    [string]$Python = '',
    [switch]$SkipSmoke
)
. (Join-Path $PSScriptRoot 'windows-common.ps1')
try {
    Set-Location -LiteralPath $AgentRoot
    if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
    if (-not (Test-Path -LiteralPath 'config\model_presets.local.yaml')) {
        Copy-Item -LiteralPath 'config\model_presets.yaml' -Destination 'config\model_presets.local.yaml'
    }
    Import-AgentEnv
    if ($Profile -eq 'desktop') {
        if (-not $env:OSWORLD_DESKTOP_ENV_PATH -or
            -not (Test-Path -LiteralPath (Join-Path $env:OSWORLD_DESKTOP_ENV_PATH 'desktop_env') -PathType Container)) {
            throw 'Desktop mode requires an external OSWorld checkout. Set OSWORLD_DESKTOP_ENV_PATH in .env; see README.md.'
        }
    }
    $venvReady = $false
    if (Test-Path -LiteralPath $AgentPython) {
        $venvReady = Test-AgentPython $AgentPython 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) and sys.maxsize > 2**32 else 1)'
        if (-not $venvReady) { throw 'The existing .venv is unusable. Rename it to .venv.old and rerun setup; never copy a venv between computers.' }
    }
    if (-not $venvReady) {
        if (-not $Python) { $Python = Find-AgentPython }
        if (-not $Python) {
            if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
                throw 'Install Python 3.12 (64-bit) from python.org, then run setup.cmd again. WinGet is unavailable.'
            }
            Write-Host '[setup] Installing Python 3.12 for this Windows user...'
            Invoke-AgentCommand 'winget.exe' @('install', '--id', 'Python.Python.3.12', '--exact', '--source', 'winget',
                '--scope', 'user', '--silent', '--accept-package-agreements', '--accept-source-agreements')
            $Python = Find-AgentPython
            if (-not $Python) { throw 'Python was installed but could not be found. Rerun setup.cmd or pass -Python C:\path\python.exe.' }
        }
        Invoke-AgentCommand $Python @('-c', 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) and sys.maxsize > 2**32 else 1)')
        Write-Host '[setup] Creating isolated .venv...'
        Invoke-AgentCommand $Python @('-m', 'venv', (Join-Path $AgentRoot '.venv'))
    }
    Write-Host '[setup] Installing the agent and dependencies...'
    Invoke-AgentCommand $AgentPython @('-m', 'pip', 'install', '--upgrade', 'pip')
    if ($Profile -eq 'desktop') {
        $desktopRequirements = $env:OSWORLD_DESKTOP_REQUIREMENTS
        if (-not $desktopRequirements) { $desktopRequirements = Join-Path $env:OSWORLD_DESKTOP_ENV_PATH 'requirements.txt' }
        if (-not (Test-Path -LiteralPath $desktopRequirements -PathType Leaf)) {
            throw 'External OSWorld requirements.txt is missing. Set OSWORLD_DESKTOP_REQUIREMENTS to its Windows dependency file.'
        }
        # Keep any relative paths in upstream requirements relative to that checkout.
        Push-Location -LiteralPath $env:OSWORLD_DESKTOP_ENV_PATH
        try { Invoke-AgentCommand $AgentPython @('-m', 'pip', 'install', '-r', $desktopRequirements) }
        finally { Pop-Location }
    }
    Invoke-AgentCommand $AgentPython @('-m', 'pip', 'install', '-r',
        (Join-Path $AgentRoot 'requirements-windows.lock.txt'), '-e', $AgentRoot)
    Invoke-AgentCommand $AgentPython @('-m', 'pip', 'check')
    Write-Host '[setup] Installing Playwright Chromium...'
    Invoke-AgentCommand $AgentPython @('-m', 'playwright', 'install', 'chromium')
    if (-not $SkipSmoke) {
        Invoke-AgentCommand $AgentPython @('-m', 'osworld_agent.script.smoke_browser_windows', '--channel', $env:OSWORLD_BROWSER_CHANNEL)
    }
    if ($Profile -eq 'desktop') {
        Invoke-AgentCommand $AgentPython @('-m', 'osworld_agent.script.doctor', '--desktop')
    }
    Write-Host '[setup] Ready. Edit .env for your model, then run run.cmd. For a credential-free smoke: run.cmd -Mode smoke'
    exit 0
} catch {
    Write-Host ("[setup] ERROR: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
