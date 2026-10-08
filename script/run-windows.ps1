param(
    [ValidateSet('agent', 'smoke', 'acceptance', 'vm', 'viz', 'doctor')][string]$Mode = 'agent',
    [ValidateSet('browser', 'desktop')][string]$Profile = 'browser',
    [string]$Task = '', [string]$Url = '',
    [string]$Model = '', [string]$ApiUrl = '',
    [string]$TaskId = '', [string]$Domain = 'all',
    [string]$Output = 'artifacts/browser-agent',
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
    Import-AgentEnv
    if ($Mode -in @('vm', 'viz')) { $Profile = 'desktop' }
    $AgentUv = Find-AgentUv
    $ready = $false
    $stampFile = Join-Path $AgentRoot '.venv\.osworld-uv-ready.json'
    if ($AgentUv -and (Test-Path -LiteralPath $AgentPython) -and (Test-Path -LiteralPath $stampFile)) {
        $stamp = Get-Content -LiteralPath $stampFile -Raw | ConvertFrom-Json
        $ready = ($stamp.fingerprint -eq (Get-AgentEnvironmentFingerprint)) -and
            (Test-AgentPython $AgentPython 'import osworld_agent.agent, osworld_agent.viz.server, playwright, pytest')
        if ($Profile -eq 'desktop' -and $stamp.profile -ne 'desktop') { $ready = $false }
    }
    if (-not $ready) {
        Write-Host '[run] First-run setup...'
        Invoke-AgentCommand 'powershell.exe' @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            (Join-Path $PSScriptRoot 'setup-windows.ps1'), '-Profile', $Profile, '-SkipSmoke')
        Import-AgentEnv
        $AgentUv = Find-AgentUv
    }
    if (-not $Channel) { $Channel = $env:OSWORLD_BROWSER_CHANNEL }
    switch ($Mode) {
        'smoke' {
            Invoke-AgentModule 'osworld_agent.script.smoke_browser_windows' @('--channel', $Channel)
        }
        'acceptance' {
            if ($Task -or $Url -or $Profile -eq 'desktop') { throw 'Acceptance uses the built-in browser fixture; use -Mode agent or vm for custom tasks.' }
            if ($Model) { $env:PLAN_MODEL = $Model }
            if ($ApiUrl) { $env:PLAN_API_URL = $ApiUrl }
            $acceptArgs = @('--channel', $Channel, '--max-steps', [string]$MaxSteps)
            if ($Headless) { $acceptArgs += '--headless' }
            Invoke-AgentModule 'osworld_agent.script.acceptance_windows' $acceptArgs
        }
        'doctor' {
            $doctorArgs = @()
            if ($Profile -eq 'desktop') { $doctorArgs += '--desktop' }
            if ($CheckApi) { $doctorArgs += '--check-api' }
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
            if (-not $GroundType) { $GroundType = $env:GROUNDING_PROTOCOL }
            if ($GroundType) { $vmArgs += @('--ground-type', $GroundType) }
            if ($Headless) { $vmArgs += '--headless' }
            Invoke-AgentModule 'osworld_agent.viz.runner' $vmArgs
        }
        'agent' {
            if ($Profile -eq 'desktop') { throw 'Use -Mode viz -Profile desktop to run VMware tasks from the Web GUI.' }
            if (-not $Model) { $Model = $env:PLAN_MODEL }
            if (-not $ApiUrl) { $ApiUrl = $env:PLAN_API_URL }
            if (-not $Model -or -not $ApiUrl -or -not $env:PLAN_API_KEY) {
                throw 'Fill PLAN_MODEL, PLAN_API_URL, PLAN_API_KEY in .env first. Use -Mode smoke to verify installation without a model.'
            }
            $runnerArgs = @('--model', $Model, '--api-url', $ApiUrl,
                '--channel', $Channel, '--max-steps', [string]$MaxSteps, '--output', $Output)
            if (-not $Headless) { $runnerArgs += '--headed' }
            if ($Task) { $runnerArgs += @('--task', $Task) }
            if ($Url) { $runnerArgs += @('--url', $Url) }
            if ($TrustEnv) { $runnerArgs += '--trust-env' }
            if ($env:PLAN_THINKING_STYLE) { $runnerArgs += @('--thinking-style', $env:PLAN_THINKING_STYLE) }
            if ($env:GROUNDING_API_URL) {
                $runnerArgs += @('--ground-url', $env:GROUNDING_API_URL)
                if ($env:GROUNDING_MODEL) { $runnerArgs += @('--ground-model', $env:GROUNDING_MODEL) }
                if (-not $GroundType) { $GroundType = $env:GROUNDING_PROTOCOL }
                if ($GroundType) { $runnerArgs += @('--ground-type', $GroundType) }
            }
            Invoke-AgentModule 'osworld_agent.run_browser_windows' $runnerArgs
        }
    }
    exit 0
} catch {
    Write-Host ("[run] ERROR: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
