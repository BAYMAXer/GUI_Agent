param(
    [ValidateSet('agent', 'smoke', 'viz', 'doctor')][string]$Mode = 'agent',
    [ValidateSet('browser', 'desktop')][string]$Profile = 'browser',
    [string]$Task = '', [string]$Url = '',
    [string]$Model = '', [string]$ApiUrl = '',
    [ValidateSet('auto', 'chromium', 'chrome', 'msedge')][string]$Channel = '',
    [int]$MaxSteps = 12,
    [switch]$Headless,
    [switch]$CheckApi,
    [switch]$NoBrowser
)
. (Join-Path $PSScriptRoot 'windows-common.ps1')
try {
    Set-Location -LiteralPath $AgentRoot
    Import-AgentEnv
    $ready = $false
    if (Test-Path -LiteralPath $AgentPython) {
        $ready = Test-AgentPython $AgentPython 'import osworld_agent.agent, osworld_agent.viz.server, playwright, pytest'
    }
    if (-not $ready) {
        Write-Host '[run] First-run setup...'
        Invoke-AgentCommand 'powershell.exe' @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            (Join-Path $PSScriptRoot 'setup-windows.ps1'), '-Profile', $Profile, '-SkipSmoke')
        Import-AgentEnv
    }
    if (-not $Channel) { $Channel = $env:OSWORLD_BROWSER_CHANNEL }
    switch ($Mode) {
        'smoke' {
            Invoke-AgentCommand $AgentPython @('-m', 'osworld_agent.script.smoke_browser_windows', '--channel', $Channel)
        }
        'doctor' {
            $doctorArgs = @('-m', 'osworld_agent.script.doctor')
            if ($Profile -eq 'desktop') { $doctorArgs += '--desktop' }
            if ($CheckApi) { $doctorArgs += '--check-api' }
            Invoke-AgentCommand $AgentPython $doctorArgs
        }
        'viz' {
            Invoke-AgentCommand $AgentPython @('-m', 'osworld_agent.script.doctor', '--desktop')
            $vizUrl = 'http://127.0.0.1:' + $env:AGENTS_VIZ_PORT
            Write-Host "[run] Web GUI: $vizUrl (Ctrl+C to stop)"
            # The helper waits for this server to respond before opening the page.
            if (-not $NoBrowser) {
                Start-Process -FilePath $AgentPython -WindowStyle Hidden -ArgumentList @('-m', 'osworld_agent.script.open_viz', $vizUrl)
            }
            Invoke-AgentCommand $AgentPython @('-m', 'osworld_agent.viz.server')
        }
        'agent' {
            if ($Profile -eq 'desktop') { throw 'Use -Mode viz -Profile desktop to run VMware tasks from the Web GUI.' }
            if (-not $Model) { $Model = $env:QWEN_MODEL }
            if (-not $ApiUrl) { $ApiUrl = $env:QWEN_API_URL }
            if (-not $Model -or -not $ApiUrl -or -not $env:QWEN_API_KEY) {
                throw 'Fill QWEN_MODEL, QWEN_API_URL, QWEN_API_KEY in .env first. Use -Mode smoke to verify installation without a model.'
            }
            $runnerArgs = @('-m', 'osworld_agent.run_browser_windows', '--model', $Model, '--api-url', $ApiUrl,
                '--channel', $Channel, '--max-steps', [string]$MaxSteps)
            if (-not $Headless) { $runnerArgs += '--headed' }
            if ($Task) { $runnerArgs += @('--task', $Task) }
            if ($Url) { $runnerArgs += @('--url', $Url) }
            if ($env:QWEN_THINKING_STYLE) { $runnerArgs += @('--thinking-style', $env:QWEN_THINKING_STYLE) }
            if ($env:GROUNDING_API_URL) {
                $runnerArgs += @('--ground-url', $env:GROUNDING_API_URL)
                if ($env:GROUNDING_MODEL) { $runnerArgs += @('--ground-model', $env:GROUNDING_MODEL) }
            }
            Invoke-AgentCommand $AgentPython $runnerArgs
        }
    }
    exit 0
} catch {
    Write-Host ("[run] ERROR: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
