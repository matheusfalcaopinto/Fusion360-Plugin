from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_rebuilt_wheel_is_deterministic_and_imports_from_clean_sha_path(tmp_path: Path) -> None:
    version = tomllib.loads((ROOT / "harness" / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    build = [sys.executable, str(ROOT / "scripts" / "build-distribution.py")]
    first = subprocess.run(build, cwd=ROOT, text=True, capture_output=True, check=True)
    wheel = next((ROOT / "wheels").glob(f"fusion_agent_harness-{version}-*.whl"))
    first_bytes = wheel.read_bytes()
    first_sha = hashlib.sha256(first_bytes).hexdigest()

    second = subprocess.run(build, cwd=ROOT, text=True, capture_output=True, check=True)
    second_bytes = wheel.read_bytes()
    second_sha = hashlib.sha256(second_bytes).hexdigest()

    assert first_sha == second_sha
    assert "sha256=" in first.stdout
    assert "sha256=" in second.stdout

    extraction = tmp_path / second_sha
    extraction.mkdir()
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(extraction)

    code = """
import importlib.metadata
import json
import sys
sys.path.insert(0, sys.argv[1])
from fusion_agent_mcp.server import list_tool_definitions
print(json.dumps({
    'version': importlib.metadata.version('fusion-agent-harness'),
    'tool_count': len(list_tool_definitions()),
    'module': sys.modules['fusion_agent_mcp.server'].__file__,
}))
"""
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    imported = subprocess.run(
        [sys.executable, "-c", code, str(extraction)],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )

    imported_payload = json.loads(imported.stdout)
    assert imported_payload["version"] == version
    assert imported_payload["tool_count"] == 35
    assert Path(imported_payload["module"]).is_relative_to(extraction)
