"""Fail-closed bridge between the shared Fusion runtime and real benchmarks.

The stock backend implements only audited disposable-document lifecycle
primitives through the internal Autodesk surface.  Canonical fixture setup,
route actions, independent oracles, and containment counters remain explicit
capabilities.  Until all selected-case capabilities exist, preflight fails
before creating a document or dispatching a route.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Protocol

from benchmark.models import BenchmarkCase, ExecutionObservation, ExecutionPath
from benchmark.runner import (
    BenchmarkExecutionError,
    IndependentEvidence,
    RealTrialFinish,
    RealTrialStart,
    TrialContext,
    enforce_route_lock,
)


COMMON_CAPABILITIES = frozenset(
    {
        "snapshot_original_document",
        "create_unsaved_trial_document",
        "apply_unique_trial_marker",
        "read_active_fixture_identity",
        "observe_oracle_independently",
        "close_trial_without_save",
        "restore_original_document",
        "read_active_document_identity",
        "list_open_documents",
        "assert_no_save_or_sync",
    }
)
CANONICAL_ALL_CAPABILITY = "canonical_real_fixture_actions"
_STOCK_LIFECYCLE_CAPABILITIES = frozenset(
    {
        "snapshot_original_document",
        "create_unsaved_trial_document",
        "apply_unique_trial_marker",
        "read_active_fixture_identity",
        "close_trial_without_save",
        "restore_original_document",
        "read_active_document_identity",
        "list_open_documents",
    }
)
ROUTE_CAPABILITIES: dict[ExecutionPath, str] = {
    "safe_harness": "execute_safe_harness_route",
    "native_fast": "execute_native_fast_route",
}


@dataclass(frozen=True, slots=True)
class FixtureSession:
    """Identity returned once an isolated unsaved trial document exists."""

    original_document_id: str | None
    fixture_document_id: str
    fixture_marker: str
    fixture_fingerprint: str
    unsaved: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FixtureIdentity:
    """Fresh, read-only identity evidence for the currently active document."""

    document_id: str | None
    fixture_marker: str | None
    fixture_fingerprint: str | None
    unsaved: bool


@dataclass(frozen=True, slots=True)
class ContainmentAudit:
    """Independent counters collected after trial teardown."""

    save_count: int = 0
    hub_sync_count: int = 0
    personal_project_access_count: int = 0
    parallel_overlap_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class RuntimeBenchmarkBackend(Protocol):
    """Reviewed implementation hook backed only by public safe runtime APIs."""

    def capabilities(self) -> set[str] | Awaitable[set[str]]:
        """Return concrete capabilities; names are validated before mutation."""

    async def prepare_fixture(self, context: TrialContext) -> FixtureSession:
        """Create a new unsaved document and apply the exact unique marker."""

    async def read_fixture_identity(
        self,
        context: TrialContext,
        session: FixtureSession,
    ) -> FixtureIdentity:
        """Read active identity without using executor-reported state."""

    async def execute_safe_harness(
        self,
        context: TrialContext,
        session: FixtureSession,
    ) -> ExecutionObservation:
        """Execute the registered canonical action through Safe Harness."""

    async def execute_native_fast(
        self,
        context: TrialContext,
        session: FixtureSession,
    ) -> ExecutionObservation:
        """Execute the registered canonical action through Native Fast Path."""

    async def observe_oracle(
        self,
        context: TrialContext,
        session: FixtureSession,
    ) -> IndependentEvidence | dict[str, Any]:
        """Read independent programmatic evidence while the fixture is active."""

    async def close_fixture_without_save(
        self,
        context: TrialContext,
        session: FixtureSession,
    ) -> bool:
        """Close only the exact trial document and explicitly decline saving."""

    async def restore_original_document(
        self,
        context: TrialContext,
        session: FixtureSession,
    ) -> bool:
        """Restore the document that was active before fixture preparation."""

    async def read_active_document_id(self) -> str | None:
        """Return current active document identity after restoration."""

    async def list_open_document_ids(self) -> list[str]:
        """Independently list open documents after close/restore."""

    async def containment_audit(
        self,
        context: TrialContext,
        session: FixtureSession,
    ) -> ContainmentAudit:
        """Return independent save/sync/project/overlap counters."""


class FusionRuntimeLifecycleBackend:
    """Stock, audited lifecycle primitives for future canonical real trials.

    This adapter can create, mark, identify, close, list, and reactivate
    disposable unsaved Fusion documents through the shared persistent runtime.
    It deliberately advertises no fixture setup, route action, oracle, or
    containment-audit capability.  Therefore the stock v2 real suite still
    fails preflight before the first MCP call until those code-owned registries
    are implemented and reviewed.
    """

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def capabilities(self) -> set[str]:
        return set(_STOCK_LIFECYCLE_CAPABILITIES)

    async def prepare_fixture(self, context: TrialContext) -> FixtureSession:
        fingerprint = hashlib.sha256(context.fixture_marker.encode("utf-8")).hexdigest()
        original_document_id = await self.read_active_document_id()
        try:
            payload = await self._script_call(
                _prepare_fixture_script(context.fixture_marker, fingerprint),
                semantics="mutating",
                operation_id=f"benchmark:{context.trial_id}:prepare",
            )
            session = FixtureSession(
                original_document_id=_optional_payload_string(
                    payload.get("original_document_id")
                ),
                fixture_document_id=_required_payload_string(
                    payload.get("fixture_document_id"), "fixture_document_id"
                ),
                fixture_marker=_required_payload_string(
                    payload.get("fixture_marker"), "fixture_marker"
                ),
                fixture_fingerprint=_required_payload_string(
                    payload.get("fixture_fingerprint"), "fixture_fingerprint"
                ),
                unsaved=payload.get("unsaved") is True,
                metadata={
                    "lifecycle_backend": "stock",
                    "fixture_id": context.fixture.id,
                    "original_open_document_ids": _payload_string_list(
                        payload.get("original_open_document_ids"),
                        "original_open_document_ids",
                    ),
                },
            )
            if session.original_document_id != original_document_id:
                raise BenchmarkExecutionError(
                    "active document changed during real fixture preparation"
                )
            if session.fixture_marker != context.fixture_marker:
                raise BenchmarkExecutionError(
                    "real fixture marker write did not round-trip"
                )
            if session.fixture_fingerprint != fingerprint:
                raise BenchmarkExecutionError(
                    "real fixture fingerprint write did not round-trip"
                )
            if not session.unsaved:
                raise BenchmarkExecutionError("real benchmark fixture is not unsaved")
            return session
        except BaseException:
            # A post-dispatch transport loss may hide a successfully created
            # fixture.  Teardown searches only for the unique marker and never
            # replays the create operation.
            await self._best_effort_close_marker(
                context.fixture_marker,
                fingerprint,
                context.trial_id,
            )
            await self._best_effort_restore(original_document_id, context.trial_id)
            raise

    async def read_fixture_identity(
        self,
        context: TrialContext,
        session: FixtureSession,
    ) -> FixtureIdentity:
        payload = await self._script_call(
            _active_identity_script(),
            semantics="read_only",
            operation_id=f"benchmark:{context.trial_id}:identity",
        )
        return FixtureIdentity(
            document_id=_optional_payload_string(payload.get("document_id")),
            fixture_marker=_optional_payload_string(payload.get("fixture_marker")),
            fixture_fingerprint=_optional_payload_string(
                payload.get("fixture_fingerprint")
            ),
            unsaved=payload.get("unsaved") is True,
        )

    async def execute_safe_harness(
        self,
        context: TrialContext,
        session: FixtureSession,
    ) -> ExecutionObservation:
        del context, session
        raise BenchmarkExecutionError(
            "REAL_BENCHMARK_CAPABILITY_MISSING: canonical Safe Harness action registry"
        )

    async def execute_native_fast(
        self,
        context: TrialContext,
        session: FixtureSession,
    ) -> ExecutionObservation:
        del context, session
        raise BenchmarkExecutionError(
            "REAL_BENCHMARK_CAPABILITY_MISSING: canonical Native Fast action registry"
        )

    async def observe_oracle(
        self,
        context: TrialContext,
        session: FixtureSession,
    ) -> IndependentEvidence | dict[str, Any]:
        del context, session
        raise BenchmarkExecutionError(
            "REAL_BENCHMARK_CAPABILITY_MISSING: canonical independent real oracle registry"
        )

    async def close_fixture_without_save(
        self,
        context: TrialContext,
        session: FixtureSession,
    ) -> bool:
        payload = await self._script_call(
            _close_fixture_script(
                session.fixture_document_id,
                session.fixture_marker,
                session.fixture_fingerprint,
            ),
            semantics="mutating",
            operation_id=f"benchmark:{context.trial_id}:close",
        )
        return payload.get("closed") is True

    async def restore_original_document(
        self,
        context: TrialContext,
        session: FixtureSession,
    ) -> bool:
        return await self._restore_document_id(
            session.original_document_id,
            operation_id=f"benchmark:{context.trial_id}:restore",
        )

    async def read_active_document_id(self) -> str | None:
        payload = await self._script_call(
            _active_document_id_script(),
            semantics="read_only",
            operation_id="benchmark:lifecycle:active-document",
        )
        document_id = _optional_payload_string(payload.get("document_id"))
        if payload.get("document_present") is True and document_id is None:
            raise BenchmarkExecutionError(
                "active document has no stable data identity; close or save the unsaved document "
                "before running a real benchmark"
            )
        if document_id is not None and not document_id.startswith("data:"):
            raise BenchmarkExecutionError(
                "active document identity is not backed by a saved Fusion data file; "
                "close the disposable benchmark document before continuing"
            )
        return document_id

    async def list_open_document_ids(self) -> list[str]:
        payload = await self._script_call(
            _list_open_documents_script(),
            semantics="read_only",
            operation_id="benchmark:lifecycle:list-open",
        )
        values = payload.get("document_ids")
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise BenchmarkExecutionError(
                "real lifecycle list-open returned an invalid payload"
            )
        return values

    async def containment_audit(
        self,
        context: TrialContext,
        session: FixtureSession,
    ) -> ContainmentAudit:
        del context, session
        raise BenchmarkExecutionError(
            "REAL_BENCHMARK_CAPABILITY_MISSING: independent save/sync containment audit"
        )

    async def _restore_document_id(
        self, document_id: str | None, *, operation_id: str
    ) -> bool:
        payload = await self._script_call(
            _restore_document_script(document_id),
            semantics="mutating",
            operation_id=operation_id,
        )
        return payload.get("restored") is True

    async def _best_effort_close_marker(
        self,
        marker: str,
        fingerprint: str,
        trial_id: str,
    ) -> None:
        try:
            await self._script_call(
                _close_fixture_by_marker_script(marker, fingerprint),
                semantics="mutating",
                operation_id=f"benchmark:{trial_id}:prepare-cleanup",
            )
        except BaseException:
            return

    async def _best_effort_restore(
        self, document_id: str | None, trial_id: str
    ) -> None:
        try:
            await self._restore_document_id(
                document_id,
                operation_id=f"benchmark:{trial_id}:prepare-restore",
            )
        except BaseException:
            return

    async def _script_call(
        self,
        script: str,
        *,
        semantics: str,
        operation_id: str,
    ) -> dict[str, Any]:
        if semantics == "read_only":
            result = await self.runtime._call_trusted_native_real(
                "fusion_mcp_execute",
                {"featureType": "script", "object": {"script": script}},
                semantics=semantics,
                operation_id=operation_id,
            )
        else:
            result = await self.runtime._call_native_real(
                "fusion_mcp_execute",
                {"featureType": "script", "object": {"script": script}},
                semantics=semantics,
                operation_id=operation_id,
            )
        return _decode_script_payload(result, operation_id=operation_id)


class FusionRuntimeBenchmarkBridge:
    """Route executors, oracle, and lifecycle sharing one Fusion runtime.

    The backend is deliberately explicit.  A stock runtime supplies lifecycle
    primitives, while missing case-specific actions/oracles keep real execution
    fail-closed without MCP calls.
    """

    def __init__(
        self, runtime: Any, backend: RuntimeBenchmarkBackend | None = None
    ) -> None:
        self.runtime = runtime
        self.backend = backend or getattr(runtime, "real_benchmark_backend", None)
        self._sessions: dict[str, FixtureSession] = {}

    @property
    def route_executors(self) -> dict[ExecutionPath, Any]:
        return {
            "safe_harness": _RuntimeRouteExecutor(self, "safe_harness"),
            "native_fast": _RuntimeRouteExecutor(self, "native_fast"),
        }

    async def preflight(
        self,
        execution_paths: list[ExecutionPath],
        cases: list[BenchmarkCase],
    ) -> None:
        required = set(COMMON_CAPABILITIES)
        required.update(ROUTE_CAPABILITIES[path] for path in execution_paths)
        available: set[str] = set()
        if self.backend is not None:
            value = self.backend.capabilities()
            if inspect.isawaitable(value):
                value = await value
            available = {str(item) for item in value}
        if CANONICAL_ALL_CAPABILITY not in available:
            for case in cases:
                applicable_paths = set(case.execution_paths).intersection(
                    execution_paths
                )
                if not applicable_paths:
                    continue
                required.add(f"canonical_real_fixture:{case.fixture_id}")
                required.add(f"canonical_real_oracle:{case.oracle_id}")
                required.update(
                    f"canonical_real_action:{case.script_id}:{path}"
                    for path in applicable_paths
                )
        missing = sorted(required - available)
        if missing:
            raise BenchmarkExecutionError(
                "REAL_BENCHMARK_CAPABILITY_MISSING: "
                + ", ".join(missing)
                + "; no real benchmark action was dispatched"
            )

    async def prepare(self, context: TrialContext) -> RealTrialStart:
        backend = self._require_backend()
        if self._sessions:
            raise BenchmarkExecutionError(
                "parallel or nested real benchmark fixture detected"
            )
        session = await backend.prepare_fixture(context)
        if not isinstance(session, FixtureSession):
            session = FixtureSession(**dict(session))
        self._sessions[context.trial_id] = session
        try:
            identity = await backend.read_fixture_identity(context, session)
        except BaseException as identity_exc:
            # A fixture already exists, so preparation owns rollback if its
            # first independent identity read fails.
            provisional = RealTrialStart(False, False, False, {})
            finish = await self.finalize(context, provisional, identity_exc)
            cleanup_ok = (
                finish.closed_without_save
                and finish.restored
                and finish.save_count == 0
                and finish.hub_sync_count == 0
                and finish.personal_project_access_count == 0
                and finish.parallel_overlap_count == 0
            )
            suffix = "" if cleanup_ok else f"; teardown evidence={finish}"
            raise BenchmarkExecutionError(
                f"real fixture identity read failed during preparation for {context.trial_id}: "
                f"{type(identity_exc).__name__}: {identity_exc}{suffix}"
            ) from identity_exc
        if not isinstance(identity, FixtureIdentity):
            identity = FixtureIdentity(**dict(identity))
        marker_ok, fingerprint_ok, isolated = _verify_identity(
            context, session, identity
        )
        return RealTrialStart(
            fixture_marker_verified=marker_ok,
            fingerprint_verified=fingerprint_ok,
            isolated_unsaved_document=isolated,
            metadata={
                "fixture_document_id": session.fixture_document_id,
                "original_document_id": session.original_document_id,
                "fixture_marker": context.fixture_marker,
                "fixture_fingerprint": session.fixture_fingerprint,
            },
        )

    async def execute(
        self, path: ExecutionPath, context: TrialContext
    ) -> ExecutionObservation:
        enforce_route_lock(path)
        if path != context.execution_path:
            raise BenchmarkExecutionError(
                f"route/context mismatch: requested={path}, context={context.execution_path}"
            )
        backend = self._require_backend()
        session = self._session(context)
        await self._require_active_identity(
            context, session, phase="before route dispatch"
        )
        if path == "safe_harness":
            value = await backend.execute_safe_harness(context, session)
        else:
            value = await backend.execute_native_fast(context, session)
        if not isinstance(value, ExecutionObservation):
            value = ExecutionObservation.model_validate(value)
        return value

    async def observe(
        self, context: TrialContext
    ) -> IndependentEvidence | dict[str, Any]:
        # Correctness evidence comes from a separate read-only backend call;
        # executor output is intentionally absent from this contract.
        backend = self._require_backend()
        session = self._session(context)
        await self._require_active_identity(
            context, session, phase="before oracle observation"
        )
        evidence = await backend.observe_oracle(context, session)
        if isinstance(evidence, IndependentEvidence):
            return evidence
        if not isinstance(evidence, dict):
            raise BenchmarkExecutionError(
                "independent real oracle must return an object"
            )
        return evidence

    async def finalize(
        self,
        context: TrialContext,
        start: RealTrialStart,
        failure: BaseException | None,
    ) -> RealTrialFinish:
        del start, failure
        backend = self._require_backend()
        session = self._session(context)
        errors: list[str] = []
        closed = False
        restored = False
        restoration_ms = 0.0
        audit = ContainmentAudit()
        try:
            closed = bool(await backend.close_fixture_without_save(context, session))
        except BaseException as exc:  # restoration is still mandatory
            errors.append(f"close:{type(exc).__name__}:{exc}")
        restoration_started = time.perf_counter()
        try:
            restored = bool(await backend.restore_original_document(context, session))
        except BaseException as exc:
            errors.append(f"restore:{type(exc).__name__}:{exc}")
        try:
            active_id = await backend.read_active_document_id()
            restored = restored and active_id == session.original_document_id
            if active_id != session.original_document_id:
                errors.append(
                    f"active_document:{active_id!r}!=original:{session.original_document_id!r}"
                )
        except BaseException as exc:
            restored = False
            errors.append(f"restore_readback:{type(exc).__name__}:{exc}")
        restoration_ms = (time.perf_counter() - restoration_started) * 1000
        try:
            open_document_ids = [
                str(value) for value in await backend.list_open_document_ids()
            ]
            if session.fixture_document_id in open_document_ids:
                closed = False
                errors.append(f"fixture_still_open:{session.fixture_document_id}")
            unidentified = [
                value
                for value in open_document_ids
                if value.startswith("unidentified:")
            ]
            if unidentified:
                closed = False
                errors.append(f"unidentified_open_documents:{len(unidentified)}")
            baseline = session.metadata.get("original_open_document_ids")
            if isinstance(baseline, list) and all(
                isinstance(value, str) for value in baseline
            ):
                if sorted(open_document_ids) != sorted(baseline):
                    closed = False
                    errors.append(
                        "open_document_inventory_drift:"
                        f"{sorted(open_document_ids)!r}!={sorted(baseline)!r}"
                    )
        except BaseException as exc:
            closed = False
            errors.append(f"open_documents_readback:{type(exc).__name__}:{exc}")
        try:
            audit = await backend.containment_audit(context, session)
            if not isinstance(audit, ContainmentAudit):
                audit = ContainmentAudit(**dict(audit))
        except BaseException as exc:
            errors.append(f"audit:{type(exc).__name__}:{exc}")
            # Missing audit cannot prove no save/sync. Force containment failure.
            audit = ContainmentAudit(save_count=1)
        finally:
            self._sessions.pop(context.trial_id, None)
        return RealTrialFinish(
            closed_without_save=closed,
            restored=restored,
            save_count=audit.save_count,
            hub_sync_count=audit.hub_sync_count,
            personal_project_access_count=audit.personal_project_access_count,
            parallel_overlap_count=audit.parallel_overlap_count,
            restoration_ms=restoration_ms,
            metadata={**audit.metadata, "errors": errors},
        )

    async def _require_active_identity(
        self,
        context: TrialContext,
        session: FixtureSession,
        *,
        phase: str,
    ) -> None:
        identity = await self._require_backend().read_fixture_identity(context, session)
        marker_ok, fingerprint_ok, isolated = _verify_identity(
            context, session, identity
        )
        violations = []
        if not marker_ok:
            violations.append("marker")
        if not fingerprint_ok:
            violations.append("fingerprint")
        if not isolated:
            violations.append("document isolation")
        if violations:
            raise BenchmarkExecutionError(
                f"real fixture identity drift {phase} for {context.trial_id}: "
                + ", ".join(violations)
            )

    def _session(self, context: TrialContext) -> FixtureSession:
        try:
            return self._sessions[context.trial_id]
        except KeyError as exc:
            raise BenchmarkExecutionError(
                f"no active fixture session for {context.trial_id}"
            ) from exc

    def _require_backend(self) -> RuntimeBenchmarkBackend:
        if self.backend is None:
            raise BenchmarkExecutionError(
                "REAL_BENCHMARK_CAPABILITY_MISSING: runtime real benchmark backend is not installed"
            )
        return self.backend


@dataclass(frozen=True, slots=True)
class _RuntimeRouteExecutor:
    bridge: FusionRuntimeBenchmarkBridge
    path: ExecutionPath

    async def execute(self, context: TrialContext) -> ExecutionObservation:
        return await self.bridge.execute(self.path, context)


def _verify_identity(
    context: TrialContext,
    session: FixtureSession,
    identity: FixtureIdentity,
) -> tuple[bool, bool, bool]:
    marker_ok = (
        session.fixture_marker == context.fixture_marker
        and identity.fixture_marker == context.fixture_marker
    )
    fingerprint_ok = bool(
        session.fixture_fingerprint
        and identity.fixture_fingerprint == session.fixture_fingerprint
    )
    distinct = (
        session.original_document_id is None
        or session.fixture_document_id != session.original_document_id
    )
    isolated = bool(
        session.unsaved
        and identity.unsaved
        and identity.document_id == session.fixture_document_id
        and distinct
    )
    return marker_ok, fingerprint_ok, isolated


_STABLE_DOCUMENT_KEY_FUNCTION = """def _saved_document_key(document):
    if document is None:
        return None
    data_file = document.dataFile
    if data_file is not None and data_file.id:
        return "data:" + str(data_file.id)
    return None

