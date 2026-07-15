#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${FUSION_AGENT_PYTHON:-}"
TOOL_PROFILE="${FUSION_AGENT_TOOL_PROFILE:-normal}"
BACKEND="${FUSION_AGENT_BACKEND:-autodesk_http}"
FUSION_DATA_URL="${FUSION_DATA_MCP_URL:-}"
WHEELS_ROOT="$PLUGIN_ROOT/wheels"
mapfile -t WHEELS < <(find "$WHEELS_ROOT" -maxdepth 1 -type f -name 'fusion_agent_harness-*.whl' 2>/dev/null | sort)
if [[ "${#WHEELS[@]}" -gt 1 ]]; then
  echo "Expected exactly one bundled harness wheel, found ${#WHEELS[@]}." >&2
  exit 1
fi
WHEEL="${WHEELS[0]:-}"

if [[ -z "$PYTHON" ]]; then
  if [[ -x "$PLUGIN_ROOT/.venv/bin/python" ]]; then
    PYTHON="$PLUGIN_ROOT/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -m venv "$PLUGIN_ROOT/.venv"
    PYTHON="$PLUGIN_ROOT/.venv/bin/python"
  elif command -v python >/dev/null 2>&1; then
    python -m venv "$PLUGIN_ROOT/.venv"
    PYTHON="$PLUGIN_ROOT/.venv/bin/python"
  else
    echo "Could not find python3 or python. Install Python 3.11+ and retry." >&2
    exit 1
  fi
fi

if [[ -n "$WHEEL" ]]; then
  "$PYTHON" -m pip install --force-reinstall "$WHEEL"
elif [[ -n "${FUSION_AGENT_HARNESS_ROOT:-}" ]]; then
  "$PYTHON" -m pip install -e "$FUSION_AGENT_HARNESS_ROOT"
else
  echo "Missing bundled fusion_agent_harness wheel under $WHEELS_ROOT. Build the plugin with scripts/build-distribution.py." >&2
  exit 1
fi
if [[ "$BACKEND" == "faust_stdio" ]]; then
  "$PYTHON" -m pip install "fusion360-mcp-server==0.1.0"
fi
CONFIG_ARGS=(
  "$PLUGIN_ROOT/scripts/configure_mcp.py"
  --plugin-root "$PLUGIN_ROOT"
  --python "$PYTHON"
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
