param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PluginRoot = Split-Path -Parent $ScriptDir
$Launcher = Join-Path $PluginRoot "scripts\fusion_agent_codex_mcp_launcher.py"
$Python = Join-Path $PluginRoot ".venv\Scripts\python.exe"
$WheelsRoot = Join-Path $PluginRoot "wheels"

$Wheel = Get-ChildItem -Path $WheelsRoot -Filter "fusion_agent_harness-*.whl" -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending |
    Select-Object -First 1

if (-not (Test-Path $Python)) {
    $SystemPython = Get-Command python -ErrorAction SilentlyContinue
    if (-not $SystemPython) {
        throw "Could not find Python. Install Python 3.11+ or set FUSION_AGENT_PYTHON."
    }
    & $SystemPython.Source -m venv (Join-Path $PluginRoot ".venv")
}

Push-Location $PluginRoot
try {
    if (-not $SkipInstall) {
        if ($Wheel) {
            & $Python -m pip install --force-reinstall $Wheel.FullName
        }
        elseif ($env:FUSION_AGENT_HARNESS_ROOT) {
            & $Python -m pip install -e $env:FUSION_AGENT_HARNESS_ROOT
        }
        else {
            throw "Missing bundled fusion_agent_harness wheel under $WheelsRoot. Build the plugin with scripts\build-distribution.py."
        }
    }

    & $Python $Launcher --check
    & $Python (Join-Path $PluginRoot "scripts\validate_plugin.py")
    & $Python -c "from fusion_agent_mcp.server import tool_specs; print(f'fusion_agent MCP tools: {len(tool_specs())}')"
}
finally {
    Pop-Location
}