def _stable_document_key(document):
    saved_key = _saved_document_key(document)
    if saved_key is not None:
        return saved_key
    design = adsk.fusion.Design.cast(document.products.itemByProductType("DesignProductType"))
    root = design.rootComponent if design is not None else None
    marker_attribute = (
        root.attributes.itemByName("fusion_agent_benchmark", "trial_marker")
        if root is not None else None
    )
    if marker_attribute is not None and marker_attribute.value:
        return "marker:" + str(marker_attribute.value)
    return None
"""


def _prepare_fixture_script(marker: str, fingerprint: str) -> str:
    return (
        """import adsk.core
import adsk.fusion
import json

_GROUP = "fusion_agent_benchmark"
_MARKER = json.loads(__MARKER_JSON__)
_FINGERPRINT = json.loads(__FINGERPRINT_JSON__)

__STABLE_DOCUMENT_KEY_FUNCTION__

def run(_context: str):
    app = adsk.core.Application.get()
    original = app.activeDocument
    original_document_id = _saved_document_key(original)
    if original is not None and original_document_id is None:
        raise RuntimeError("active document has no stable data identity")
    original_open_document_ids = []
    for index in range(app.documents.count):
        candidate_id = _saved_document_key(app.documents.item(index))
        if candidate_id is None:
            raise RuntimeError("an open document has no stable saved identity")
        original_open_document_ids.append(candidate_id)
    created = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    keep_open = False
    try:
        design = adsk.fusion.Design.cast(created.products.itemByProductType("DesignProductType"))
        if design is None:
            raise RuntimeError("new benchmark document has no Fusion design product")
        root = design.rootComponent
        marker_attribute = root.attributes.add(_GROUP, "trial_marker", _MARKER)
        if marker_attribute is None or marker_attribute.value != _MARKER:
            raise RuntimeError("benchmark marker could not be written")
        fingerprint_attribute = root.attributes.add(_GROUP, "fixture_fingerprint", _FINGERPRINT)
        if fingerprint_attribute is None or fingerprint_attribute.value != _FINGERPRINT:
            raise RuntimeError("benchmark fingerprint could not be written")
        fixture_document_id = _stable_document_key(created)
        if fixture_document_id is None:
            raise RuntimeError("benchmark document has no stable marker identity")
        print(json.dumps({
            "ok": True,
            "original_document_id": original_document_id,
            "original_open_document_ids": original_open_document_ids,
            "fixture_document_id": fixture_document_id,
            "fixture_marker": marker_attribute.value,
            "fixture_fingerprint": fingerprint_attribute.value,
            "unsaved": created.dataFile is None,
        }, sort_keys=True))
        keep_open = True
    finally:
        if not keep_open:
            created.close(False)
