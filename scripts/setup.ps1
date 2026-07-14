param(
    [switch]$SkipInstall,
    [switch]$SkipMcpConfig
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PluginRoot = Split-Path -Parent $ScriptDir
$Launcher = Join-Path $PluginRoot "scripts\fusion_agent_codex_mcp_launcher.py"
$DefaultPython = Join-Path $PluginRoot ".venv\Scripts\python.exe"
$Python = if ($env:FUSION_AGENT_PYTHON) { $env:FUSION_AGENT_PYTHON } else { $DefaultPython }
$WheelsRoot = Join-Path $PluginRoot "wheels"

$Wheels = @(Get-ChildItem -Path $WheelsRoot -Filter "fusion_agent_harness-*.whl" -ErrorAction SilentlyContinue)
if ($Wheels.Count -gt 1) {
    throw "Expected exactly one bundled harness wheel, found $($Wheels.Count)."
}
$Wheel = if ($Wheels.Count -eq 1) { $Wheels[0] } else { $null }

if (-not (Test-Path $Python)) {
    if ($env:FUSION_AGENT_PYTHON) {
        throw "FUSION_AGENT_PYTHON does not exist: $Python"
    }
    $SystemPython = Get-Command python -ErrorAction SilentlyContinue
    if (-not $SystemPython) {
        throw "Could not find Python. Install Python 3.11+ or set FUSION_AGENT_PYTHON."
    }
    & $SystemPython.Source -m venv (Join-Path $PluginRoot ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the plugin virtual environment." }
}

Push-Location $PluginRoot
try {
    if (-not $SkipInstall) {
        if ($Wheel) {
            & $Python -m pip install --force-reinstall $Wheel.FullName
            if ($LASTEXITCODE -ne 0) { throw "Failed to install $($Wheel.FullName)." }
        }
        elseif ($env:FUSION_AGENT_HARNESS_ROOT) {
            & $Python -m pip install -e $env:FUSION_AGENT_HARNESS_ROOT
            if ($LASTEXITCODE -ne 0) { throw "Failed to install FUSION_AGENT_HARNESS_ROOT." }
        }
        else {
            throw "Missing bundled fusion_agent_harness wheel under $WheelsRoot. Build the plugin with scripts\build-distribution.py."
        }
    }

    if (-not $SkipMcpConfig) {
        & $Python (Join-Path $PluginRoot "scripts\configure_mcp.py") --plugin-root $PluginRoot --python $Python
        if ($LASTEXITCODE -ne 0) { throw "Failed to configure .mcp.json." }
    }

    & $Python $Launcher --check
    if ($LASTEXITCODE -ne 0) { throw "Fusion Agent launcher check failed." }
    & $Python (Join-Path $PluginRoot "scripts\validate_plugin.py")
    if ($LASTEXITCODE -ne 0) { throw "Fusion Agent plugin validation failed." }
    & $Python -c "import fusion_agent_mcp; from importlib.metadata import version; installed=version('fusion-agent-harness'); print(f'fusion-agent-harness: {installed}'); assert installed == fusion_agent_mcp.__version__ == '0.2.1', (installed, fusion_agent_mcp.__version__)"
    if ($LASTEXITCODE -ne 0) { throw "Installed Fusion Agent version validation failed." }
    & $Python -c "from fusion_agent_mcp.server import tool_specs; count=len(tool_specs()); print(f'fusion_agent MCP tools: {count}'); assert count == 35, count"
    if ($LASTEXITCODE -ne 0) { throw "Installed Fusion Agent tool-surface validation failed." }
}
finally {
    Pop-Location
}
