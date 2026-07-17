#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PLUGIN_ROOT/.venv/bin/python"
TOOL_PROFILE="${FUSION_AGENT_TOOL_PROFILE:-normal}"
BACKEND="${FUSION_AGENT_BACKEND:-autodesk_http}"
FUSION_DATA_URL="${FUSION_DATA_MCP_URL:-}"
WHEELS_ROOT="$PLUGIN_ROOT/wheels"
REQUIREMENTS_ROOT="$PLUGIN_ROOT/harness/requirements"
RUNTIME_LOCK="$REQUIREMENTS_ROOT/runtime.lock"
FAUST_LOCK="$REQUIREMENTS_ROOT/faust.lock"
BUILD_LOCK="$REQUIREMENTS_ROOT/build.lock"
WHEELS=()
for candidate in "$WHEELS_ROOT"/fusion_agent_harness-*.whl; do
  [[ -f "$candidate" ]] || continue
  WHEELS+=("$candidate")
done
DEVELOPMENT_SOURCE_ROOT="${FUSION_AGENT_HARNESS_ROOT:-}"
if [[ "${#WHEELS[@]}" -eq 1 && -n "$DEVELOPMENT_SOURCE_ROOT" ]]; then
  echo "FUSION_AGENT_HARNESS_ROOT is forbidden when the release wheel is bundled. Clear the development override." >&2
  exit 1
fi
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
    if [[ "${FUSION_AGENT_PYTHON}" == "$PLUGIN_ROOT/.venv/"* ]]; then
      echo "FUSION_AGENT_PYTHON must not point into the pre-existing plugin .venv used for preinstall verification." >&2
      exit 1
    fi
    VERIFY_PYTHON="${FUSION_AGENT_PYTHON}"
  else
    while IFS= read -r candidate; do
      [[ -n "$candidate" ]] || continue
      if [[ "$candidate" != /* ]]; then
        candidate="$PWD/$candidate"
      fi
      [[ "$candidate" == "$PLUGIN_ROOT/.venv/"* ]] && continue
      VERIFY_PYTHON="$candidate"
      break
    done < <(type -a -p python3 2>/dev/null; type -a -p python 2>/dev/null)
  fi
  if [[ -z "$VERIFY_PYTHON" ]]; then
    echo "A pre-existing Python 3.11+ interpreter is required to verify the plugin bundle before installation." >&2
    exit 1
  fi
  "$VERIFY_PYTHON" -I -S -B "$PLUGIN_ROOT/scripts/preinstall_verify.py" --plugin-root "$PLUGIN_ROOT" --wheel "$WHEEL"
fi

VENV_ROOT="$PLUGIN_ROOT/.venv"
if [[ -L "$VENV_ROOT" ]]; then
  echo "Refusing to replace a virtual environment that is a symbolic link." >&2
  exit 1
fi
if [[ -e "$VENV_ROOT" ]]; then
  RESOLVED_PLUGIN_ROOT="$(cd "$PLUGIN_ROOT" && pwd -P)"
  RESOLVED_VENV_ROOT="$(cd "$VENV_ROOT" && pwd -P)"
  if [[ "$(dirname "$RESOLVED_VENV_ROOT")" != "$RESOLVED_PLUGIN_ROOT" || "$(basename "$RESOLVED_VENV_ROOT")" != ".venv" ]]; then
    echo "Refusing to replace a virtual environment outside the exact plugin root." >&2
    exit 1
  fi
  rm -rf -- "$RESOLVED_VENV_ROOT"
fi
if [[ -n "${FUSION_AGENT_PYTHON:-}" && -x "${FUSION_AGENT_PYTHON}" ]]; then
  "${FUSION_AGENT_PYTHON}" -I -B -m venv "$VENV_ROOT"
elif command -v python3 >/dev/null 2>&1; then
  python3 -I -B -m venv "$VENV_ROOT"
elif command -v python >/dev/null 2>&1; then
  python -I -B -m venv "$VENV_ROOT"
else
  echo "Could not find python3 or python. Install Python 3.11+ and retry." >&2
  exit 1
fi

ISOLATED_PIP="$PLUGIN_ROOT/scripts/run-isolated-pip.py"
WHEELHOUSE="$VENV_ROOT/.fusion-agent-wheelhouse"
SELECTED_LOCK="$RUNTIME_LOCK"
INSTALLED_DEPENDENCY_LOCK="runtime.lock"
if [[ "$BACKEND" == "faust_stdio" ]]; then
  SELECTED_LOCK="$FAUST_LOCK"
  INSTALLED_DEPENDENCY_LOCK="faust.lock"
fi
mkdir -p "$WHEELHOUSE"
"$PYTHON" -I -S -B "$ISOLATED_PIP" download --require-hashes --only-binary=:all: --dest "$WHEELHOUSE" -r "$SELECTED_LOCK"
"$PYTHON" -I -S -B "$ISOLATED_PIP" install --no-compile --no-index --find-links "$WHEELHOUSE" --require-hashes --only-binary=:all: -r "$SELECTED_LOCK"
if [[ -n "$WHEEL" ]]; then
  "$PYTHON" -I -S -B "$ISOLATED_PIP" install --no-compile --force-reinstall --no-deps "$WHEEL"
elif [[ -n "$DEVELOPMENT_SOURCE_ROOT" ]]; then
  "$PYTHON" -I -S -B "$ISOLATED_PIP" install --no-compile --require-hashes --only-binary=:all: -r "$BUILD_LOCK"
  "$PYTHON" -I -S -B "$ISOLATED_PIP" install --no-compile --no-deps --no-build-isolation -e "$DEVELOPMENT_SOURCE_ROOT"
else
  echo "Missing bundled fusion_agent_harness wheel under $WHEELS_ROOT. Build the plugin with scripts/build-distribution.py." >&2
  exit 1
fi
if [[ -n "$WHEEL" ]]; then
  "$PYTHON" -I -S -B "$PLUGIN_ROOT/scripts/preinstall_verify.py" --plugin-root "$PLUGIN_ROOT" --wheel "$WHEEL" --verify-installed --dependency-lock "$INSTALLED_DEPENDENCY_LOCK" --dependency-wheelhouse "$WHEELHOUSE"
  "$PYTHON" -I -S -B "$ISOLATED_PIP" check
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
"$PYTHON" -I -B "${CONFIG_ARGS[@]}"
"$PYTHON" -I -B "$PLUGIN_ROOT/scripts/fusion_agent_codex_mcp_launcher.py" --check
export FUSION_AGENT_EXPECTED_TOOL_PROFILE="$TOOL_PROFILE"
export FUSION_AGENT_EXPECTED_BACKEND="$BACKEND"
"$PYTHON" -I -B "$PLUGIN_ROOT/scripts/validate_plugin.py"
"$PYTHON" -I -B -c "import fusion_agent_mcp; from importlib.metadata import version; installed=version('fusion-agent-harness'); print(f'fusion-agent-harness: {installed}'); assert installed == fusion_agent_mcp.__version__, (installed, fusion_agent_mcp.__version__)"
"$PYTHON" -I -B -c "from fusion_agent_mcp.server import list_tool_definitions,tool_specs; registry=len(tool_specs()); normal_tools=list_tool_definitions('normal'); normal=len(normal_tools); all_tools=len(list_tool_definitions('all')); print(f'fusion_agent MCP tools: registry={registry}, normal={normal}, all={all_tools}'); assert (registry,normal,all_tools)==(35,12,35),(registry,normal,all_tools); assert all('script' not in tool.inputSchema.get('properties', {}) for tool in normal_tools)"