""".replace("__MARKER_JSON__", repr(json.dumps(marker)))
        .replace("__FINGERPRINT_JSON__", repr(json.dumps(fingerprint)))
        .replace("__STABLE_DOCUMENT_KEY_FUNCTION__", _STABLE_DOCUMENT_KEY_FUNCTION)
    )


def _active_identity_script() -> str:
    return """import adsk.core
import adsk.fusion
import json

_GROUP = "fusion_agent_benchmark"

__STABLE_DOCUMENT_KEY_FUNCTION__

def run(_context: str):
    app = adsk.core.Application.get()
    document = app.activeDocument
    marker = None
    fingerprint = None
    unsaved = False
    if document is not None:
        design = adsk.fusion.Design.cast(document.products.itemByProductType("DesignProductType"))
        if design is not None:
            marker_attribute = design.rootComponent.attributes.itemByName(_GROUP, "trial_marker")
            fingerprint_attribute = design.rootComponent.attributes.itemByName(_GROUP, "fixture_fingerprint")
            marker = marker_attribute.value if marker_attribute is not None else None
            fingerprint = fingerprint_attribute.value if fingerprint_attribute is not None else None
        unsaved = document.dataFile is None
    print(json.dumps({
        "ok": True,
        "document_id": _stable_document_key(document),
        "fixture_marker": marker,
        "fixture_fingerprint": fingerprint,
        "unsaved": unsaved,
    }, sort_keys=True))
