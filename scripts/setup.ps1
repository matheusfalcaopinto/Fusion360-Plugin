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
$VenvRoot = Join-Path $PluginRoot ".venv"
$DefaultPython = Join-Path $PluginRoot ".venv\Scripts\python.exe"
$Python = $DefaultPython
$WheelsRoot = Join-Path $PluginRoot "wheels"
$RequirementsRoot = Join-Path $PluginRoot "harness\requirements"
$RuntimeLock = Join-Path $RequirementsRoot "runtime.lock"
$FaustLock = Join-Path $RequirementsRoot "faust.lock"
$BuildLock = Join-Path $RequirementsRoot "build.lock"

$Wheels = @(Get-ChildItem -Path $WheelsRoot -Filter "fusion_agent_harness-*.whl" -ErrorAction SilentlyContinue)
$DevelopmentSourceRoot = $env:FUSION_AGENT_HARNESS_ROOT
if ($Wheels.Count -eq 1 -and $DevelopmentSourceRoot) {
    throw "FUSION_AGENT_HARNESS_ROOT is forbidden when the release wheel is bundled. Clear the development override."
}
if ($Wheels.Count -ne 1 -and -not ($Wheels.Count -eq 0 -and $DevelopmentSourceRoot)) {
    throw "Expected exactly one bundled harness wheel before setup, found $($Wheels.Count). Set FUSION_AGENT_HARNESS_ROOT only for the documented non-release development override."
}
$Wheel = if ($Wheels.Count -eq 1) { $Wheels[0] } else { $null }
if (-not $Wheel) {
    $DevelopmentSourceRoot = (Resolve-Path -LiteralPath $DevelopmentSourceRoot -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath (Join-Path $DevelopmentSourceRoot "pyproject.toml") -PathType Leaf)) {
        throw "FUSION_AGENT_HARNESS_ROOT must contain pyproject.toml before the development override can alter the environment."
    }
    Write-Warning "Using the explicit FUSION_AGENT_HARNESS_ROOT development override; no release wheel is being verified or installed."
}

