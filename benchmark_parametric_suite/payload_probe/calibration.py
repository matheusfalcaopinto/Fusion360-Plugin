"""Generate exact protected byte sizes by changing inert padding only."""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from typing import Callable

from .models import CanaryContract


ScriptProtector = Callable[[str], str]


@dataclass(frozen=True)
class CalibratedScript:
    raw_script: str
    protected_script: str
    padding_bytes: int
    original_payload_bytes: int
    protected_payload_bytes: int
    original_payload_sha256: str
    protected_payload_sha256: str
    ast_topology_sha256: str
    padding_invariant_sha256: str


class PayloadCalibrationError(ValueError):
    pass


class PayloadScriptCalibrator:
    """Calibrate scripts against the exact transformation used on the wire."""

    def __init__(self, protector: ScriptProtector) -> None:
        self._protector = protector

    def calibrate(
        self, *, target_protected_bytes: int, canaries: CanaryContract
    ) -> CalibratedScript:
        if type(target_protected_bytes) is not int or target_protected_bytes <= 0:
            raise PayloadCalibrationError(
                "target_protected_bytes must be a positive integer"
            )
        padding_bytes = 0
        raw = render_probe_script(canaries=canaries, padding_bytes=padding_bytes)
        invariant = padding_invariant_sha256(raw)
        protected = self._protect(raw)
        for _ in range(8):
            difference = target_protected_bytes - len(protected.encode("utf-8"))
            if difference == 0:
                break
            padding_bytes += difference
            if padding_bytes < 0:
                raise PayloadCalibrationError(
                    f"target {target_protected_bytes} B is smaller than the protected template"
                )
            raw = render_probe_script(canaries=canaries, padding_bytes=padding_bytes)
            if padding_invariant_sha256(raw) != invariant:
                raise PayloadCalibrationError(
                    "calibration changed executable AST outside inert padding"
                )
            protected = self._protect(raw)
        actual = len(protected.encode("utf-8"))
        if actual != target_protected_bytes:
            raise PayloadCalibrationError(
                f"protector could not produce exact target: expected {target_protected_bytes} B, got {actual} B"
            )
        raw_bytes = raw.encode("utf-8")
        protected_bytes = protected.encode("utf-8")
        return CalibratedScript(
            raw_script=raw,
            protected_script=protected,
            padding_bytes=padding_bytes,
            original_payload_bytes=len(raw_bytes),
            protected_payload_bytes=len(protected_bytes),
            original_payload_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            protected_payload_sha256=hashlib.sha256(protected_bytes).hexdigest(),
            ast_topology_sha256=ast_topology_sha256(protected),
            padding_invariant_sha256=invariant,
        )

    def _protect(self, script: str) -> str:
        protected = self._protector(script)
        if not isinstance(protected, str) or not protected:
            raise PayloadCalibrationError(
                "script protector must return a non-empty string"
            )
        try:
            ast.parse(protected)
        except SyntaxError as exc:
            raise PayloadCalibrationError(
                "script protector returned invalid Python"
            ) from exc
        return protected


def render_probe_script(*, canaries: CanaryContract, padding_bytes: int) -> str:
    """Render a constant-shape script with start/end canaries as attributes.

    The only value adjusted during calibration is the ASCII value assigned to
    ``_payload_probe_padding``.  The assignment is deliberately between the
    start canary and the mutation/end canaries.
    """

    if type(padding_bytes) is not int or padding_bytes < 0:
        raise PayloadCalibrationError("padding_bytes must be a non-negative integer")

    def literal(value: str) -> str:
        return json.dumps(value, ensure_ascii=True)

    padding = "x" * padding_bytes
    lines = [
        "import adsk.core",
        "import adsk.fusion",
        "import json",
        "",
        "def run(_context: str):",
        "    _app = adsk.core.Application.get()",
        "    _document = _app.activeDocument",
        "    _design = adsk.fusion.Design.cast(_app.activeProduct)",
        "    if _document is None or _design is None:",
        "        raise RuntimeError('PAYLOAD_PROBE_NO_ACTIVE_DESIGN')",
        "    _attributes = _design.rootComponent.attributes",
        f"    _group = {literal(canaries.group)}",
        "    _fixture = _attributes.itemByName(_group, 'fixture_marker')",
        f"    if _fixture is None or _fixture.value != {literal(canaries.fixture_marker)}:",
        "        raise RuntimeError('PAYLOAD_PROBE_FIXTURE_DRIFT')",
        "    _existing_trial = _attributes.itemByName(_group, 'trial_id')",
        "    if _existing_trial is not None:",
        "        raise RuntimeError('PAYLOAD_PROBE_CONTAMINATED_TRIAL')",
        f"    _attributes.add(_group, 'trial_id', {literal(canaries.trial_id)})",
        f"    _attributes.add(_group, 'start', {literal(canaries.start_value)})",
        f"    _payload_probe_padding = {literal(padding)}",
        f"    _attributes.add(_group, 'mutation', {literal(canaries.mutation_value)})",
        f"    _attributes.add(_group, 'end', {literal(canaries.end_value)})",
        "    _result = {",
        "        'schema_version': 'fusion_executor_payload_probe.result.v1',",
        f"        'trial_id': {literal(canaries.trial_id)},",
        f"        'start': {literal(canaries.start_value)},",
        f"        'mutation': {literal(canaries.mutation_value)},",
        f"        'end': {literal(canaries.end_value)},",
        "    }",
        "    print(json.dumps(_result, sort_keys=True, separators=(',', ':')))",
        "    return _result",
        "",
    ]
    return "\n".join(lines)


def ast_topology_sha256(script: str) -> str:
    """Hash AST topology while intentionally ignoring all literal values."""

    tree = ast.parse(script)

    class LiteralEraser(ast.NodeTransformer):
        def visit_Constant(self, node: ast.Constant) -> ast.AST:  # noqa: N802 - AST API
            kind = type(node.value).__name__
            return ast.copy_location(ast.Constant(value=f"<{kind}>"), node)

    normalized = LiteralEraser().visit(tree)
    ast.fix_missing_locations(normalized)
    value = ast.dump(normalized, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def padding_invariant_sha256(script: str) -> str:
    """Hash the complete AST after erasing only the calibration padding."""

    tree = ast.parse(script)
    replacements = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "_payload_probe_padding":
            if not isinstance(node.value, ast.Constant) or not isinstance(
                node.value.value, str
            ):
                raise PayloadCalibrationError(
                    "padding assignment is not a string literal"
                )
            node.value = ast.copy_location(
                ast.Constant(value="<INERT_PADDING>"), node.value
            )
            replacements += 1
    if replacements != 1:
        raise PayloadCalibrationError(
            "probe script must contain exactly one inert padding assignment"
        )
    value = ast.dump(tree, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