""".replace("__STABLE_DOCUMENT_KEY_FUNCTION__", _STABLE_DOCUMENT_KEY_FUNCTION)


def _close_fixture_script(document_id: str, marker: str, fingerprint: str) -> str:
    return (
        """import adsk.core
import adsk.fusion
import json

_GROUP = "fusion_agent_benchmark"
_DOCUMENT_ID = json.loads(__DOCUMENT_ID_JSON__)
_MARKER = json.loads(__MARKER_JSON__)
_FINGERPRINT = json.loads(__FINGERPRINT_JSON__)

__STABLE_DOCUMENT_KEY_FUNCTION__

def run(_context: str):
    app = adsk.core.Application.get()
    matches = []
    for index in range(app.documents.count):
        candidate = app.documents.item(index)
        design = adsk.fusion.Design.cast(candidate.products.itemByProductType("DesignProductType"))
        marker_attribute = (
            design.rootComponent.attributes.itemByName(_GROUP, "trial_marker")
            if design is not None else None
        )
        fingerprint_attribute = (
            design.rootComponent.attributes.itemByName(_GROUP, "fixture_fingerprint")
            if design is not None else None
        )
        marker_matches = marker_attribute is not None and marker_attribute.value == _MARKER
        fingerprint_matches = (
            fingerprint_attribute is not None and fingerprint_attribute.value == _FINGERPRINT
        )
        if marker_matches or fingerprint_matches:
            matches.append(candidate)
    if len(matches) > 1:
        raise RuntimeError("multiple documents have the unique benchmark marker")
    if not matches:
        print(json.dumps({"ok": True, "found": False, "closed": False}, sort_keys=True))
        return
    target = matches[0]
    design = adsk.fusion.Design.cast(target.products.itemByProductType("DesignProductType"))
    marker_attribute = (
        design.rootComponent.attributes.itemByName(_GROUP, "trial_marker")
        if design is not None else None
    )
    fingerprint_attribute = (
        design.rootComponent.attributes.itemByName(_GROUP, "fixture_fingerprint")
        if design is not None else None
    )
    marker_matches = marker_attribute is not None and marker_attribute.value == _MARKER
    fingerprint_matches = (
        fingerprint_attribute is not None and fingerprint_attribute.value == _FINGERPRINT
    )
    if not marker_matches and not fingerprint_matches:
        raise RuntimeError("refusing to close a document without an exact benchmark identity")
    if marker_matches and _stable_document_key(target) != _DOCUMENT_ID:
        raise RuntimeError("refusing to close a document with mismatched stable identity")
    active = app.activeDocument
    if _stable_document_key(active) != _DOCUMENT_ID:
        target.activate()
    closed = bool(target.close(False))
    print(json.dumps({"ok": True, "found": True, "closed": closed}, sort_keys=True))
