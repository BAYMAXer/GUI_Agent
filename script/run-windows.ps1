param(
    [ValidateSet('agent', 'smoke', 'computer-smoke', 'computer-acceptance', 'acceptance', 'vm', 'viz', 'doctor')][string]$Mode = 'agent',
    [ValidateSet('browser', 'computer', 'desktop')][string]$Profile = 'browser',
    [string]$Task = '', [string]$Url = '',
    [string]$Model = '', [string]$ApiUrl = '',
    [string]$TaskId = '', [string]$Domain = 'all',
    [string]$Output = '',
    [string]$BrowserExecutable = '', [string]$BrowserProfileDir = '', [string]$CdpEndpoint = '',
    [string]$DownloadDir = '',
    [string]$Monitor = '',
    [ValidateSet('auto', 'structured', 'pixel', 'normalized')][string]$GroundType = '',
    [ValidateSet('auto', 'chromium', 'chrome', 'msedge')][string]$Channel = '',
    [int]$MaxSteps = 12,
    [switch]$Headless,
    [switch]$CheckApi,
    [switch]$TrustEnv,
    [switch]$NoBrowser
)
. (Join-Path $PSScriptRoot 'windows-common.ps1')
try {
    Set-Location -LiteralPath $AgentRoot
    if ($Mode -in @('vm', 'viz')) { $Profile = 'desktop' }
    if ($Mode -in @('computer-smoke', 'computer-acceptance')) { $Profile = 'computer' }
    $isolatedComputerBrowser = $Mode -in @('computer-smoke', 'computer-acceptance')
    # Reject invalid task commands before installation or desktop checks.
    if ($Profile -eq 'computer' -and $Headless) {
        throw 'Computer use requires a visible interactive Windows desktop; omit -Headless.'
    }
    if ($Mode -eq 'agent' -and $Profile -eq 'computer' -and -not $Task.Trim()) {
        throw 'Provide -Task for Windows computer use.'
    }
    if ($Mode -eq 'computer-acceptance' -and
        ($Task -or $Url -or $BrowserProfileDir -or $CdpEndpoint -or $DownloadDir)) {
        throw 'Computer acceptance uses its own task, URL, profile and download folder; omit -Task/-Url/-BrowserProfileDir/-CdpEndpoint/-DownloadDir.'
    }
    if ($Mode -eq 'acceptance' -and ($Task -or $Url -or $Profile -ne 'browser')) {
        throw 'Acceptance uses the built-in browser fixture; use -Mode agent or vm for custom tasks.'
    }
    if ($Profile -eq 'computer' -and -not $PSBoundParameters.ContainsKey('MaxSteps')) { $MaxSteps = 30 }
    Import-AgentEnv -IgnoreComputerSession:$isolatedComputerBrowser
    # CLI overrides are inherited by setup, doctor and fixture subprocesses.
    if ($Model) { $env:PLAN_MODEL = $Model }
    if ($ApiUrl) { $env:PLAN_API_URL = $ApiUrl }
    if ($PSBoundParameters.ContainsKey('Channel')) {
        if ($Profile -eq 'computer') { $env:COMPUTER_BROWSER_CHANNEL = $Channel }
        else { $env:OSWORLD_BROWSER_CHANNEL = $Channel }
    }
    if ($Profile -eq 'computer') {
        if ($PSBoundParameters.ContainsKey('Monitor')) { $env:COMPUTER_MONITOR = $Monitor }
        if ($BrowserExecutable) { $env:COMPUTER_BROWSER_EXECUTABLE = $BrowserExecutable }
        if (-not $isolatedComputerBrowser) {
            if ($BrowserProfileDir) { $env:COMPUTER_BROWSER_PROFILE_DIR = $BrowserProfileDir }
            if ($CdpEndpoint) { $env:COMPUTER_CDP_ENDPOINT = $CdpEndpoint }
            if ($DownloadDir) { $env:COMPUTER_DOWNLOAD_DIR = $DownloadDir }
        }
    }
    if ($GroundType) { $env:GROUNDING_PROTOCOL = $GroundType }
    $AgentUv = Find-AgentUv
    $ready = $false
    $stampFile = Join-Path $AgentRoot '.venv\.osworld-uv-ready.json'
    if ($AgentUv -and (Test-Path -LiteralPath $AgentPython) -and (Test-Path -LiteralPath $stampFile)) {
        try {
            $stamp = Get-Content -LiteralPath $stampFile -Raw | ConvertFrom-Json
            $importCheck = 'import osworld_agent.agent, osworld_agent.viz.server, playwright, pytest'
            if ($Profile -eq 'computer') { $importCheck += ', mss, psutil, osworld_agent.adapters.windows_native' }
            $ready = ($stamp.fingerprint -eq (Get-AgentEnvironmentFingerprint)) -and
                (Test-AgentPython $AgentPython $importCheck)
            if ($Profile -eq 'desktop' -and $stamp.profile -ne 'desktop') { $ready = $false }
            if ($ready -and $Profile -eq 'computer' -and $env:COMPUTER_BROWSER_CHANNEL -eq 'chromium' -and
                -not $env:COMPUTER_BROWSER_EXECUTABLE -and -not $env:COMPUTER_CDP_ENDPOINT) {
                # Changing optional browser configuration need not change the dependency lock.
                $chromiumCheck = 'import sys; from pathlib import Path; from playwright.sync_api import sync_playwright; pw=sync_playwright().start(); available=Path(pw.chromium.executable_path).is_file(); pw.stop(); sys.exit(0 if available else 1)'
                $ready = Test-AgentPython $AgentPython $chromiumCheck
            }
        } catch { $ready = $false }
    }
    if (-not $ready) {
        Write-Host '[run] First-run setup...'
        $setupArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            (Join-Path $PSScriptRoot 'setup-windows.ps1'), '-Profile', $Profile, '-SkipSmoke')
        if ($isolatedComputerBrowser) { $setupArgs += '-IsolatedComputerBrowser' }
        Invoke-AgentCommand 'powershell.exe' $setupArgs
        Import-AgentEnv -IgnoreComputerSession:$isolatedComputerBrowser
        $AgentUv = Find-AgentUv
    }
    if (-not $Channel) {
        if ($Profile -eq 'computer') { $Channel = $env:COMPUTER_BROWSER_CHANNEL }
        else { $Channel = $env:OSWORLD_BROWSER_CHANNEL }
    }
    if (-not $Model) { $Model = $env:PLAN_MODEL }
    if (-not $ApiUrl) { $ApiUrl = $env:PLAN_API_URL }
    if (-not $GroundType) { $GroundType = $env:GROUNDING_PROTOCOL }
    $modelArgs = @()
    if ($Model) { $modelArgs += @('--model', $Model) }
    if ($ApiUrl) { $modelArgs += @('--api-url', $ApiUrl) }
    if ($TrustEnv) { $modelArgs += '--trust-env' }
    if ($env:PLAN_THINKING_STYLE) { $modelArgs += @('--thinking-style', $env:PLAN_THINKING_STYLE) }
    if ($env:GROUNDING_API_URL) { $modelArgs += @('--ground-url', $env:GROUNDING_API_URL) }
    if ($env:GROUNDING_MODEL) { $modelArgs += @('--ground-model', $env:GROUNDING_MODEL) }
    if ($GroundType) { $modelArgs += @('--ground-type', $GroundType) }
    switch ($Mode) {
        'smoke' {
            $smokeArgs = @('--channel', $Channel)
            if ($Output) { $smokeArgs += @('--output', $Output) }
            Invoke-AgentModule 'osworld_agent.script.smoke_browser_windows' $smokeArgs
        }
        'computer-smoke' {
            $smokeArgs = @('--channel', $Channel, '--monitor', $env:COMPUTER_MONITOR)
            if ($env:COMPUTER_BROWSER_EXECUTABLE) { $smokeArgs += @('--browser-executable', $env:COMPUTER_BROWSER_EXECUTABLE) }
            if ($Output) { $smokeArgs += @('--output', $Output) }
            Invoke-AgentModule 'osworld_agent.script.smoke_computer_windows' $smokeArgs
        }
        'computer-acceptance' {
            $acceptArgs = $modelArgs + @('--channel', $Channel, '--monitor', $env:COMPUTER_MONITOR, '--max-steps', [string]$MaxSteps)
            if ($env:COMPUTER_BROWSER_EXECUTABLE) { $acceptArgs += @('--browser-executable', $env:COMPUTER_BROWSER_EXECUTABLE) }
            if ($Output) { $acceptArgs += @('--output', $Output) }
            Invoke-AgentModule 'osworld_agent.script.acceptance_computer_windows' $acceptArgs
        }
        'acceptance' {
            $acceptArgs = @('--channel', $Channel, '--max-steps', [string]$MaxSteps)
            if ($Headless) { $acceptArgs += '--headless' }
            Invoke-AgentModule 'osworld_agent.script.acceptance_windows' $acceptArgs
        }
        'doctor' {
            $doctorArgs = @()
            if ($Profile -eq 'desktop') { $doctorArgs += '--desktop' }
            if ($Profile -eq 'computer') { $doctorArgs += @('--computer', '--monitor', $env:COMPUTER_MONITOR) }
            if ($CheckApi) { $doctorArgs += '--check-api' }
            if ($Output) { $doctorArgs += @('--output', $Output) }
            Invoke-AgentModule 'osworld_agent.script.doctor' $doctorArgs
        }
        'viz' {
            Invoke-AgentModule 'osworld_agent.script.doctor' @('--desktop')
            $vizUrl = 'http://127.0.0.1:' + $env:AGENTS_VIZ_PORT
            Write-Host "[run] Web GUI: $vizUrl (Ctrl+C to stop)"
            # The helper waits for this server to respond before opening the page.
            if (-not $NoBrowser) {
                Start-Process -FilePath $AgentPython -WindowStyle Hidden -ArgumentList @('-m', 'osworld_agent.script.open_viz', $vizUrl)
            }
            Invoke-AgentModule 'osworld_agent.viz.server' @()
        }
        'vm' {
            if (-not $TaskId) { throw 'Provide -TaskId for an OSWorld task. The VM will reset to OSWORLD_SNAPSHOT_NAME.' }
            foreach ($name in @('PLAN_MODEL', 'PLAN_API_URL', 'PLAN_API_KEY', 'GROUNDING_MODEL', 'GROUNDING_API_URL', 'GROUNDING_API_KEY')) {
                if (-not [Environment]::GetEnvironmentVariable($name, 'Process')) { throw "Fill $name in .env before a desktop task." }
            }
            Invoke-AgentModule 'osworld_agent.script.doctor' @('--desktop')
            $vmArgs = @('--task-id', $TaskId, '--domain', $Domain, '--examples-dir', $env:OSWORLD_EXAMPLES_DIR,
                '--desktop-env-path', $env:OSWORLD_DESKTOP_ENV_PATH, '--vm-path', $env:OSWORLD_VM_PATH,
                '--model', $env:PLAN_MODEL, '--model-url', $env:PLAN_API_URL,
                '--ground-model', $env:GROUNDING_MODEL, '--ground-url', $env:GROUNDING_API_URL,
                '--max-steps', [string]$MaxSteps)
            if ($GroundType) { $vmArgs += @('--ground-type', $GroundType) }
            if ($Headless) { $vmArgs += '--headless' }
            Invoke-AgentModule 'osworld_agent.viz.runner' $vmArgs
        }
        'agent' {
            if ($Profile -eq 'desktop') { throw 'Use -Mode viz -Profile desktop to run VMware tasks from the Web GUI.' }
            if (-not $Model -or -not $ApiUrl -or -not $env:PLAN_API_KEY) {
                $smokeMode = if ($Profile -eq 'computer') { 'computer-smoke' } else { 'smoke' }
                throw "Fill PLAN_MODEL, PLAN_API_URL, PLAN_API_KEY in .env first. Use -Mode $smokeMode to verify installation without a model."
            }
            $runnerArgs = $modelArgs + @('--channel', $Channel, '--max-steps', [string]$MaxSteps)
            if ($Profile -eq 'computer') {
                $runnerArgs += @('--monitor', $env:COMPUTER_MONITOR)
                if ($BrowserExecutable) { $runnerArgs += @('--browser-executable', $BrowserExecutable) }
                if ($BrowserProfileDir) { $runnerArgs += @('--browser-profile-dir', $BrowserProfileDir) }
                if ($CdpEndpoint) { $runnerArgs += @('--cdp-endpoint', $CdpEndpoint) }
                if ($DownloadDir) { $runnerArgs += @('--download-dir', $DownloadDir) }
            } elseif (-not $Headless) { $runnerArgs += '--headed' }
            if ($Output) { $runnerArgs += @('--output', $Output) }
            if ($Task) { $runnerArgs += @('--task', $Task) }
            if ($Url) { $runnerArgs += @('--url', $Url) }
            if ($Profile -eq 'computer') {
                Invoke-AgentModule 'osworld_agent.run_computer_windows' $runnerArgs
            } else {
                Invoke-AgentModule 'osworld_agent.run_browser_windows' $runnerArgs
            }
        }
    }
    exit 0
} catch {
    Write-Host ("[run] ERROR: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