# Verify the bundle with a trusted, already-available interpreter before this
# script creates a venv or lets pip process any wheel member.
if ($Wheel) {
    $VerifierPython = $null
    if ($env:FUSION_AGENT_PYTHON -and (Test-Path $env:FUSION_AGENT_PYTHON)) {
        $ExplicitVerifier = [System.IO.Path]::GetFullPath($env:FUSION_AGENT_PYTHON)
        $ExactVenvPrefix = [System.IO.Path]::GetFullPath($VenvRoot).TrimEnd('\') + '\'
        if ($ExplicitVerifier.StartsWith($ExactVenvPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "FUSION_AGENT_PYTHON must not point into the pre-existing plugin .venv used for preinstall verification."
        }
        $VerifierPython = $env:FUSION_AGENT_PYTHON
    }
    else {
        $ExactVenvPrefix = [System.IO.Path]::GetFullPath($VenvRoot).TrimEnd('\') + '\'
        foreach ($SystemPythonForVerification in @(Get-Command python -All -ErrorAction SilentlyContinue)) {
            if (-not $SystemPythonForVerification.Source) { continue }
            $Candidate = [System.IO.Path]::GetFullPath($SystemPythonForVerification.Source)
            if (-not $Candidate.StartsWith($ExactVenvPrefix, [System.StringComparison]::OrdinalIgnoreCase) -and $Candidate -notmatch '\\WindowsApps\\python(?:3)?\.exe$') {
                $VerifierPython = $Candidate
                break
            }
        }
    }
    if (-not $VerifierPython) {
        throw "A pre-existing Python 3.11+ interpreter is required to verify the plugin bundle before installation."
    }
    & $VerifierPython -I -S -B (Join-Path $PluginRoot "scripts\preinstall_verify.py") `
        --plugin-root $PluginRoot --wheel $Wheel.FullName
    if ($LASTEXITCODE -ne 0) { throw "Fusion Agent preinstall bundle verification failed." }
}

if (-not $SkipInstall) {
    $VenvItem = Get-Item -LiteralPath $VenvRoot -Force -ErrorAction SilentlyContinue
    if ($VenvItem) {
        if (($VenvItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing to replace a virtual environment that is a reparse point."
        }
        $ResolvedPluginRoot = (Resolve-Path -LiteralPath $PluginRoot).Path
        $ResolvedVenvRoot = (Resolve-Path -LiteralPath $VenvRoot).Path
        if ((Split-Path -Parent $ResolvedVenvRoot) -ne $ResolvedPluginRoot -or (Split-Path -Leaf $ResolvedVenvRoot) -ne ".venv") {
            throw "Refusing to replace a virtual environment outside the exact plugin root."
        }
        Remove-Item -LiteralPath $ResolvedVenvRoot -Recurse -Force
    }
}

if (-not (Test-Path $Python)) {
    $BootstrapPython = $null
    if ($env:FUSION_AGENT_PYTHON -and (Test-Path $env:FUSION_AGENT_PYTHON)) {
        $BootstrapPython = $env:FUSION_AGENT_PYTHON
    }
    else {
        $SystemPython = Get-Command python -ErrorAction SilentlyContinue
        if ($SystemPython) { $BootstrapPython = $SystemPython.Source }
    }
    if (-not $BootstrapPython) {
        throw "Could not find Python. Install Python 3.11+ or set FUSION_AGENT_PYTHON."
    }
    & $BootstrapPython -I -B -m venv $VenvRoot
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the plugin virtual environment." }
}

Push-Location $PluginRoot
try {
    $IsolatedPip = Join-Path $PluginRoot "scripts\run-isolated-pip.py"
    $Wheelhouse = Join-Path $VenvRoot ".fusion-agent-wheelhouse"
    if (-not $SkipInstall) {
        $SelectedLock = if ($Backend -eq "faust_stdio") { $FaustLock } else { $RuntimeLock }
        New-Item -ItemType Directory -Path $Wheelhouse -Force | Out-Null
        & $Python -I -S -B $IsolatedPip download --require-hashes --only-binary=:all: --dest $Wheelhouse -r $SelectedLock
        if ($LASTEXITCODE -ne 0) { throw "Failed to download the hash-locked Fusion Agent dependency wheelhouse." }
        & $Python -I -S -B $IsolatedPip install --no-compile --no-index --find-links $Wheelhouse --require-hashes --only-binary=:all: -r $SelectedLock
        if ($LASTEXITCODE -ne 0) { throw "Failed to install hash-locked Fusion Agent runtime dependencies." }
        if ($Wheel) {
            & $Python -I -S -B $IsolatedPip install --no-compile --force-reinstall --no-deps $Wheel.FullName
            if ($LASTEXITCODE -ne 0) { throw "Failed to install $($Wheel.FullName)." }
        }
        elseif ($DevelopmentSourceRoot) {
            & $Python -I -S -B $IsolatedPip install --no-compile --require-hashes --only-binary=:all: -r $BuildLock
            if ($LASTEXITCODE -ne 0) { throw "Failed to install hash-locked PEP 517 backends." }
            & $Python -I -S -B $IsolatedPip install --no-compile --no-deps --no-build-isolation -e $DevelopmentSourceRoot
            if ($LASTEXITCODE -ne 0) { throw "Failed to install FUSION_AGENT_HARNESS_ROOT." }
        }
        else {
            throw "Missing bundled fusion_agent_harness wheel under $WheelsRoot. Build the plugin with scripts\build-distribution.py."
        }
    }

    if ($Wheel) {
        $InstalledDependencyLock = if ($Backend -eq "faust_stdio") { "faust.lock" } else { "runtime.lock" }
        & $Python -I -S -B (Join-Path $PluginRoot "scripts\preinstall_verify.py") `
            --plugin-root $PluginRoot --wheel $Wheel.FullName --verify-installed `
            --dependency-lock $InstalledDependencyLock --dependency-wheelhouse $Wheelhouse
        if ($LASTEXITCODE -ne 0) { throw "Installed Fusion Agent wheel verification failed." }
        & $Python -I -S -B $IsolatedPip check
        if ($LASTEXITCODE -ne 0) { throw "Installed Fusion Agent dependency graph is inconsistent." }
    }

    if (-not $SkipMcpConfig) {
        $ConfigureArgs = @(
            (Join-Path $PluginRoot "scripts\configure_mcp.py"),
            "--plugin-root", $PluginRoot,
            "--python", $Python,
            "--require-contained-runtime",
            "--tool-profile", $ToolProfile,
            "--backend", $Backend
        )
        if ($FaustCommand) { $ConfigureArgs += @("--faust-command", $FaustCommand) }
        if ($FusionDataUrl) { $ConfigureArgs += @("--fusion-data-url", $FusionDataUrl) }
        if ($EnableFusionData) { $ConfigureArgs += "--enable-fusion-data" }
        & $Python -I -B @ConfigureArgs
        if ($LASTEXITCODE -ne 0) { throw "Failed to configure .mcp.json." }
    }

    & $Python -I -B $Launcher --check
    if ($LASTEXITCODE -ne 0) { throw "Fusion Agent launcher check failed." }
    $env:FUSION_AGENT_EXPECTED_TOOL_PROFILE = $ToolProfile
    $env:FUSION_AGENT_EXPECTED_BACKEND = $Backend
    & $Python -I -B (Join-Path $PluginRoot "scripts\validate_plugin.py")
    if ($LASTEXITCODE -ne 0) { throw "Fusion Agent plugin validation failed." }
    & $Python -I -B -c "import fusion_agent_mcp; from importlib.metadata import version; installed=version('fusion-agent-harness'); print(f'fusion-agent-harness: {installed}'); assert installed == fusion_agent_mcp.__version__, (installed, fusion_agent_mcp.__version__)"
    if ($LASTEXITCODE -ne 0) { throw "Installed Fusion Agent version validation failed." }
    & $Python -I -B -c "from fusion_agent_mcp.server import list_tool_definitions,tool_specs; registry=len(tool_specs()); normal_tools=list_tool_definitions('normal'); normal=len(normal_tools); all_tools=len(list_tool_definitions('all')); print(f'fusion_agent MCP tools: registry={registry}, normal={normal}, all={all_tools}'); assert (registry,normal,all_tools)==(35,12,35),(registry,normal,all_tools); assert all('script' not in tool.inputSchema.get('properties', {}) for tool in normal_tools)"
    if ($LASTEXITCODE -ne 0) { throw "Installed Fusion Agent tool-surface validation failed." }
}
finally {
    Pop-Location
}