""".replace("__DOCUMENT_ID_JSON__", repr(json.dumps(document_id)))
        .replace("__MARKER_JSON__", repr(json.dumps(marker)))
        .replace("__FINGERPRINT_JSON__", repr(json.dumps(fingerprint)))
        .replace("__STABLE_DOCUMENT_KEY_FUNCTION__", _STABLE_DOCUMENT_KEY_FUNCTION)
    )


def _close_fixture_by_marker_script(marker: str, fingerprint: str) -> str:
    return """import adsk.core
import adsk.fusion
import json

_GROUP = "fusion_agent_benchmark"
_MARKER = json.loads(__MARKER_JSON__)
_FINGERPRINT = json.loads(__FINGERPRINT_JSON__)

def run(_context: str):
    app = adsk.core.Application.get()
    matches = []
    for index in range(app.documents.count):
        candidate = app.documents.item(index)
        design = adsk.fusion.Design.cast(candidate.products.itemByProductType("DesignProductType"))
        marker_attribute = (
            design.rootComponent.attributes.itemByName(_GROUP, "trial_marker")
            if design is not None else None
        )
        fingerprint_attribute = (
            design.rootComponent.attributes.itemByName(_GROUP, "fixture_fingerprint")
            if design is not None else None
        )
        marker_matches = marker_attribute is not None and marker_attribute.value == _MARKER
        fingerprint_matches = (
            fingerprint_attribute is not None and fingerprint_attribute.value == _FINGERPRINT
        )
        if marker_matches or fingerprint_matches:
            matches.append(candidate)
    if len(matches) > 1:
        raise RuntimeError("multiple documents have the unique benchmark marker")
    closed = True
    if matches:
        target = matches[0]
        target.activate()
        closed = bool(target.close(False))
    print(json.dumps({"ok": True, "match_count": len(matches), "closed": closed}, sort_keys=True))
