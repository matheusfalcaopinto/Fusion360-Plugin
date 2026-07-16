#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PLUGIN_ROOT/.venv/bin/python"
TOOL_PROFILE="${FUSION_AGENT_TOOL_PROFILE:-normal}"
BACKEND="${FUSION_AGENT_BACKEND:-autodesk_http}"
FUSION_DATA_URL="${FUSION_DATA_MCP_URL:-}"
WHEELS_ROOT="$PLUGIN_ROOT/wheels"
WHEELS=()
for candidate in "$WHEELS_ROOT"/fusion_agent_harness-*.whl; do
  [[ -f "$candidate" ]] || continue
  WHEELS+=("$candidate")
done
DEVELOPMENT_SOURCE_ROOT="${FUSION_AGENT_HARNESS_ROOT:-}"
if [[ "${#WHEELS[@]}" -ne 1 ]]; then
  if [[ "${#WHEELS[@]}" -ne 0 || -z "$DEVELOPMENT_SOURCE_ROOT" ]]; then
    echo "Expected exactly one bundled harness wheel before setup, found ${#WHEELS[@]}. Set FUSION_AGENT_HARNESS_ROOT only for the documented non-release development override." >&2
    exit 1
  fi
fi
WHEEL="${WHEELS[0]:-}"
if [[ -z "$WHEEL" ]]; then
  if [[ ! -d "$DEVELOPMENT_SOURCE_ROOT" || ! -f "$DEVELOPMENT_SOURCE_ROOT/pyproject.toml" ]]; then
    echo "FUSION_AGENT_HARNESS_ROOT must be a harness checkout containing pyproject.toml before the development override can alter the environment." >&2
    exit 1
  fi
  DEVELOPMENT_SOURCE_ROOT="$(cd "$DEVELOPMENT_SOURCE_ROOT" && pwd)"
  echo "Warning: using the explicit FUSION_AGENT_HARNESS_ROOT development override; no release wheel is being verified or installed." >&2
fi

# Verify the wheel before creating a venv or invoking pip.  Development source
# overrides remain available, but are explicitly non-canonical and cannot be
# mistaken for a verified release bundle.
if [[ -n "$WHEEL" ]]; then
  VERIFY_PYTHON=""
  if [[ -n "${FUSION_AGENT_PYTHON:-}" && -x "${FUSION_AGENT_PYTHON}" ]]; then
    VERIFY_PYTHON="${FUSION_AGENT_PYTHON}"
  elif [[ -x "$PLUGIN_ROOT/.venv/bin/python" ]]; then
    VERIFY_PYTHON="$PLUGIN_ROOT/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    VERIFY_PYTHON="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    VERIFY_PYTHON="$(command -v python)"
  else
    echo "A pre-existing Python 3.11+ interpreter is required to verify the plugin bundle before installation." >&2
    exit 1
  fi
  "$VERIFY_PYTHON" -E -s -S "$PLUGIN_ROOT/scripts/preinstall_verify.py" --plugin-root "$PLUGIN_ROOT" --wheel "$WHEEL"
fi

if [[ ! -x "$PYTHON" ]]; then
  if [[ -n "${FUSION_AGENT_PYTHON:-}" && -x "${FUSION_AGENT_PYTHON}" ]]; then
    "${FUSION_AGENT_PYTHON}" -m venv "$PLUGIN_ROOT/.venv"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -m venv "$PLUGIN_ROOT/.venv"
  elif command -v python >/dev/null 2>&1; then
    python -m venv "$PLUGIN_ROOT/.venv"
  else
    echo "Could not find python3 or python. Install Python 3.11+ and retry." >&2
    exit 1
  fi
fi

if [[ -n "$WHEEL" ]]; then
  "$PYTHON" -m pip install --force-reinstall "$WHEEL"
elif [[ -n "$DEVELOPMENT_SOURCE_ROOT" ]]; then
  "$PYTHON" -m pip install -e "$DEVELOPMENT_SOURCE_ROOT"
else
  echo "Missing bundled fusion_agent_harness wheel under $WHEELS_ROOT. Build the plugin with scripts/build-distribution.py." >&2
  exit 1
fi
if [[ "$BACKEND" == "faust_stdio" ]]; then
  "$PYTHON" -m pip install "fusion360-mcp-server==0.1.0"
fi
if [[ -n "$WHEEL" ]]; then
  "$PYTHON" "$PLUGIN_ROOT/scripts/preinstall_verify.py" --plugin-root "$PLUGIN_ROOT" --wheel "$WHEEL" --verify-installed
fi
CONFIG_ARGS=(
  "$PLUGIN_ROOT/scripts/configure_mcp.py"
  --plugin-root "$PLUGIN_ROOT"
  --python "$PYTHON"
  --require-contained-runtime
  --tool-profile "$TOOL_PROFILE"
  --backend "$BACKEND"
)
if [[ -n "${FUSION_FAUST_COMMAND:-}" ]]; then
  CONFIG_ARGS+=(--faust-command "$FUSION_FAUST_COMMAND")
fi
if [[ -n "$FUSION_DATA_URL" ]]; then
  CONFIG_ARGS+=(--fusion-data-url "$FUSION_DATA_URL" --enable-fusion-data)
fi
"$PYTHON" "${CONFIG_ARGS[@]}"
"$PYTHON" "$PLUGIN_ROOT/scripts/fusion_agent_codex_mcp_launcher.py" --check
export FUSION_AGENT_EXPECTED_TOOL_PROFILE="$TOOL_PROFILE"
export FUSION_AGENT_EXPECTED_BACKEND="$BACKEND"
"$PYTHON" "$PLUGIN_ROOT/scripts/validate_plugin.py"
"$PYTHON" -c "import fusion_agent_mcp; from importlib.metadata import version; installed=version('fusion-agent-harness'); print(f'fusion-agent-harness: {installed}'); assert installed == fusion_agent_mcp.__version__, (installed, fusion_agent_mcp.__version__)"
"$PYTHON" -c "from fusion_agent_mcp.server import list_tool_definitions,tool_specs; registry=len(tool_specs()); normal_tools=list_tool_definitions('normal'); normal=len(normal_tools); all_tools=len(list_tool_definitions('all')); print(f'fusion_agent MCP tools: registry={registry}, normal={normal}, all={all_tools}'); assert (registry,normal,all_tools)==(35,12,35),(registry,normal,all_tools); assert all('script' not in tool.inputSchema.get('properties', {}) for tool in normal_tools)"
