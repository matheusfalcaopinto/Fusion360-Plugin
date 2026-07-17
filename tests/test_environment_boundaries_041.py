from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (ROOT / "harness" / "apps", ROOT / "harness" / "packages")

# Process environment access is intentionally limited to these synchronous startup
# boundaries. RuntimeConfiguration is the server snapshot factory; the remaining
# entries are compatibility constructors/factories used by standalone CLI commands
# and tests before an event loop starts. Exact function names keep this allowlist
# reviewable and prevent an entire module from silently becoming an environment API.
ALLOWED_SYNC_ENVIRONMENT_READS: dict[str, frozenset[str]] = {
    "harness/apps/fusion_agent_mcp/profiles.py": frozenset({"resolve_tool_profile"}),
    "harness/apps/fusion_agent_mcp/runtime.py": frozenset(
        {
            "RuntimeConfiguration.from_environment",
            "_choice_env",
            "_env_bool",
            "_float_env",
            "_int_env",
            "_optional_env",
        }
    ),
    "harness/packages/agent_core/authority.py": frozenset(
        {"AuthorityPolicy.from_environment"}
    ),
    "harness/packages/benchmark/artifacts.py": frozenset({"collect_environment"}),
    "harness/packages/benchmark/codex_driver.py": frozenset(
        {"CodexE2EDriver.__init__", "discover_codex_executable"}
    ),
    "harness/packages/benchmark/public.py": frozenset(
        {"PublicBenchmarkRunner.__init__"}
    ),
    "harness/packages/cli/main.py": frozenset(
        {
            "_candidate_endpoints",
            "_default_mode",
            "_doctor",
            "_env_bool",
            "_http_get_probe",
            "_startup_environment_snapshot",
        }
    ),
    "harness/packages/fusion_mcp_adapter/backend.py": frozenset({"selected_backend"}),
    "harness/packages/fusion_mcp_adapter/endpoint_policy.py": frozenset(
        {"validate_endpoint"}
    ),
    "harness/packages/fusion_mcp_adapter/real_client.py": frozenset(
        {"RealMcpClient.__init__"}
    ),
}


@dataclass(frozen=True, slots=True)
class EnvironmentRead:
    path: str
    line: int
    scope: str
    async_scope: bool
    import_time: bool

    def describe(self) -> str:
        location = f"{self.path}:{self.line}"
        return f"{location} ({self.scope})"


class _EnvironmentReadVisitor(ast.NodeVisitor):
    def __init__(self, path: str, tree: ast.Module) -> None:
        self.path = path
        self.os_aliases = {"os"}
        self.getenv_aliases: set[str] = set()
        self.environ_aliases: set[str] = set()
        self.scope: list[str] = []
        self.async_depth = 0
        self.function_depth = 0
        self.reads: list[EnvironmentRead] = []
        self._recorded: set[tuple[int, int]] = set()
        self._collect_import_aliases(tree)

    def _collect_import_aliases(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "os":
                        self.os_aliases.add(alias.asname or "os")
            elif isinstance(node, ast.ImportFrom) and node.module == "os":
                for alias in node.names:
                    if alias.name == "getenv":
                        self.getenv_aliases.add(alias.asname or alias.name)
                    elif alias.name == "environ":
                        self.environ_aliases.add(alias.asname or alias.name)

    def _record(self, node: ast.AST) -> None:
        key = (node.lineno, node.col_offset)
        if key in self._recorded:
            return
        self._recorded.add(key)
        self.reads.append(
            EnvironmentRead(
                path=self.path,
                line=node.lineno,
                scope=".".join(self.scope) or "<module>",
                async_scope=self.async_depth > 0,
                import_time=self.function_depth == 0,
            )
        )

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        function = node.func
        if (
            isinstance(function, ast.Attribute)
            and function.attr == "getenv"
            and isinstance(function.value, ast.Name)
            and function.value.id in self.os_aliases
        ) or (isinstance(function, ast.Name) and function.id in self.getenv_aliases):
            self._record(node)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        if (
            node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id in self.os_aliases
        ):
            self._record(node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if isinstance(node.ctx, ast.Load) and node.id in self.environ_aliases:
            self._record(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(  # noqa: N802
        self, node: ast.AsyncFunctionDef
    ) -> None:
        self._visit_function(node, is_async=True)

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        is_async: bool,
    ) -> None:
        # Defaults, annotations, and decorators execute in their enclosing scope;
        # for a module-level function that means import time.
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)

        self.scope.append(node.name)
        self.function_depth += 1
        self.async_depth += int(is_async)
        for statement in node.body:
            self.visit(statement)
        self.async_depth -= int(is_async)
        self.function_depth -= 1
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        # A class body and its bases/decorators execute in the enclosing scope.
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)
        self.scope.append(node.name)
        for statement in node.body:
            self.visit(statement)
        self.scope.pop()


def _environment_reads() -> list[EnvironmentRead]:
    reads: list[EnvironmentRead] = []
    for source_root in SOURCE_ROOTS:
        for path in sorted(source_root.rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            visitor = _EnvironmentReadVisitor(relative, tree)
            visitor.visit(tree)
            reads.extend(visitor.reads)
    return reads


def _format_reads(reads: list[EnvironmentRead]) -> str:
    return "\n".join(f"- {read.describe()}" for read in reads)


def test_async_functions_never_read_process_environment() -> None:
    violations = [read for read in _environment_reads() if read.async_scope]

    assert not violations, (
        "async code must use its immutable request/startup snapshot, not the "
        f"process environment:\n{_format_reads(violations)}"
    )


def test_environment_reads_are_confined_to_explicit_startup_boundaries() -> None:
    reads = _environment_reads()
    import_time = [read for read in reads if read.import_time]
    unexpected = [
        read
        for read in reads
        if not read.import_time
        and read.scope not in ALLOWED_SYNC_ENVIRONMENT_READS.get(read.path, frozenset())
    ]
    observed = {(read.path, read.scope) for read in reads if not read.import_time}
    allowed = {
        (path, scope)
        for path, scopes in ALLOWED_SYNC_ENVIRONMENT_READS.items()
        for scope in scopes
    }
    stale_allowlist = sorted(allowed - observed)

    assert not import_time, (
        "import-time environment reads make timeout/authorization policy depend "
        f"on module import order:\n{_format_reads(import_time)}"
    )
    assert not unexpected, (
        "environment reads require an explicit synchronous startup boundary:\n"
        f"{_format_reads(unexpected)}"
    )
    assert not stale_allowlist, (
        "remove obsolete startup-boundary exceptions from the allowlist:\n"
        + "\n".join(f"- {path} ({scope})" for path, scope in stale_allowlist)
    )