""".replace("__MARKER_JSON__", repr(json.dumps(marker))).replace(
        "__FINGERPRINT_JSON__", repr(json.dumps(fingerprint))
    )


def _restore_document_script(document_id: str | None) -> str:
    return """import adsk.core
import adsk.fusion
import json

_DOCUMENT_ID = json.loads(__DOCUMENT_ID_JSON__)

__STABLE_DOCUMENT_KEY_FUNCTION__

def run(_context: str):
    app = adsk.core.Application.get()
    if _DOCUMENT_ID is None:
        print(json.dumps({"ok": True, "restored": app.activeDocument is None}, sort_keys=True))
        return
    matches = []
    for index in range(app.documents.count):
        candidate = app.documents.item(index)
        if _stable_document_key(candidate) == _DOCUMENT_ID:
            matches.append(candidate)
    if len(matches) != 1:
        print(json.dumps({"ok": True, "restored": False, "reason": "original_not_open"}, sort_keys=True))
        return
    target = matches[0]
    active = app.activeDocument
    if _stable_document_key(active) != _DOCUMENT_ID:
        target.activate()
    active = app.activeDocument
    restored = _stable_document_key(active) == _DOCUMENT_ID
    print(json.dumps({"ok": True, "restored": restored}, sort_keys=True))
