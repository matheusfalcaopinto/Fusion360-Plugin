#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${FUSION_AGENT_PYTHON:-}"
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
"$PYTHON" "$PLUGIN_ROOT/scripts/configure_mcp.py" --plugin-root "$PLUGIN_ROOT" --python "$PYTHON"
"$PYTHON" "$PLUGIN_ROOT/scripts/fusion_agent_codex_mcp_launcher.py" --check
"$PYTHON" "$PLUGIN_ROOT/scripts/validate_plugin.py"
"$PYTHON" -c "import fusion_agent_mcp; from importlib.metadata import version; installed=version('fusion-agent-harness'); print(f'fusion-agent-harness: {installed}'); assert installed == fusion_agent_mcp.__version__ == '0.2.1', (installed, fusion_agent_mcp.__version__)"
"$PYTHON" -c "from fusion_agent_mcp.server import tool_specs; count=len(tool_specs()); print(f'fusion_agent MCP tools: {count}'); assert count == 35, count"
