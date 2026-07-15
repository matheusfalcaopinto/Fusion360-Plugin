param(
    [switch]$SkipInstall,
    [switch]$SkipMcpConfig,
    [ValidateSet("normal", "advanced", "diagnostic", "benchmark", "all")]
    [string]$ToolProfile = "normal",
    [ValidateSet("autodesk_http", "faust_stdio")]
    [string]$Backend = "autodesk_http",
    [string]$FaustCommand,
    [string]$FusionDataUrl,
    [switch]$EnableFusionData
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
        if ($Backend -eq "faust_stdio") {
            & $Python -m pip install "fusion360-mcp-server==0.1.0"
            if ($LASTEXITCODE -ne 0) { throw "Failed to install optional Faust backend 0.1.0." }
        }
    }

    if (-not $SkipMcpConfig) {
        $ConfigureArgs = @(
            (Join-Path $PluginRoot "scripts\configure_mcp.py"),
            "--plugin-root", $PluginRoot,
            "--python", $Python,
            "--tool-profile", $ToolProfile,
            "--backend", $Backend
        )
        if ($FaustCommand) { $ConfigureArgs += @("--faust-command", $FaustCommand) }
        if ($FusionDataUrl) { $ConfigureArgs += @("--fusion-data-url", $FusionDataUrl) }
        if ($EnableFusionData) { $ConfigureArgs += "--enable-fusion-data" }
        & $Python @ConfigureArgs
        if ($LASTEXITCODE -ne 0) { throw "Failed to configure .mcp.json." }
    }

    & $Python $Launcher --check
    if ($LASTEXITCODE -ne 0) { throw "Fusion Agent launcher check failed." }
    $env:FUSION_AGENT_EXPECTED_TOOL_PROFILE = $ToolProfile
    $env:FUSION_AGENT_EXPECTED_BACKEND = $Backend
    & $Python (Join-Path $PluginRoot "scripts\validate_plugin.py")
    if ($LASTEXITCODE -ne 0) { throw "Fusion Agent plugin validation failed." }
    & $Python -c "import fusion_agent_mcp; from importlib.metadata import version; installed=version('fusion-agent-harness'); print(f'fusion-agent-harness: {installed}'); assert installed == fusion_agent_mcp.__version__, (installed, fusion_agent_mcp.__version__)"
    if ($LASTEXITCODE -ne 0) { throw "Installed Fusion Agent version validation failed." }
    & $Python -c "from fusion_agent_mcp.server import list_tool_definitions,tool_specs; registry=len(tool_specs()); normal=len(list_tool_definitions('normal')); all_tools=len(list_tool_definitions('all')); print(f'fusion_agent MCP tools: registry={registry}, normal={normal}, all={all_tools}'); assert (registry,normal,all_tools)==(35,12,35),(registry,normal,all_tools)"
    if ($LASTEXITCODE -ne 0) { throw "Installed Fusion Agent tool-surface validation failed." }
}
finally {
    Pop-Location
}
