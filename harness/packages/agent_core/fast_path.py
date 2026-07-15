"""Guarded native fast path for Fusion MCP read, script, and recovery calls."""

from __future__ import annotations

import ast
import base64
import copy
import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from agent_core.targeted_inspection import build_targeted_inspection_script, validate_inspection_payload
from fusion_mcp_adapter.errors import ErrorCode
from fusion_mcp_adapter.execute_guard import normalize_execute_script, protected_script_descriptor


NativeCall = Callable[..., Awaitable[Any]]
CHANGE_CLASSES = {"read_only", "additive", "scoped_update"}
ASSERTION_OPERATORS = {
    "eq",
    "ne",
    "approx",
    "gte",
    "lte",
    "contains",
    "unchanged",
    "increased_by",
    "decreased_by",
}
ALLOWED_IMPORTS = {"adsk", "json", "math"}
BLOCKED_IMPORTS = {
    "asyncio",
    "builtins",
    "ctypes",
    "ftplib",
    "http",
    "importlib",
    "multiprocessing",
    "os",
    "pathlib",
    "pickle",
    "shutil",
    "socket",
    "subprocess",
    "sys",
    "tempfile",
    "urllib",
}
BLOCKED_NAMES = {
    "open",
    "exec",
    "eval",
    "compile",
    "__import__",
    "getattr",
    "setattr",
    "delattr",
    "hasattr",
    "vars",
    "dir",
    "globals",
    "locals",
}
BLOCKED_ATTRIBUTES = {
    "addbycopy",
    "addbyinsert",
    "addexistingcomponent",
    "addnewcomponentcopy",
    "close",
    "componentize",
    "createcomponentfrombodies",
    "deleteme",
    "executeTextCommand".lower(),
    "exportmanager",
    "exporttoarchive",
    "importmanager",
    "hide",
    "moveToComponent".lower(),
    "save",
    "saveas",
    "show",
}
BLOCKED_ASSIGN_ATTRIBUTES = {
    "islightbulbon",
    "isvisible",
    "transform",
    "transform2",
}
RESERVED_BINDING_NAMES = {
    "targets",
    "target_components",
    "_fusion_agent_user_run",
    "_fusion_agent_runtime_sys",
    "_fusion_agent_is_ns_writer",
    "_fusion_agent_collapse_stream",
}
_ROOT_COMPONENT_BINDING = "__fusion_agent_root_component__"
ADDITIVE_METHODS = {"project", "project2", "offset", "include", "intersectwithsketchplane"}
PURE_METHODS = {
    "append",
    "cast",
    "classtype",
    "copy",
    "count",
    "create",
    "createbyobject",
    "createbyreal",
    "createbystring",
    "createinput",
    "dumps",
    "endswith",
    "findentitybytoken",
    "format",
    "get",
    "item",
    "itembyid",
    "itembyname",
    "items",
    "join",
    "keys",
    "loads",
    "lower",
    "modeltosketchspace",
    "replace",
    "sketchtomodelspace",
    "startswith",
    "strip",
    "upper",
    "values",
}
PURE_FUNCTIONS = {
    "RuntimeError",
    "TypeError",
    "ValueError",
    "all",
    "any",
    "bool",
    "dict",
    "enumerate",
    "float",
    "int",
    "isinstance",
    "len",
    "list",
    "max",
    "min",
    "next",
    "print",
    "range",
    "round",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
}

ACTIVE_COMMAND_SCRIPT = r'''
import adsk.core
import json


def run(_context: str):
    app = adsk.core.Application.get()
    command_id = ""
    try:
        command_id = str(app.activeCommand or "")
    except BaseException:
        try:
            command_id = str(app.userInterface.activeCommand or "")
        except BaseException:
            command_id = ""
    default_ids = {"", "SelectCommand", "FusionSelectCommand"}
    active = None if command_id in default_ids else {
        "id": command_id,
        "isDefaultCommand": False,
    }
    print(json.dumps({
        "success": True,
        "probe": "fusion_agent_active_command",
        "activeCommand": active,
    }))
'''


@dataclass
class FastPathResponse:
    """Structured payload plus optional MCP content blocks."""

    payload: dict[str, Any]
    content: list[dict[str, Any]] = field(default_factory=list)
    is_error: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScriptPolicyDecision:
    """Result of static script analysis."""

    allowed: bool
    change_class: str
    script_sha256: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mutating_syntax_detected: bool = False
    detected_change_class: str = "read_only"

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "change_class": self.change_class,
            "script_sha256": self.script_sha256,
            "errors": self.errors,
            "warnings": self.warnings,
            "mutating_syntax_detected": self.mutating_syntax_detected,
            "detected_change_class": self.detected_change_class,
        }


@dataclass
class _HelperSummary:
    parameter_roles: dict[int, str] = field(default_factory=dict)
    returns_new: bool = False


def _summary_origin(node: ast.AST | None, origins: dict[str, str], helpers: dict[str, _HelperSummary]) -> str:
    if node is None:
        return "unknown"
    if isinstance(node, ast.Name):
        return origins.get(node.id, "unknown")
    if isinstance(node, (ast.Attribute, ast.Subscript)):
        return _summary_origin(node.value, origins, helpers)
    if isinstance(node, ast.Call):
        name = _call_name(node.func)
        leaf = name.lower().rsplit(".", 1)[-1]
        if isinstance(node.func, ast.Name) and node.func.id in helpers and helpers[node.func.id].returns_new:
            return "new"
        if leaf.startswith(("add", "create")) or leaf in ADDITIVE_METHODS:
            return "new"
        if isinstance(node.func, ast.Attribute):
            return _summary_origin(node.func.value, origins, helpers)
    return "unknown"


def _summarize_helper(node: ast.FunctionDef, helpers: dict[str, _HelperSummary]) -> _HelperSummary:
    origins = {argument.arg: f"param:{index}" for index, argument in enumerate(node.args.args)}
    assignments = [child for child in ast.walk(node) if isinstance(child, (ast.Assign, ast.AnnAssign))]
    for _ in range(max(1, len(assignments) + 1)):
        changed = False
        for assignment in assignments:
            targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            value = assignment.value
            origin = _summary_origin(value, origins, helpers)
            for target in targets:
                if isinstance(target, ast.Name) and origins.get(target.id) != origin:
                    origins[target.id] = origin
                    changed = True
        if not changed:
            break

    roles: dict[int, str] = {}

    def require(origin: str, role: str) -> None:
        if not origin.startswith("param:"):
            return
        index = int(origin.split(":", 1)[1])
        previous = roles.get(index)
        roles[index] = role if previous in (None, role) else "conflict"

    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _call_name(child.func)
            leaf = name.lower().rsplit(".", 1)[-1]
            if isinstance(child.func, ast.Name) and child.func.id in helpers:
                called = helpers[child.func.id]
                for index, role in called.parameter_roles.items():
                    if index < len(child.args):
                        require(_summary_origin(child.args[index], origins, helpers), role)
            elif isinstance(child.func, ast.Attribute):
                receiver = _summary_origin(child.func.value, origins, helpers)
                if leaf.startswith("add") or leaf in ADDITIVE_METHODS:
                    require(receiver, "component")
                elif leaf.startswith("set"):
                    require(receiver, "target")
        elif isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            for target in targets:
                if isinstance(target, ast.Attribute):
                    origin = _summary_origin(target.value, origins, helpers)
                    if origin != "new":
                        require(origin, "target")

    return_nodes = [child for child in ast.walk(node) if isinstance(child, ast.Return)]
    returns_new = False
    if len(return_nodes) == 1:
        returned = return_nodes[0].value
        if isinstance(returned, ast.Name):
            writes = [
                assignment
                for assignment in assignments
                for target in (assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target])
                if isinstance(target, ast.Name) and target.id == returned.id
            ]
            returns_new = len(writes) == 1 and _summary_origin(
                writes[0].value,
                origins,
                helpers,
            ) == "new"
        else:
            returns_new = _summary_origin(returned, origins, helpers) == "new"
    return _HelperSummary(parameter_roles=roles, returns_new=returns_new)


def _summarize_helpers(tree: ast.Module) -> dict[str, _HelperSummary]:
    definitions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name != "run"
    }
    summaries = {name: _HelperSummary() for name in definitions}
    for _ in range(max(1, len(definitions) + 1)):
        updated = {name: _summarize_helper(node, summaries) for name, node in definitions.items()}
        if updated == summaries:
            break
        summaries = updated
    return summaries