""".replace("__DOCUMENT_ID_JSON__", repr(json.dumps(document_id))).replace(
        "__STABLE_DOCUMENT_KEY_FUNCTION__", _STABLE_DOCUMENT_KEY_FUNCTION
    )


def _active_document_id_script() -> str:
    return """import adsk.core
import adsk.fusion
import json

__STABLE_DOCUMENT_KEY_FUNCTION__

def run(_context: str):
    document = adsk.core.Application.get().activeDocument
    print(json.dumps({
        "ok": True,
        "document_present": document is not None,
        "document_id": _saved_document_key(document),
    }, sort_keys=True))
""".replace("__STABLE_DOCUMENT_KEY_FUNCTION__", _STABLE_DOCUMENT_KEY_FUNCTION)


def _list_open_documents_script() -> str:
    return """import adsk.core
import adsk.fusion
import json

__STABLE_DOCUMENT_KEY_FUNCTION__

def run(_context: str):
    documents = adsk.core.Application.get().documents
    document_ids = []
    for index in range(documents.count):
        document_id = _stable_document_key(documents.item(index))
        document_ids.append(
            document_id if document_id is not None else "unidentified:" + str(index)
        )
    print(json.dumps({"ok": True, "document_ids": document_ids}, sort_keys=True))
""".replace("__STABLE_DOCUMENT_KEY_FUNCTION__", _STABLE_DOCUMENT_KEY_FUNCTION)


def _decode_script_payload(result: Any, *, operation_id: str) -> dict[str, Any]:
    if isinstance(result, dict):
        ok = bool(result.get("ok", not result.get("isError", False)))
        data = (
            result.get("structuredContent")
            or result.get("structured_content")
            or result.get("data")
            or {}
        )
        content = result.get("content") or []
        error_code = result.get("error_code")
        error_message = result.get("error_message")
    else:
        ok = bool(getattr(result, "ok", False))
        data = getattr(result, "structured_content", None) or getattr(
            result, "data", {}
        )
        content = getattr(result, "content", [])
        error_code = getattr(result, "error_code", None)
        error_message = getattr(result, "error_message", None)
    if not ok:
        raise BenchmarkExecutionError(
            f"real lifecycle call {operation_id} failed: {error_code or 'FUSION_OPERATION_FAILED'}: "
            f"{error_message or 'no error detail'}"
        )

    candidates: list[Any] = [data]
    if isinstance(data, dict) and isinstance(data.get("text"), str):
        candidates.append(data["text"])
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                candidates.append(block["text"])
            elif isinstance(getattr(block, "text", None), str):
                candidates.append(block.text)
    for candidate in candidates:
        payload = _parse_payload_candidate(candidate)
        if payload is not None:
            if payload.get("ok") is not True:
                raise BenchmarkExecutionError(
                    f"real lifecycle call {operation_id} returned a negative acknowledgement"
                )
            return payload
    raise BenchmarkExecutionError(
        f"real lifecycle call {operation_id} returned no JSON object"
    )


def _parse_payload_candidate(candidate: Any) -> dict[str, Any] | None:
    if isinstance(candidate, dict):
        if "ok" in candidate:
            return candidate
        for key in ("result", "message", "text"):
            nested = candidate.get(key)
            if isinstance(nested, str):
                parsed = _parse_payload_candidate(nested)
                if parsed is not None:
                    return parsed
        return None
    if not isinstance(candidate, str):
        return None
    texts = [
        candidate.strip(),
        *(line.strip() for line in reversed(candidate.splitlines())),
    ]
    for text in texts:
        if not text:
            continue
        try:
            loaded = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(loaded, dict):
            parsed = _parse_payload_candidate(loaded)
            if parsed is not None:
                return parsed
    return None


def _required_payload_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise BenchmarkExecutionError(f"real lifecycle payload field {name} is invalid")
    return value


def _optional_payload_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise BenchmarkExecutionError(
            "real lifecycle optional document identity is invalid"
        )
    return value


def _payload_string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BenchmarkExecutionError(f"real lifecycle payload field {name} is invalid")
    return list(value)