class _ScriptPolicyVisitor(ast.NodeVisitor):
    def __init__(
        self,
        change_class: str,
        *,
        helper_summaries: dict[str, _HelperSummary],
        allowed_target_ids: set[str] | None,
        allowed_component_paths: set[str] | None,
    ) -> None:
        self.change_class = change_class
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.mutating = False
        self.additive_detected = False
        self.scoped_update_detected = False
        self.helper_summaries = helper_summaries
        self.allowed_target_ids = allowed_target_ids
        self.allowed_component_paths = allowed_component_paths
        self._provenance_scopes: list[dict[str, str]] = [{}]
        self._reported_binding_errors: set[tuple[int, str]] = set()

    def error(self, node: ast.AST, message: str) -> None:
        self.errors.append(f"line {getattr(node, 'lineno', '?')}: {message}")

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            root = alias.name.split(".", 1)[0]
            if root not in ALLOWED_IMPORTS:
                self.error(node, f"import is not allowlisted: {alias.name}")
            if (alias.asname or root) in RESERVED_BINDING_NAMES:
                self.error(node, "reserved binding names may not be shadowed")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        module = node.module or ""
        root = module.split(".", 1)[0]
        if root not in ALLOWED_IMPORTS:
            self.error(node, f"import is not allowlisted: {module or '<relative>'}")
        for alias in node.names:
            if (alias.asname or alias.name) in RESERVED_BINDING_NAMES:
                self.error(node, "reserved binding names may not be shadowed")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if node.id in BLOCKED_NAMES or node.id.startswith("__"):
            self.error(node, f"blocked name: {node.id}")
        if node.id in RESERVED_BINDING_NAMES and isinstance(node.ctx, (ast.Store, ast.Del)):
            self.error(node, f"reserved binding may not be shadowed: {node.id}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        attribute = node.attr.lower()
        if attribute.startswith("__"):
            self.error(node, f"dunder access is blocked: {node.attr}")
        if attribute in {value.lower() for value in BLOCKED_ATTRIBUTES} or attribute == "execute":
            self.error(node, f"blocked Fusion operation: {node.attr}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = _call_name(node.func)
        lowered = name.lower()
        leaf = lowered.rsplit(".", 1)[-1]
        blocked = False
        if leaf in {value.lower() for value in BLOCKED_NAMES | BLOCKED_ATTRIBUTES} or leaf == "execute":
            self.error(node, f"blocked call: {name}")
            blocked = True
        if leaf.startswith(("save", "close", "import", "export", "delete", "remove", "move", "componentize")):
            self.error(node, f"operation is Safe Harness only: {name}")
            blocked = True
        if "transform" in leaf or "visibility" in leaf or leaf in {"hide", "show"}:
            self.error(node, f"transform or visibility operation is Safe Harness only: {name}")
            blocked = True
        if any(part.startswith("__") for part in lowered.split(".")):
            self.error(node, f"dunder call is blocked: {name}")
            blocked = True

        if isinstance(node.func, ast.Name):
            if node.func.id in self.helper_summaries:
                self._check_helper_call(node, self.helper_summaries[node.func.id])
            elif node.func.id not in PURE_FUNCTIONS:
                self.error(node, f"unclassified callable is blocked: {node.func.id}")
        elif not blocked and isinstance(node.func, ast.Attribute):
            receiver = self._expr_provenance(node.func.value)
            if leaf.startswith("add") or leaf in ADDITIVE_METHODS:
                self.mutating = True
                self.additive_detected = True
                if self.change_class != "additive":
                    self.error(node, f"risk analysis requires change_class=additive for call: {name}")
                if receiver not in {"component", "new"}:
                    self.error(node, f"additive mutation receiver is not bound to target_components or a new entity: {name}")
            elif leaf.startswith("set"):
                self.mutating = True
                if receiver == "new":
                    self.additive_detected = True
                    if self.change_class != "additive":
                        self.error(node, f"risk analysis requires change_class=additive for call on new entity: {name}")
                elif receiver == "target":
                    self.scoped_update_detected = True
                    if self.change_class != "scoped_update":
                        self.error(node, f"risk analysis requires change_class=scoped_update for call: {name}")
                else:
                    self.scoped_update_detected = True
                    self.error(node, f"scoped mutation receiver is not bound to targets or a new entity: {name}")
            elif not (leaf.startswith("create") or leaf in PURE_METHODS or _root_name(node.func) in {"json", "math"}):
                self.error(node, f"unclassified Fusion call is blocked: {name}")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        self._track_provenance(node.targets, node.value)
        for target in node.targets:
            self._check_assignment(target, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        self._track_provenance([node.target], node.value)
        self._check_assignment(node.target, node)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:  # noqa: N802
        self._check_assignment(node.target, node)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:  # noqa: N802
        for target in node.targets:
            self._check_assignment(target, node)
        self.error(node, "delete statements are Safe Harness only")
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:  # noqa: N802
        self._check_assignment(node.target, node)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:  # noqa: N802
        self._check_assignment(node.target, node)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:  # noqa: N802
        for item in node.items:
            if item.optional_vars is not None:
                self._check_assignment(item.optional_vars, node)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:  # noqa: N802
        for item in node.items:
            if item.optional_vars is not None:
                self._check_assignment(item.optional_vars, node)
        self.generic_visit(node)

    def visit_Yield(self, node: ast.Yield) -> None:  # noqa: N802
        self.error(node, "generator functions are blocked; run and helpers must execute synchronously")
        self.generic_visit(node)

    def visit_YieldFrom(self, node: ast.YieldFrom) -> None:  # noqa: N802
        self.error(node, "generator functions are blocked; run and helpers must execute synchronously")
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:  # noqa: N802
        self.error(node, "global declarations are blocked")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:  # noqa: N802
        self.error(node, "nonlocal declarations are blocked")

    def _check_assignment(self, target: ast.AST, node: ast.AST) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for child in target.elts:
                self._check_assignment(child, node)
            return
        if isinstance(target, ast.Name) and target.id in RESERVED_BINDING_NAMES:
            self.error(node, f"reserved binding may not be overwritten: {target.id}")
        if isinstance(target, ast.Subscript) and _root_name(target) in RESERVED_BINDING_NAMES:
            self.error(node, f"reserved binding entries may not be overwritten: {_root_name(target)}")
        if isinstance(target, ast.Attribute):
            self.mutating = True
            if target.attr.lower() in BLOCKED_ASSIGN_ATTRIBUTES:
                self.error(node, f"blocked attribute mutation: {target.attr}")
                return
            receiver = self._expr_provenance(target.value)
            if receiver == "new":
                self.additive_detected = True
                if self.change_class != "additive":
                    self.error(node, f"risk analysis requires change_class=additive for attribute on new entity: {target.attr}")
            elif receiver == "target":
                self.scoped_update_detected = True
                if self.change_class != "scoped_update":
                    self.error(node, f"risk analysis requires change_class=scoped_update for attribute: {target.attr}")
            else:
                self.scoped_update_detected = True
                self.error(node, f"attribute mutation receiver is not bound to targets or a new entity: {target.attr}")

    def _track_provenance(self, targets: list[ast.expr], value: ast.AST | None) -> None:
        provenance = self._expr_provenance(value)
        for target in targets:
            if isinstance(target, ast.Name):
                self._provenance_scopes[-1][target.id] = provenance

    def _expr_provenance(self, node: ast.AST | None) -> str:
        if node is None:
            return "unknown"
        if isinstance(node, ast.Name):
            return self._provenance_scopes[-1].get(node.id, "unknown")
        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name) and node.value.id in RESERVED_BINDING_NAMES:
                key = node.slice.value if isinstance(node.slice, ast.Constant) else None
                if not isinstance(key, str) or not key:
                    self._binding_error(node, f"{node.value.id} requires a non-empty literal string key")
                    return "unknown"
                allowed = self.allowed_target_ids if node.value.id == "targets" else self.allowed_component_paths
                if allowed is not None and key not in allowed:
                    self._binding_error(node, f"undeclared {node.value.id} binding: {key}")
                    return "unknown"
                return "target" if node.value.id == "targets" else "component"
            base = self._expr_provenance(node.value)
            return base if base in {"component", "new"} else "unknown"
        if isinstance(node, ast.Attribute):
            base = self._expr_provenance(node.value)
            return base if base in {"component", "new"} else "unknown"
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in self.helper_summaries:
                return "new" if self.helper_summaries[node.func.id].returns_new else "unknown"
            leaf = _call_name(node.func).lower().rsplit(".", 1)[-1]
            if leaf.startswith(("add", "create")) or leaf in ADDITIVE_METHODS:
                return "new"
            if isinstance(node.func, ast.Attribute) and leaf in PURE_METHODS:
                receiver = self._expr_provenance(node.func.value)
                return receiver if receiver in {"component", "new"} else "unknown"
        return "unknown"

    def _binding_error(self, node: ast.AST, message: str) -> None:
        marker = (getattr(node, "lineno", -1), message)
        if marker not in self._reported_binding_errors:
            self._reported_binding_errors.add(marker)
            self.error(node, message)

    def _check_helper_call(self, node: ast.Call, summary: _HelperSummary) -> None:
        for index, role in summary.parameter_roles.items():
            if role == "conflict":
                self.error(node, "helper parameter mixes target and target-component mutations")
                continue
            if index >= len(node.args):
                self.error(node, "helper mutation binding must be supplied positionally")
                continue
            actual = self._expr_provenance(node.args[index])
            if actual != role:
                binding_name = "targets" if role == "target" else "target_components"
                self.error(node, f"helper mutation parameter {index} must derive from {binding_name}")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        if node.name in RESERVED_BINDING_NAMES:
            self.error(node, f"reserved internal function name is blocked: {node.name}")
        if any(argument.arg in RESERVED_BINDING_NAMES for argument in node.args.args):
            self.error(node, "reserved binding names may not be function parameters")
        scope: dict[str, str] = {}
        summary = self.helper_summaries.get(node.name)
        if summary:
            for index, role in summary.parameter_roles.items():
                if index < len(node.args.args) and role in {"target", "component"}:
                    scope[node.args.args[index].arg] = role
        self._provenance_scopes.append(scope)
        try:
            self.generic_visit(node)
        finally:
            self._provenance_scopes.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.error(node, "async functions are blocked")
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        if node.type is None:
            self.error(node, "bare except is blocked")
        elif _contains_broad_exception(node.type):
            self.error(node, "broad exception handler is blocked")
        if node.name in RESERVED_BINDING_NAMES:
            self.error(node, f"reserved binding may not be shadowed: {node.name}")
        self.generic_visit(node)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _root_name(node: ast.AST) -> str:
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _contains_broad_exception(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in {"Exception", "BaseException"}
    if isinstance(node, ast.Tuple):
        return any(_contains_broad_exception(item) for item in node.elts)
    return False


def _safe_module_statement(node: ast.stmt) -> bool:
    if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef)):
        return True
    if isinstance(node, ast.Assign):
        try:
            ast.literal_eval(node.value)
            return True
        except (ValueError, TypeError):
            return False
    if isinstance(node, ast.AnnAssign):
        return node.value is None or isinstance(node.value, ast.Constant)
    return False


def lint_fusion_script(
    script: str,
    change_class: str,
    *,
    allowed_target_ids: set[str] | None = None,
    allowed_component_paths: set[str] | None = None,
) -> ScriptPolicyDecision:
    """Fail closed on unsafe or mismatched Python before Fusion receives it."""

    script_bytes = script.encode("utf-8")
    digest = hashlib.sha256(script_bytes).hexdigest()
    errors: list[str] = []
    if change_class not in CHANGE_CLASSES:
        errors.append(f"unsupported change_class: {change_class}")
    if len(script_bytes) > 64 * 1024:
        errors.append("script exceeds the 64 KiB limit")
    try:
        tree = ast.parse(script)
    except SyntaxError as exc:
        errors.append(f"syntax error at line {exc.lineno}: {exc.msg}")
        return ScriptPolicyDecision(False, change_class, digest, errors)

    for statement in tree.body:
        if not _safe_module_statement(statement):
            errors.append(
                f"line {getattr(statement, 'lineno', '?')}: executable module-level code is blocked; put logic inside run or a helper"
            )

    run_functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"]
    if len(run_functions) != 1:
        errors.append("script must define exactly one top-level run function")
    else:
        run_function = run_functions[0]
        args = run_function.args
        if isinstance(run_function, ast.AsyncFunctionDef):
            errors.append("run must be synchronous")
        if len(args.args) != 1 or args.args[0].arg != "_context" or args.vararg or args.kwarg or args.kwonlyargs:
            errors.append("run signature must be exactly def run(_context: str)")
        annotation = args.args[0].annotation if args.args else None
        if not isinstance(annotation, ast.Name) or annotation.id != "str":
            errors.append("run parameter must be annotated as str")

    helper_summaries = _summarize_helpers(tree)
    visitor = _ScriptPolicyVisitor(
        change_class,
        helper_summaries=helper_summaries,
        allowed_target_ids=allowed_target_ids,
        allowed_component_paths=allowed_component_paths,
    )
    visitor.visit(tree)
    manual_basis_warning = _manual_sketch_basis_warning(tree)
    if manual_basis_warning:
        visitor.warnings.append(manual_basis_warning)
    errors.extend(visitor.errors)
    if change_class == "additive" and not visitor.additive_detected:
        errors.append("risk analysis found no additive mutation for change_class=additive")
    if change_class == "scoped_update" and not visitor.scoped_update_detected:
        errors.append("risk analysis found no scoped update for change_class=scoped_update")
    detected_change_class = (
        "mixed"
        if visitor.additive_detected and visitor.scoped_update_detected
        else "additive"
        if visitor.additive_detected
        else "scoped_update"
        if visitor.scoped_update_detected
        else "read_only"
    )
    return ScriptPolicyDecision(
        allowed=not errors,
        change_class=change_class,
        script_sha256=digest,
        errors=errors,
        warnings=visitor.warnings,
        mutating_syntax_detected=visitor.mutating,
        detected_change_class=detected_change_class,
    )


def _manual_sketch_basis_warning(tree: ast.Module) -> str | None:
    """Warn when model code manually projects sketch axes into model points.

    Fusion sketch coordinate systems are not interchangeable with model-space
    axes.  A hand-written xDirection/yDirection dot-product helper is a common
    source of detached geometry; the native conversion API is deterministic.
    """

    attributes = {
        node.attr.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    }
    creates_point = any(
        isinstance(node, ast.Call)
        and _call_name(node.func).lower().endswith("point3d.create")
        for node in ast.walk(tree)
    )
    uses_native_conversion = any(
        isinstance(node, ast.Call)
        and _call_name(node.func).lower().endswith("modeltosketchspace")
        for node in ast.walk(tree)
    )
    if attributes.intersection({"xdirection", "ydirection"}) and creates_point and not uses_native_conversion:
        return (
            "manual sketch xDirection/yDirection projection into Point3D can detach geometry; "
            "prefer sketch.modelToSketchSpace"
        )
    return None


def validate_fast_execute_request(arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate the public Fast Execute request and normalize its contract."""

    intent = str(arguments.get("intent") or "").strip()
    change_class = str(arguments.get("change_class") or "").strip()
    script = arguments.get("script")
    if not intent:
        raise ValueError("intent is required")
    if change_class not in CHANGE_CLASSES:
        raise ValueError("change_class must be read_only, additive, or scoped_update")
    if not isinstance(script, str) or not script.strip():
        raise ValueError("script is required")
    api_references = arguments.get("api_references") or []
    if not isinstance(api_references, list) or not all(isinstance(value, str) for value in api_references):
        raise ValueError("api_references must be an array of strings")
    verification = arguments.get("verification") or {}
    if not isinstance(verification, dict):
        raise ValueError("verification must be an object")
    queries = verification.get("queries") or []
    assertions = verification.get("assertions") or []
    requirements = verification.get("requirements") or []
    if len(queries) > 50 or len(assertions) > 100 or not isinstance(requirements, list) or len(requirements) > 100:
        raise ValueError("verification supports at most 50 queries, 100 assertions, and 100 requirements")
    target_query_ids = arguments.get("target_query_ids") or []
    if not isinstance(target_query_ids, list) or not all(isinstance(value, str) for value in target_query_ids):
        raise ValueError("target_query_ids must be an array of strings")
    if len(target_query_ids) > 20:
        raise ValueError("target_query_ids supports at most 20 entries")
    if change_class != "read_only" and (not queries or not assertions or not target_query_ids):
        raise ValueError("mutations require verification queries, assertions, and target_query_ids")
    if change_class == "read_only" and not queries:
        queries = [
            {
                "id": "__fusion_agent_document__",
                "entity_type": "document",
                "selector": {},
                "fields": ["exists", "name", "id"],
            }
        ]
    normalized_inspection = validate_inspection_payload({"queries": queries, "limit_per_query": verification.get("limit_per_query", 20)})
    query_ids = {query["id"] for query in normalized_inspection["queries"]}
    if not set(target_query_ids).issubset(query_ids):
        raise ValueError("every target_query_id must reference a verification query")

    normalized_assertions = _normalize_assertions(assertions, query_ids)
    normalized_requirements = _normalize_requirements(requirements, normalized_assertions)
    if change_class != "read_only":
        asserted_query_ids = {assertion["query_id"] for assertion in normalized_assertions}
        if not set(target_query_ids).issubset(asserted_query_ids):
            raise ValueError("every mutation target must have at least one verification assertion")
    queries_by_id = {query["id"]: query for query in normalized_inspection["queries"]}
    target_component_paths: list[str] = []
    if change_class == "additive":
        for query_id in target_query_ids:
            component_path = str((queries_by_id[query_id].get("selector") or {}).get("component_path") or "").strip()
            if not component_path:
                raise ValueError("every additive target query requires selector.component_path")
            target_component_paths.append(component_path)
    return {
        "intent": intent,
        "change_class": change_class,
        "script": script,
        "api_references": api_references,
        "target_query_ids": list(dict.fromkeys(target_query_ids)),
        "target_component_paths": list(dict.fromkeys(target_component_paths)),
        "verification": {
            **normalized_inspection,
            "assertions": normalized_assertions,
            "requirements": normalized_requirements,
            "include_screenshot": bool(verification.get("include_screenshot", False)),
        },
    }


def _normalize_assertions(assertions: Any, query_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(assertions, list) or len(assertions) > 100:
        raise ValueError("assertions must be an array with at most 100 entries")
    normalized_assertions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(assertions):
        if not isinstance(raw, dict):
            raise ValueError(f"assertions[{index}] must be an object")
        assertion_id = str(raw.get("id") or f"assertion_{index}")
        if assertion_id in seen_ids:
            raise ValueError("assertion ids must be unique")
        seen_ids.add(assertion_id)
        query_id = str(raw.get("query_id") or "")
        field_name = str(raw.get("field") or "")
        operator = str(raw.get("operator") or "")
        if query_id not in query_ids or not field_name or operator not in ASSERTION_OPERATORS:
            raise ValueError(f"assertions[{index}] is invalid")
        if operator == "approx" and "tolerance" not in raw:
            raise ValueError(f"assertions[{index}] approx requires tolerance")
        if operator != "unchanged" and "expected" not in raw:
            raise ValueError(f"assertions[{index}] {operator} requires expected")
        requirement_ids = raw.get("requirement_ids") or []
        if not isinstance(requirement_ids, list) or not all(isinstance(value, str) and value for value in requirement_ids):
            raise ValueError(f"assertions[{index}].requirement_ids must be an array of ids")
        normalized_assertions.append(
            {
                "id": assertion_id,
                "query_id": query_id,
                "field": field_name,
                "operator": operator,
                "expected": raw.get("expected"),
                "tolerance": raw.get("tolerance"),
                "requirement_ids": list(
                    dict.fromkeys(
                        [str(value) for value in requirement_ids if str(value)]
                    )
                ),
            }
        )
    return normalized_assertions


def _normalize_requirements(
    requirements: Any,
    assertions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(requirements, list) or len(requirements) > 100:
        raise ValueError("requirements must be an array with at most 100 entries")
    assertion_ids = {assertion["id"] for assertion in assertions}
    linked_from_assertions: dict[str, list[str]] = {}
    for assertion in assertions:
        for requirement_id in assertion.get("requirement_ids") or []:
            linked_from_assertions.setdefault(requirement_id, []).append(assertion["id"])
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(requirements):
        if not isinstance(raw, dict):
            raise ValueError(f"requirements[{index}] must be an object")
        requirement_id = str(raw.get("id") or "").strip()
        if not requirement_id or requirement_id in seen:
            raise ValueError("requirement ids must be present and unique")
        seen.add(requirement_id)
        explicit_assertions = raw.get("assertion_ids") or []
        if not isinstance(explicit_assertions, list) or not all(isinstance(value, str) for value in explicit_assertions):
            raise ValueError(f"requirements[{index}].assertion_ids must be an array of strings")
        linked = list(dict.fromkeys([*explicit_assertions, *linked_from_assertions.get(requirement_id, [])]))
        unknown = sorted(set(linked) - assertion_ids)
        if unknown:
            raise ValueError(f"requirements[{index}] references unknown assertions: {', '.join(unknown)}")
        oracle = str(raw.get("oracle") or "contract")
        if oracle not in {"contract", "independent_oracle"}:
            raise ValueError(f"requirements[{index}].oracle must be contract or independent_oracle")
        normalized.append(
            {
                "id": requirement_id,
                "description": str(raw.get("description") or ""),
                "required": bool(raw.get("required", True)),
                "assertion_ids": linked,
                "oracle": oracle,
            }
        )
    unknown_requirement_links = sorted(set(linked_from_assertions) - seen)
    if unknown_requirement_links:
        raise ValueError(
            "assertions reference unknown requirements: " + ", ".join(unknown_requirement_links)
        )
    return normalized


def build_native_read_arguments(arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Map the safe public read vocabulary to the Autodesk read schema."""

    query_type = str(arguments.get("query_type") or "").strip()
    mapping = {
        "api_documentation": "apiDocumentation",
        "projects": "projects",
        "document": "document",
        "active_command": "activeCommand",
        "screenshot": "screenshot",
    }
    if query_type not in mapping:
        raise ValueError(f"unsupported query_type: {query_type}")
    payload: dict[str, Any] = {"queryType": mapping[query_type]}
    if query_type == "api_documentation":
        pattern = str(arguments.get("search_pattern") or "").strip()
        if not pattern:
            raise ValueError("search_pattern is required for api_documentation")
        payload["searchPattern"] = pattern
        if arguments.get("api_category"):
            payload["apiCategory"] = arguments["api_category"]
        if arguments.get("filter"):
            payload["filter"] = arguments["filter"]
    elif query_type == "document":
        operation = str(arguments.get("operation") or "list_open")
        operation_map = {"search": "search", "list_open": "open", "recent": "recent"}
        if operation not in operation_map:
            raise ValueError("document operation must be search, list_open, or recent")
        payload["operation"] = operation_map[operation]
        if operation == "search":
            name = str(arguments.get("name") or "").strip()
            if not name:
                raise ValueError("name is required for document search")
            payload["name"] = name
            if arguments.get("fusion_project"):
                payload["project"] = arguments["fusion_project"]
    elif query_type == "screenshot":
        for public, native in {
            "width": "width",
            "height": "height",
            "anti_aliasing": "antiAliasing",
            "transparent_background": "transparentBackground",
            "direction": "direction",
        }.items():
            if public in arguments and arguments[public] is not None:
                payload[native] = arguments[public]
    return query_type, payload


class FastPathService:
    """Orchestrate safe native reads, targeted inspection, execution, and recovery."""

    def __init__(
        self,
        call_native: NativeCall,
        manifest_fingerprint: Callable[[], str] | None = None,
        trusted_read_native: NativeCall | None = None,
    ) -> None:
        self._call_native = call_native
        self._trusted_read_native = trusted_read_native or call_native
        self._manifest_fingerprint = manifest_fingerprint or (lambda: "")
        self._last_operation: dict[str, Any] | None = None

    async def native_read(self, arguments: dict[str, Any]) -> FastPathResponse:
        query_type, payload = build_native_read_arguments(arguments)
        started = time.perf_counter()
        operation_id = _operation_id("read")
        if query_type == "active_command":
            result = await self._trusted_read_native(
                "fusion_mcp_execute",
                {"featureType": "script", "object": {"script": ACTIVE_COMMAND_SCRIPT}},
                semantics="read_only",
                operation_id=operation_id,
            )
        else:
            result = await self._call_native(
                "fusion_mcp_read",
                payload,
                semantics="read_only",
                operation_id=operation_id,
            )
        duration_ms = int((time.perf_counter() - started) * 1000)
        if not _result_ok(result):
            return FastPathResponse(
                {
                    "query_type": query_type,
                    "status": "read_failed",
                    "error": _result_error(result),
                    "manifest_fingerprint": self._manifest_fingerprint(),
                    "duration_ms": duration_ms,
                },
                is_error=True,
                meta=copy.deepcopy(_result_meta(result)),
            )
        data = copy.deepcopy(_result_data(result))
        if query_type == "active_command":
            data = _parse_script_payload(data)
        content = copy.deepcopy(_result_content(result))
        if query_type == "screenshot":
            data, extracted = _extract_image(data)
            for block in extracted:
                if not any(
                    existing.get("type") == "image"
                    and existing.get("data") == block.get("data")
                    and existing.get("mimeType") == block.get("mimeType")
                    for existing in content
                ):
                    content.append(block)
            image_blocks = [block for block in content if block.get("type") == "image"]
            if not image_blocks or not all(_valid_png_block(block) for block in image_blocks):
                return FastPathResponse(
                    {
                        "query_type": query_type,
                        "status": "read_failed",
                        "error": "screenshot did not return a valid PNG image block",
                        "manifest_fingerprint": self._manifest_fingerprint(),
                        "duration_ms": duration_ms,
                    },
                    is_error=True,
                )
            data["image_in_content"] = True
            data["mimeType"] = "image/png"
            if arguments.get("width") is not None:
                data.setdefault("width", int(arguments["width"]))
            if arguments.get("height") is not None:
                data.setdefault("height", int(arguments["height"]))
        return FastPathResponse(
            {
                "query_type": query_type,
                "status": "read_succeeded",
                "data": data,
                "manifest_fingerprint": self._manifest_fingerprint(),
                "duration_ms": duration_ms,
            },
            content=content,
            meta=copy.deepcopy(_result_meta(result)),
        )

    async def targeted_inspect(self, arguments: dict[str, Any]) -> FastPathResponse:
        normalized = validate_inspection_payload(arguments)
        operation_id = _operation_id("inspect")
        started = time.perf_counter()
        result = await self._trusted_read_native(
            "fusion_mcp_execute",
            {"featureType": "script", "object": {"script": build_targeted_inspection_script(normalized)}},
            semantics="read_only",
            operation_id=operation_id,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        if not _result_ok(result):
            return FastPathResponse(
                {"status": "inspection_failed", "error": _result_error(result), "duration_ms": duration_ms},
                is_error=True,
                meta=copy.deepcopy(_result_meta(result)),
            )
        inspection = _parse_script_payload(_result_data(result))
        return FastPathResponse(
            {
                "status": "read_succeeded",
                **inspection,
                "manifest_fingerprint": self._manifest_fingerprint(),
                "duration_ms": duration_ms,
            },
            meta=copy.deepcopy(_result_meta(result)),
        )

    async def fast_execute(self, arguments: dict[str, Any]) -> FastPathResponse:
        request = validate_fast_execute_request(arguments)
        policy = lint_fusion_script(
            request["script"],
            request["change_class"],
            allowed_target_ids=set(request["target_query_ids"]) if request["change_class"] == "scoped_update" else set(),
            allowed_component_paths=set(request["target_component_paths"]),
        )
        operation_id = _operation_id("fast")
        started = time.perf_counter()
        if not policy.allowed:
            return FastPathResponse(
                {
                    "operation_id": operation_id,
                    "execution_path": "native_fast",
                    "status": "blocked_before_apply",
                    "policy": policy.as_dict(),
                    "recovery_instruction": "Route this request through the Safe Harness or revise the script.",
                }
            )

        active = await self.native_read({"query_type": "active_command"})
        active_command = _active_command(active.payload.get("data"))
        if active.is_error or active_command:
            return FastPathResponse(
                {
                    "operation_id": operation_id,
                    "execution_path": "native_fast",
                    "status": "blocked_before_apply",
                    "policy": policy.as_dict(),
                    "reason": "active_command" if active_command else "active_command_check_failed",
                    "active_command": active_command,
                }
            )

        inspection_args = _inspection_args_for_request(
            request,
            include_state_fingerprint=request["change_class"] != "read_only",
        )
        readback_args = _inspection_args_for_request(
            request,
            include_state_fingerprint=request["change_class"] != "read_only",
        )
        baseline_response = await self.targeted_inspect(inspection_args)
        if baseline_response.is_error:
            return FastPathResponse(
                {
                    "operation_id": operation_id,
                    "execution_path": "native_fast",
                    "status": "blocked_before_apply",
                    "policy": policy.as_dict(),
                    "reason": "baseline_failed",
                    "baseline": baseline_response.payload,
                }
            )
        baseline = baseline_response.payload
        baseline_issue = _mutation_baseline_issue(request, baseline)
        if baseline_issue:
            return FastPathResponse(
                {
                    "operation_id": operation_id,
                    "execution_path": "native_fast",
                    "status": "blocked_before_apply",
                    "policy": policy.as_dict(),
                    "reason": "incomplete_baseline",
                    "baseline_issue": baseline_issue,
                    "baseline": baseline,
                    "transport_mutating_dispatch_count": 0,
                    "mutating_call_count": 0,
                    "recovery_instruction": "No mutation was dispatched. Repeat a bounded targeted inspection with sufficient budget or use the Safe Harness.",
                }
            )
        target_error, bindings = _validate_targets(request, baseline)
        if target_error:
            return FastPathResponse(
                {
                    "operation_id": operation_id,
                    "execution_path": "native_fast",
                    "status": "blocked_before_apply",
                    "policy": policy.as_dict(),
                    "reason": target_error,
                    "baseline": baseline,
                }
            )

        guarded_script = _guard_script(
            request["script"],
            baseline.get("document") or {},
            bindings=bindings,
        )
        protected_script = normalize_execute_script(guarded_script)
        executor_guard = protected_script_descriptor(protected_script)
        if not executor_guard["within_limit"]:
            return FastPathResponse(
                {
                    "operation_id": operation_id,
                    "execution_path": "native_fast",
                    "status": "blocked_before_apply",
                    "reason": "protected_script_size_limit",
                    "error_code": ErrorCode.SCRIPT_SIZE_LIMIT_EXCEEDED.value,
                    "message": (
                        f"Protected Fusion script is {executor_guard['protected_payload_bytes']} bytes; "
                        f"the configured limit is {executor_guard['limit_bytes']} bytes."
                    ),
                    "script_sha256": policy.script_sha256,
                    "policy": policy.as_dict(),
                    "baseline": baseline,
                    "executor_guard": executor_guard,
                    "native_call_count": 2,
                    "declared_mutation_count": 0 if request["change_class"] == "read_only" else 1,
                    "transport_mutating_dispatch_count": 0,
                    "mutating_call_count": 0,
                    "recovery_instruction": "No mutation was dispatched. Reduce or split the script before retrying.",
                }
            )
        execute_result = await self._call_native(
            "fusion_mcp_execute",
            {"featureType": "script", "object": {"script": guarded_script}},
            semantics="mutating",
            operation_id=operation_id,
        )
        after_response = await self.targeted_inspect(readback_args)
        after = after_response.payload if not after_response.is_error else {}
        readback_issue = (
            "readback_call_failed"
            if after_response.is_error
            else _mutation_baseline_issue(request, after)
        )
        verification = evaluate_verification(
            baseline,
            after,
            request["verification"]["assertions"],
            request["change_class"],
            request["verification"].get("requirements") or [],
        )
        if readback_issue:
            verification = {
                **verification,
                "passed": False,
                "assertions_passed": False,
                "assertion_status": "incomplete",
                "intent_coverage": (
                    "partial" if verification.get("intent_coverage") == "complete" else verification.get("intent_coverage", "none")
                ),
                "contract_verified": False,
                "readback_complete": False,
                "readback_issue": readback_issue,
            }
        else:
            verification = {
                **verification,
                "readback_complete": True,
                "readback_issue": None,
            }
        execution_error = None if _result_ok(execute_result) else _result_error(execute_result)
        error_code = _result_error_code(execute_result)
        execute_data = _result_data(execute_result)
        transport_meta = (_result_meta(execute_result).get("fusion_agent_transport") or {})
        dispatched = bool(transport_meta.get("dispatched", execute_data.get("dispatched", True)))
        mutation_outcome = str(
            transport_meta.get("mutation_outcome")
            or ("unknown" if error_code == "MUTATION_OUTCOME_UNKNOWN" else "known")
        )
        post_dispatch_replay_suppressed = bool(
            transport_meta.get("post_dispatch_replay_suppressed", dispatched)
        )
        if mutation_outcome == "unknown":
            # Positive readback can describe the current state, but it cannot
            # prove whether this uncertain dispatch caused that state.  Keep
            # contract assertions as useful evidence without promoting the
            # mutation outcome or permitting automatic replay.
            verification = {
                **verification,
                "contract_verified": False,
                "mutation_outcome": "unknown",
                "mutation_status": "outcome_unknown",
            }
            status = "mutation_outcome_unknown"
            verification_source = "post_dispatch_readback" if not readback_issue else "unavailable"
        elif error_code == "CALL_CANCELLED":
            if not dispatched:
                status = "blocked_before_apply"
                verification_source = "pre_apply_cancelled"
            else:
                status = "applied_verified" if verification["contract_verified"] and not readback_issue else "outcome_unknown"
                verification_source = "post_cancel_readback" if status == "applied_verified" else "unavailable"
        elif execution_error and "fusion agent document guard" in execution_error.lower():
            status = "blocked_before_apply"
            verification_source = "document_identity_guard"
        elif error_code in {"MANIFEST_DRIFT", "CONNECTION_UNAVAILABLE", "CLIENT_CLOSED"}:
            status = "blocked_before_apply"
            verification_source = "pre_apply_failure_readback"
        elif readback_issue and request["change_class"] != "read_only":
            status = "applied_unverified"
            verification_source = "partial_readback"
        elif execution_error:
            status = "partial_change_detected" if _snapshot_changed(baseline, after) else "execution_failed"
            verification_source = "post_failure_readback"
        elif after_response.is_error:
            status = "applied_unverified"
            verification_source = "unavailable"
        else:
            if verification["contract_verified"]:
                status = "applied_verified"
            elif verification["passed"]:
                status = "applied_partially_verified"
            else:
                status = "applied_unverified"
            verification_source = "post_apply"

        if readback_issue:
            drift_conclusion = "inconclusive"
        elif verification.get("passed"):
            drift_conclusion = "no_drift_in_observed_scope"
        elif _snapshot_changed(baseline, after):
            drift_conclusion = "drift_detected_in_observed_scope"
        else:
            drift_conclusion = "inconclusive"
        verification = {**verification, "drift_conclusion": drift_conclusion}

        duration_ms = int((time.perf_counter() - started) * 1000)
        evidence_content: list[dict[str, Any]] = []
        screenshot_payload: dict[str, Any] | None = None
        native_call_count = 4
        if request["verification"].get("include_screenshot"):
            screenshot = await self.native_read({"query_type": "screenshot"})
            screenshot_payload = screenshot.payload
            evidence_content = screenshot.content
            native_call_count += 1

        response = {
            "operation_id": operation_id,
            "execution_path": "native_fast",
            "intent": request["intent"],
            "change_class": request["change_class"],
            "status": status,
            "error_code": error_code,
            "script_sha256": policy.script_sha256,
            "api_references": request["api_references"],
            "policy": policy.as_dict(),
            "executor_guard": executor_guard,
            "baseline": baseline,
            "execution": {
                "ok": _result_ok(execute_result),
                "error": execution_error,
                "error_code": error_code,
                "dispatched": dispatched,
                "may_have_applied": bool(dispatched and mutation_outcome == "unknown"),
                "post_dispatch_replay_suppressed": post_dispatch_replay_suppressed,
                "mutation_outcome": mutation_outcome,
            },
            "after": after,
            "verification": {**verification, "source": verification_source},
            "manifest_fingerprint": self._manifest_fingerprint(),
            "duration_ms": duration_ms,
            "native_call_count": native_call_count,
            "declared_mutation_count": 0 if request["change_class"] == "read_only" else 1,
            "transport_mutating_dispatch_count": int(dispatched and request["change_class"] != "read_only"),
            "mutating_call_count": int(dispatched and request["change_class"] != "read_only"),
            "dispatched": dispatched,
            "may_have_applied": bool(dispatched and mutation_outcome == "unknown"),
            "post_dispatch_replay_suppressed": post_dispatch_replay_suppressed,
            "mutation_outcome": mutation_outcome,
            "mutation_status": (
                "outcome_unknown"
                if mutation_outcome == "unknown"
                else "observed_in_readback"
                if dispatched and not readback_issue and verification.get("passed")
                else "observed_in_readback"
                if dispatched and not readback_issue
                else "not_dispatched"
                if not dispatched
                else "unknown"
            ),
            "assertion_status": verification["assertion_status"],
            "intent_coverage": verification["intent_coverage"],
            "verification_level": verification["verification_level"],
            "bindings": {
                "targets": sorted(bindings["targets"]),
                "target_components": sorted(bindings["target_components"]),
            },
            "recovery_instruction": (
                "Do not save. Inspect the active design and use fusion_agent_recover_change only if the post-state still matches."
                if status != "applied_verified"
                else ""
            ),
        }
        if screenshot_payload is not None:
            response["screenshot"] = screenshot_payload
        if request["change_class"] != "read_only" and status in {"applied_verified", "applied_partially_verified", "applied_unverified", "partial_change_detected"}:
            self._last_operation = {
                "operation_id": operation_id,
                "document": after.get("document") or baseline.get("document"),
                "after_fingerprint": _snapshot_fingerprint(after),
                "after_state_fingerprint": (after.get("summary") or {}).get("state_fingerprint"),
                "state_fingerprint_truncated": bool(
                    (after.get("summary") or {}).get("state_fingerprint_truncated", True)
                ),
                "inspection_args": readback_args,
                "after": after,
                "recovery_phase": "applied",
            }
        return FastPathResponse(
            response,
            content=evidence_content,
            is_error=status in {"execution_failed", "outcome_unknown", "mutation_outcome_unknown"},
            meta=copy.deepcopy(_result_meta(execute_result)),
        )

    async def recover_change(self, arguments: dict[str, Any]) -> FastPathResponse:
        action = str(arguments.get("action") or "")
        operation_id = str(arguments.get("operation_id") or "")
        if action not in {"undo", "redo"}:
            raise ValueError("action must be undo or redo")
        if arguments.get("confirm") is not True:
            raise ValueError("confirm=true is required")
        if not self._last_operation or self._last_operation["operation_id"] != operation_id:
            return FastPathResponse({"status": "blocked_before_apply", "reason": "operation_is_not_latest"})
        recovery_phase = str(self._last_operation.get("recovery_phase") or "applied")
        expected_action = "undo" if recovery_phase == "applied" else "redo"
        if action != expected_action:
            return FastPathResponse(
                {
                    "status": "blocked_before_apply",
                    "reason": "recovery_action_not_available",
                    "expected_action": expected_action,
                }
            )
        verification = arguments.get("verification") or {}
        normalized = validate_inspection_payload(
            {"queries": verification.get("queries") or [], "limit_per_query": verification.get("limit_per_query", 20)}
        )
        assertions = _normalize_assertions(
            verification.get("assertions") or [],
            {query["id"] for query in normalized["queries"]},
        )
        active = await self.native_read({"query_type": "active_command"})
        active_command = _active_command(active.payload.get("data"))
        if active.is_error or active_command:
            return FastPathResponse(
                {
                    "status": "blocked_before_apply",
                    "reason": "active_command" if active_command else "active_command_check_failed",
                    "active_command": active_command,
                }
            )
        expected_state_fingerprint = self._last_operation.get("after_state_fingerprint")
        if self._last_operation.get("state_fingerprint_truncated") or not expected_state_fingerprint:
            return FastPathResponse(
                {"status": "blocked_before_apply", "reason": "recovery_state_fingerprint_unavailable"}
            )
        current = await self.targeted_inspect(self._last_operation["inspection_args"])
        current_summary = current.payload.get("summary") or {}
        if (
            current.is_error
            or current_summary.get("state_fingerprint_truncated")
            or current_summary.get("state_fingerprint") != expected_state_fingerprint
            or not _same_document(
                self._last_operation.get("document") or {},
                current.payload.get("document") or {},
            )
        ):
            return FastPathResponse({"status": "blocked_before_apply", "reason": "document_or_state_drift"})
        verification_before = await self.targeted_inspect(normalized)
        if verification_before.is_error:
            return FastPathResponse(
                {"status": "blocked_before_apply", "reason": "recovery_verification_baseline_failed"}
            )
        recovery_id = _operation_id("recover")
        result = await self._call_native(
            "fusion_mcp_update",
            {"featureType": action},
            semantics="mutating",
            operation_id=recovery_id,
        )
        after = await self.targeted_inspect(normalized)
        if not _result_ok(result):
            status = "outcome_unknown" if _result_error_code(result) == "MUTATION_OUTCOME_UNKNOWN" else "execution_failed"
            return FastPathResponse(
                {"operation_id": recovery_id, "status": status, "error": _result_error(result)},
                is_error=True,
            )
        evaluated = evaluate_verification(
            verification_before.payload,
            after.payload,
            assertions,
            "recovery",
        )
        status = "recovered_verified" if evaluated["passed"] else "recovery_unverified"
        if status == "recovered_verified":
            post_state = await self.targeted_inspect(self._last_operation["inspection_args"])
            post_summary = post_state.payload.get("summary") or {}
            post_fingerprint = post_summary.get("state_fingerprint")
            if (
                post_state.is_error
                or post_summary.get("state_fingerprint_truncated")
                or not post_fingerprint
            ):
                status = "recovery_unverified"
            else:
                self._last_operation.update(
                    {
                        "document": post_state.payload.get("document") or {},
                        "after_fingerprint": _snapshot_fingerprint(post_state.payload),
                        "after_state_fingerprint": post_fingerprint,
                        "state_fingerprint_truncated": False,
                        "after": post_state.payload,
                        "recovery_phase": "undone" if action == "undo" else "applied",
                    }
                )
        return FastPathResponse(
            {
                "operation_id": recovery_id,
                "status": status,
                "action": action,
                "verification": evaluated,
                "after": after.payload,
            },
            is_error=status != "recovered_verified",
        )


_INTERNAL_COMPONENT_QUERY_PREFIX = "__fusion_agent_component_"


def _component_query_id(component_path: str) -> str:
    digest = hashlib.sha256(component_path.encode("utf-8")).hexdigest()[:16]
    return f"{_INTERNAL_COMPONENT_QUERY_PREFIX}{digest}"


def _inspection_args_for_request(
    request: dict[str, Any],
    *,
    include_state_fingerprint: bool = False,
) -> dict[str, Any]:
    queries = copy.deepcopy(request["verification"]["queries"])
    existing_ids = {query["id"] for query in queries}
    for component_path in request["target_component_paths"]:
        query_id = _component_query_id(component_path)
        if query_id in existing_ids:
            raise ValueError(f"verification query id is reserved by Fusion Agent: {query_id}")
        existing_ids.add(query_id)
        queries.append(
            {
                "id": query_id,
                "entity_type": "component",
                "selector": {"path": component_path},
                "fields": ["exists", "valid"],
            }
        )
    if len(queries) > 50:
        raise ValueError("verification plus additive component bindings supports at most 50 queries")
    return {
        "queries": queries,
        "limit_per_query": request["verification"]["limit_per_query"],
        "include_state_fingerprint": include_state_fingerprint,
        "state_fingerprint_limit": 5000,
    }


def _target_record_error(match: Any, query_id: str) -> str | None:
    if not isinstance(match, dict):
        return f"invalid_target_record:{query_id}"
    if match.get("visible") is False:
        return f"hidden_target_requires_safe_harness:{query_id}"
    if match.get("is_referenced_component") is True:
        return f"referenced_target_requires_safe_harness:{query_id}"
    if int(match.get("occurrence_count_for_component") or 0) > 1:
        return f"shared_component_requires_safe_harness:{query_id}"
    return None


def _mutation_baseline_issue(request: dict[str, Any], snapshot: dict[str, Any]) -> str | None:
    """Fail closed before target binding when a mutation baseline is partial."""

    if request.get("change_class") == "read_only":
        return None
    if snapshot.get("complete") is not True:
        return "complete_not_true"
    if snapshot.get("counts_exact") is not True:
        return "counts_not_exact"
    if bool(snapshot.get("truncated")):
        return "snapshot_truncated"
    if snapshot.get("stop_reason") not in (None, "", "complete"):
        return f"stop_reason:{snapshot.get('stop_reason')}"
    for index, result in enumerate(snapshot.get("results", [])):
        if not isinstance(result, dict):
            return f"invalid_query_result:{index}"
        if result.get("match_count_exact") is not True:
            query_id = str(result.get("query_id") or index)
            return f"query_match_count_inexact:{query_id}"
    return None


def _validate_targets(
    request: dict[str, Any],
    snapshot: dict[str, Any],
) -> tuple[str | None, dict[str, dict[str, str]]]:
    by_id = {result.get("query_id"): result for result in snapshot.get("results", [])}
    document = snapshot.get("document") or {}
    bindings: dict[str, dict[str, str]] = {"targets": {}, "target_components": {}}
    if request["change_class"] != "read_only" and not (document.get("id") or document.get("runtime_id")):
        return "document_stable_identity_unavailable", bindings
    for query_id in request["target_query_ids"]:
        if query_id not in by_id:
            return f"target_query_missing:{query_id}", bindings
        result = by_id.get(query_id) or {}
        matches = result.get("matches") or []
        if result.get("ambiguous"):
            return f"ambiguous_target:{query_id}", bindings
        if request["change_class"] == "additive" and matches:
            return f"additive_target_already_exists:{query_id}", bindings
        if request["change_class"] == "scoped_update" and len(matches) != 1:
            return f"scoped_target_must_resolve_uniquely:{query_id}", bindings
        for match in matches:
            error = _target_record_error(match, query_id)
            if error:
                return error, bindings
        if request["change_class"] == "scoped_update":
            token = str(matches[0].get("entity_token") or "")
            if not token:
                return f"target_entity_token_missing:{query_id}", bindings
            bindings["targets"][query_id] = token

    if request["change_class"] == "additive":
        for component_path in request["target_component_paths"]:
            query_id = _component_query_id(component_path)
            result = by_id.get(query_id) or {}
            matches = result.get("matches") or []
            if result.get("ambiguous") or len(matches) != 1:
                return f"target_component_must_resolve_uniquely:{component_path}", bindings
            match = matches[0]
            error = _target_record_error(match, f"component:{component_path}")
            if error:
                return error, bindings
            paths = match.get("paths") or []
            if component_path not in paths:
                return f"target_component_path_mismatch:{component_path}", bindings
            token = (
                _ROOT_COMPONENT_BINDING
                if component_path == "root"
                else str(match.get("entity_token") or "")
            )
            if not token:
                return f"target_component_token_missing:{component_path}", bindings
            bindings["target_components"][component_path] = token
    return None, bindings


def evaluate_verification(
    baseline: dict[str, Any],
    after: dict[str, Any],
    assertions: list[dict[str, Any]],
    change_class: str,
    requirements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate the declared contract plus automatic document/count invariants."""

    details = []
    baseline_results = {item.get("query_id"): item for item in baseline.get("results", [])}
    after_results = {item.get("query_id"): item for item in after.get("results", [])}
    for assertion in assertions:
        before_value = _query_value(baseline_results.get(assertion["query_id"], {}), assertion["field"])
        after_value = _query_value(after_results.get(assertion["query_id"], {}), assertion["field"])
        passed, message = _compare(assertion, before_value, after_value)
        details.append({**assertion, "before": before_value, "actual": after_value, "passed": passed, "message": message})

    invariants = []
    same_document = _same_document(baseline.get("document") or {}, after.get("document") or {})
    invariants.append({"name": "document_identity_unchanged", "passed": same_document})
    before_summary = baseline.get("summary") or {}
    after_summary = after.get("summary") or {}
    count_keys = {
        "components",
        "occurrences",
        "bodies",
        "sketches",
        "features",
        "parameters",
        "visible_body_count",
    }
    comparable_counts = [key for key in count_keys if key in before_summary]
    if change_class == "additive":
        counts_ok = all(after_summary.get(key, 0) >= before_summary.get(key, 0) for key in comparable_counts)
        invariants.append({"name": "additive_counts_do_not_decrease", "passed": counts_ok})
    elif change_class in {"read_only", "scoped_update"}:
        counts_ok = all(after_summary.get(key) == before_summary.get(key) for key in comparable_counts)
        invariants.append({"name": f"{change_class}_counts_unchanged", "passed": counts_ok})
    visible_bbox = after_summary.get("visible_body_bbox_mm")
    if visible_bbox is not None:
        invariants.append({"name": "visible_body_bbox_valid", "passed": _valid_bbox(visible_bbox)})
    target_matches = [
        match
        for result in after.get("results", [])
        for match in result.get("matches", [])
        if isinstance(match, dict)
    ]
    feature_health = [
        str(match.get("health") or "").strip().lower()
        for match in target_matches
        if match.get("entity_type") == "feature" or "health" in match
    ]
    if feature_health:
        invariants.append(
            {
                "name": "target_feature_health",
                "passed": all(_healthy_feature_value(value) for value in feature_health),
                "values": feature_health,
            }
        )
    bounding_boxes = [match.get("bounding_box_mm") for match in target_matches if match.get("bounding_box_mm") is not None]
    if bounding_boxes:
        bbox_ok = all(_valid_bbox(bbox) for bbox in bounding_boxes)
        invariants.append({"name": "target_bounding_boxes_valid", "passed": bbox_ok})
    assertions_required = change_class != "read_only"
    passed = (
        (bool(details) or not assertions_required)
        and all(item["passed"] for item in details)
        and all(item["passed"] for item in invariants)
    )
    requirements = requirements or []
    assertion_results = {item["id"]: item for item in details}
    requirement_results = []
    for requirement in requirements:
        assertion_ids = list(requirement.get("assertion_ids") or [])
        covered = bool(assertion_ids) and all(assertion_id in assertion_results for assertion_id in assertion_ids)
        requirement_passed = covered and all(assertion_results[assertion_id]["passed"] for assertion_id in assertion_ids)
        independent = requirement.get("oracle") == "independent_oracle"
        requirement_results.append(
            {
                **requirement,
                "covered": covered and not independent,
                "passed": requirement_passed and not independent,
                **({"oracle_evidence": "not_available"} if independent else {}),
            }
        )
    required_results = [item for item in requirement_results if item.get("required", True)]
    if not required_results:
        intent_coverage = "none"
    elif all(item["covered"] for item in required_results):
        intent_coverage = "complete"
    else:
        intent_coverage = "partial" if any(item["covered"] for item in required_results) else "none"
    contract_verified = bool(
        required_results
        and intent_coverage == "complete"
        and passed
        and all(item["passed"] for item in required_results)
    )
    independent_declared = any(
        item.get("oracle") == "independent_oracle" for item in required_results
    )
    if independent_declared:
        # Public Fast Path assertions are contract checks.  They cannot elevate
        # themselves to an independent oracle merely by labeling a requirement.
        contract_verified = False
        verification_level = "independent_oracle"
    elif required_results:
        verification_level = "contract"
    else:
        verification_level = "assertions_only"
    return {
        "passed": passed,
        "assertions_passed": passed,
        "assertion_status": "passed" if passed else "failed",
        "assertions": details,
        "invariants": invariants,
        "requirements": requirement_results,
        "intent_coverage": intent_coverage,
        "verification_level": verification_level,
        "contract_verified": contract_verified,
    }


def _valid_bbox(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    size = value.get("size_mm")
    if not isinstance(size, list) or len(size) != 3:
        return False
    try:
        return all(math.isfinite(float(item)) and float(item) >= 0.0 for item in size)
    except (TypeError, ValueError):
        return False


def _healthy_feature_value(value: str) -> bool:
    normalized = value.replace("_", "").replace(" ", "").lower()
    if normalized in {"0", "ok", "healthy", "noerror"}:
        return True
    return "healthy" in normalized and not any(part in normalized for part in ("unhealthy", "warning", "error"))


def _compare(assertion: dict[str, Any], before: Any, actual: Any) -> tuple[bool, str]:
    operator = assertion["operator"]
    expected = assertion.get("expected")
    try:
        if operator == "eq":
            passed = actual == expected
        elif operator == "ne":
            passed = actual != expected
        elif operator == "approx":
            passed = math.isclose(float(actual), float(expected), abs_tol=float(assertion["tolerance"]), rel_tol=0.0)
        elif operator == "gte":
            passed = actual >= expected
        elif operator == "lte":
            passed = actual <= expected
        elif operator == "contains":
            passed = expected in actual
        elif operator == "unchanged":
            passed = actual == before
        elif operator == "increased_by":
            passed = actual == before + expected
        elif operator == "decreased_by":
            passed = actual == before - expected
        else:
            passed = False
    except (TypeError, ValueError, KeyError):
        passed = False
    return passed, "ok" if passed else f"expected {operator} {expected!r}, got {actual!r}"


def _query_value(result: dict[str, Any], field_name: str) -> Any:
    if field_name == "exists":
        return bool(result.get("matches"))
    matches = result.get("matches") or []
    if not matches:
        return None
    value: Any = matches[0]
    for part in field_name.split("."):
        if isinstance(value, list) and part.isdigit():
            index = int(part)
            value = value[index] if index < len(value) else None
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def _guard_script(
    script: str,
    document: dict[str, Any],
    *,
    bindings: dict[str, dict[str, str]] | None = None,
) -> str:
    expected_name = json.dumps(document.get("name") or "")
    expected_id = json.dumps(document.get("id") or "")
    expected_runtime_id = json.dumps(document.get("runtime_id") or "")
    binding_payload = bindings or {"targets": {}, "target_components": {}}
    expected_targets = json.dumps(binding_payload.get("targets") or {}, sort_keys=True)
    expected_components = json.dumps(binding_payload.get("target_components") or {}, sort_keys=True)
    root_component_binding = json.dumps(_ROOT_COMPONENT_BINDING)
    parsed = ast.parse(script)
    entrypoints = [
        node
        for node in parsed.body
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    ]
    if len(entrypoints) != 1:
        raise ValueError("guarded script must contain exactly one run entrypoint")
    entrypoints[0].name = "_fusion_agent_user_run"
    guarded_user_script = ast.unparse(ast.fix_missing_locations(parsed))
    guarded = (
        guarded_user_script.rstrip()
        + "\n\n"
        + "def run(_context: str):\n"
        + "    import adsk.core\n"
        + "    import adsk.fusion\n"
        + "    _app = adsk.core.Application.get()\n"
        + "    _doc = _app.activeDocument\n"
        + "    if _doc is None:\n"
        + "        raise RuntimeError('Fusion Agent document guard: no active document')\n"
        + f"    _expected_name = {expected_name}\n"
        + f"    _expected_id = {expected_id}\n"
        + f"    _expected_runtime_id = {expected_runtime_id}\n"
        + f"    _expected_targets = {expected_targets}\n"
        + f"    _expected_components = {expected_components}\n"
        + f"    _root_component_binding = {root_component_binding}\n"
        + "    _actual_id = ''\n"
        + "    _actual_runtime_id = ''\n"
        + "    try:\n"
        + "        _actual_id = _doc.dataFile.id if _doc.dataFile else ''\n"
        + "    except BaseException:\n"
        + "        _actual_id = ''\n"
        + "    if _actual_id:\n"
        + "        _actual_runtime_id = 'data:' + _actual_id\n"
        + "    else:\n"
        + "        try:\n"
        + "            _identity_design = adsk.fusion.Design.cast(_doc.products.itemByProductType('DesignProductType'))\n"
        + "            _identity_root = _identity_design.rootComponent if _identity_design else None\n"
        + "            _marker = _identity_root.attributes.itemByName('fusion_agent_benchmark', 'trial_marker') if _identity_root else None\n"
        + "            _actual_runtime_id = 'marker:' + _marker.value if _marker and _marker.value else ''\n"
        + "        except BaseException:\n"
        + "            _actual_runtime_id = ''\n"
        + "    if _expected_name and _doc.name != _expected_name:\n"
        + "        raise RuntimeError('Fusion Agent document guard: active document changed')\n"
        + "    if _expected_id and _actual_id and _actual_id != _expected_id:\n"
        + "        raise RuntimeError('Fusion Agent document guard: data file changed')\n"
        + "    if _expected_runtime_id and _actual_runtime_id != _expected_runtime_id:\n"
        + "        raise RuntimeError('Fusion Agent document guard: runtime document changed')\n"
        + "    _design = _app.activeProduct\n"
        + "    def _resolve_bound_entity(_token):\n"
        + "        if _token == _root_component_binding:\n"
        + "            return _design.rootComponent\n"
        + "        _found = _design.findEntityByToken(_token)\n"
        + "        if isinstance(_found, tuple) and len(_found) == 2 and isinstance(_found[1], bool):\n"
        + "            _found = _found[0]\n"
        + "        if isinstance(_found, (list, tuple)):\n"
        + "            _items = list(_found)\n"
        + "        else:\n"
        + "            try:\n"
        + "                _items = list(_found) if _found is not None else []\n"
        + "            except TypeError:\n"
        + "                _count = int(getattr(_found, 'count', 0) or 0) if _found is not None else 0\n"
        + "                if _count:\n"
        + "                    _items = [_found.item(_index) for _index in range(_count)]\n"
        + "                else:\n"
        + "                    _items = [_found] if _found is not None else []\n"
        + "        _items = [_item for _item in _items if _item is not None]\n"
        + "        if len(_items) != 1:\n"
        + "            raise RuntimeError('Fusion Agent target binding guard: entity token did not resolve uniquely')\n"
        + "        return _items[0]\n"
        + "    global targets, target_components\n"
        + "    targets = {_key: _resolve_bound_entity(_token) for _key, _token in _expected_targets.items()}\n"
        + "    target_components = {_key: _resolve_bound_entity(_token) for _key, _token in _expected_components.items()}\n"
        + "    return _fusion_agent_user_run(_context)\n"
    )
    return guarded


def _same_document(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if not before or not after:
        return False
    if before.get("id") and after.get("id"):
        return before["id"] == after["id"]
    if before.get("runtime_id") and after.get("runtime_id"):
        return before["runtime_id"] == after["runtime_id"]
    return before.get("name") == after.get("name")


def _snapshot_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return bool(after) and _snapshot_fingerprint(before) != _snapshot_fingerprint(after)


def _snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    comparable = {key: snapshot.get(key) for key in ("document", "summary", "results")}
    return hashlib.sha256(json.dumps(comparable, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _operation_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _result_ok(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("ok", not result.get("isError", False)))
    return bool(getattr(result, "ok", False))


def _result_data(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        data = result.get("data", result.get("structuredContent", result))
    else:
        data = getattr(result, "data", {})
    return data if isinstance(data, dict) else {"value": data}


def _result_content(result: Any) -> list[dict[str, Any]]:
    value = result.get("content", []) if isinstance(result, dict) else getattr(result, "content", [])
    content = []
    for block in value or []:
        if isinstance(block, dict):
            content.append(block)
        elif hasattr(block, "model_dump"):
            content.append(block.model_dump(by_alias=True, mode="json", exclude_none=True))
    return content


def _result_meta(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        value = result.get("_meta", result.get("meta", {}))
    else:
        value = getattr(result, "meta", {})
    return value if isinstance(value, dict) else {}


def _result_error(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("error_message") or result.get("error") or "native call failed")
    return str(getattr(result, "error_message", None) or "native call failed")


def _result_error_code(result: Any) -> str | None:
    value = result.get("error_code") if isinstance(result, dict) else getattr(result, "error_code", None)
    return str(value) if value is not None else None


def _parse_script_payload(data: dict[str, Any]) -> dict[str, Any]:
    for key in ("message", "text"):
        value = data.get(key)
        if isinstance(value, str):
            candidate = value.strip()
            try:
                loaded = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(loaded, dict):
                return loaded
    if data.get("success") is True or "results" in data:
        return data
    return {"success": True, **data}


def _active_command(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    candidate = data.get("activeCommand")
    if isinstance(candidate, dict) and not candidate.get("isDefaultCommand", False):
        return candidate
    if isinstance(candidate, str) and candidate.strip():
        return {"id": candidate.strip(), "isDefaultCommand": False}
    if candidate not in (None, False, "") and not isinstance(candidate, dict):
        return {"value": candidate, "isDefaultCommand": False}
    return None


def _extract_image(data: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = copy.deepcopy(data)
    content: list[dict[str, Any]] = []
    candidates = [result]
    for key in ("image", "screenshot", "result"):
        value = result.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    for candidate in candidates:
        encoded = candidate.get("base64Data") or candidate.get("data")
        mime_type = candidate.get("mimeType") or candidate.get("mime_type") or "image/png"
        if isinstance(encoded, str) and mime_type.startswith("image/"):
            try:
                base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                continue
            content.append({"type": "image", "data": encoded, "mimeType": mime_type})
            candidate.pop("base64Data", None)
            candidate.pop("data", None)
            candidate["image_in_content"] = True
            candidate["mimeType"] = mime_type
            break
    return result, content


def _valid_png_block(block: dict[str, Any]) -> bool:
    if str(block.get("mimeType") or block.get("mime_type") or "").lower() != "image/png":
        return False
    encoded = block.get("data")
    if not isinstance(encoded, str):
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return False
    return decoded.startswith(b"\x89PNG\r\n\x1a\n")
