"""Capability-bound authority for typed CAD operations.

Caller-supplied paths request access; they never confer it.  This module is
deliberately independent of MCP transport so one immutable policy and ledger
can be created at process startup and injected into every executor.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import asdict, dataclass, is_dataclass, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Literal, cast

from cad_spec.v2 import CadSpecV2, ExportOperation, ImportOperation, OperationSpec


Direction = Literal["import", "export", "cad"]
CapabilityState = Literal[
    "issued", "claimed", "consumed", "unknown", "revoked", "expired"
]
_TERMINAL_STATES = {"consumed", "unknown", "revoked", "expired"}
_ROOT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SCHEMA_VERSION = "fusion_agent.authority_policy.v1"
_FORMAT_EXTENSIONS: dict[str, frozenset[str]] = {
    "step": frozenset({".step", ".stp"}),
    "stp": frozenset({".step", ".stp"}),
    "stl": frozenset({".stl"}),
    "iges": frozenset({".iges", ".igs"}),
    "igs": frozenset({".iges", ".igs"}),
    "sat": frozenset({".sat"}),
    "f3d": frozenset({".f3d"}),
    "png": frozenset({".png"}),
}
_IMPORT_FORMATS = frozenset({"step", "stp", "iges", "igs", "sat", "f3d"})
_EXPORT_FORMATS = frozenset({"step", "stp", "stl", "iges", "igs", "f3d", "png"})


class AuthorityDeniedError(ValueError):
    """A request did not carry independently configured authority."""

    error_code = "AUTHORITY_DENIED"


class HostOutputDisabledError(AuthorityDeniedError):
    """Real Fusion export/capture is structurally disabled for this release."""

    error_code = "HOST_OUTPUT_DISABLED"


REAL_HOST_OUTPUT_POLICY = "deny_io"
REAL_HOST_OUTPUT_DENIED_MESSAGE = (
    "real Fusion export and capture are disabled by deny_io in 0.4.1"
)


@dataclass(frozen=True, slots=True)
class AuthorityRoot:
    id: str
    canonical_path: str
    formats: frozenset[str]
    default: bool = False


@dataclass(frozen=True, slots=True)
class AuthorityPolicy:
    """Immutable startup snapshot of approved host I/O roots."""

    schema_version: str
    import_roots: tuple[AuthorityRoot, ...]
    export_roots: tuple[AuthorityRoot, ...]
    allow_overwrite: bool = False
    capability_ttl_seconds: int = 1800
    source_path: str | None = None
    digest: str = ""

    @property
    def io_enabled(self) -> bool:
        """Report only executable real host I/O for this release.

        Export roots remain parsed for one compatibility cycle, but `deny_io`
        means they cannot enable a real operation in 0.4.1.
        """

        return bool(self.import_roots)

    @property
    def root_ids(self) -> dict[str, tuple[str, ...]]:
        return {"import": tuple(root.id for root in self.import_roots)}

    def safe_summary(self) -> dict[str, object]:
        """Return the only policy fields suitable for public diagnostics."""

        return {
            "digest": self.digest,
            "io_enabled": bool(self.import_roots),
            "import_enabled": bool(self.import_roots),
            "output_enabled": False,
            "output_policy": REAL_HOST_OUTPUT_POLICY,
            "overwrite_supported": False,
            "root_ids": {
                "import": [root.id for root in self.import_roots],
            },
        }

    @classmethod
    def deny_all(cls) -> "AuthorityPolicy":
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "import_roots": [],
            "export_roots": [],
            "allow_overwrite": False,
            "capability_ttl_seconds": 1800,
        }
        return cls(
            schema_version=_SCHEMA_VERSION,
            import_roots=(),
            export_roots=(),
            digest=_json_digest(payload),
        )

    @classmethod
    def from_environment(
        cls, environment: dict[str, str] | None = None
    ) -> "AuthorityPolicy":
        """Load once from the only supported authority-policy environment key."""

        values = os.environ if environment is None else environment
        raw_path = str(values.get("FUSION_AGENT_AUTHORITY_POLICY_PATH") or "").strip()
        if not raw_path:
            return cls.deny_all()
        return cls.load(raw_path)

    @classmethod
    def load(cls, path: Path | str) -> "AuthorityPolicy":
        source = Path(path)
        try:
            canonical_source = source.resolve(strict=True)
        except OSError as exc:
            raise AuthorityDeniedError("authority policy file is unavailable") from exc
        if not canonical_source.is_file():
            raise AuthorityDeniedError("authority policy path must name a file")
        if canonical_source.stat().st_size > 1024 * 1024:
            raise AuthorityDeniedError("authority policy file is too large")
        try:
            payload = json.loads(canonical_source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AuthorityDeniedError(
                "authority policy is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise AuthorityDeniedError("authority policy must be a JSON object")
        allowed_keys = {
            "schema_version",
            "import_roots",
            "export_roots",
            "allow_overwrite",
            "capability_ttl_seconds",
        }
        unknown = set(payload) - allowed_keys
        if unknown:
            raise AuthorityDeniedError(
                "authority policy has unknown fields: " + ", ".join(sorted(unknown))
            )
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise AuthorityDeniedError(
                f"authority policy schema_version must be {_SCHEMA_VERSION!r}"
            )
        allow_overwrite = payload.get("allow_overwrite", False)
        if not isinstance(allow_overwrite, bool):
            raise AuthorityDeniedError("allow_overwrite must be boolean")
        ttl = payload.get("capability_ttl_seconds", 1800)
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 86400:
            raise AuthorityDeniedError(
                "capability_ttl_seconds must be an integer from 1 through 86400"
            )
        import_roots = _load_roots(
            payload.get("import_roots", []), "import", _IMPORT_FORMATS
        )
        export_roots = _load_roots(
            payload.get("export_roots", []), "export", _EXPORT_FORMATS
        )
        all_ids = [root.id for root in (*import_roots, *export_roots)]
        if len(all_ids) != len(set(all_ids)):
            raise AuthorityDeniedError("authority root ids must be globally unique")
        normalized = {
            "schema_version": _SCHEMA_VERSION,
            "import_roots": [_root_payload(root) for root in import_roots],
            "export_roots": [_root_payload(root) for root in export_roots],
            "allow_overwrite": allow_overwrite,
            "capability_ttl_seconds": ttl,
        }
        return cls(
            schema_version=_SCHEMA_VERSION,
            import_roots=import_roots,
            export_roots=export_roots,
            allow_overwrite=allow_overwrite,
            capability_ttl_seconds=ttl,
            source_path=str(canonical_source),
            digest=_json_digest(normalized),
        )


@dataclass(frozen=True, slots=True)
class HostPathBinding:
    direction: Direction
    root_id: str
    canonical_root: str
    canonical_path: str
    relative_path: str
    format: str
    overwrite: bool
    existed_at_issue: bool
    resource_fingerprint: str


@dataclass(frozen=True, slots=True)
class CadTargetBinding:
    reference_kind: str
    requested_ref: str
    document_identity: str
    entity_identity: str
    fingerprint: str
    producer_operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class BindingProof:
    algorithm: str
    digest: str


@dataclass(frozen=True, slots=True)
class OperationCapability:
    capability_id: str
    direction: Direction
    root_id: str
    canonical_path: str
    spec_digest: str
    operation_digest: str
    session_id: str
    provider: str
    overwrite: bool
    issued_at: float
    expires_at: float
    binding_digest: str


@dataclass(frozen=True, slots=True)
class LegacyOutputOperation:
    """One deprecated CadSpec v1 host output bound by the shared authority layer."""

    id: str
    kind: Literal["legacy.export", "legacy.capture"]
    path: str
    format: str
    target_identity: str
    overwrite: bool = False


@dataclass(frozen=True, slots=True)
class BoundOperation:
    operation: OperationSpec | LegacyOutputOperation
    spec_digest: str
    operation_digest: str
    session_id: str
    provider: str
    host_path: HostPathBinding | None = None
    target_bindings: tuple[CadTargetBinding, ...] = ()
    capability: OperationCapability | None = None
    proof: BindingProof | None = None


@dataclass(frozen=True, slots=True)
class PreparedOperationGraph:
    spec_digest: str
    session_id: str
    provider: str
    operations: tuple[BoundOperation, ...]


class CapabilityLedger:
    """Single-use capability ledger with optional durable claim files."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else None
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def reconcile_startup(self) -> int:
        """Make interrupted durable claims terminal before accepting work.

        A process can stop after the exclusive claim marker is created or
        after the record reaches ``claimed`` but before a terminal outcome is
        persisted.  Neither state may be retried after restart: provider
        dispatch might already have happened.  Startup therefore persists
        ``unknown`` for both crash windows while leaving unclaimed grants and
        terminal records unchanged.
        """

        if self.root is None:
            return 0
        with self._lock:
            try:
                record_paths = tuple(sorted(self.root.glob("*.json")))
            except OSError as exc:
                raise AuthorityDeniedError(
                    "capability ledger reconciliation is unavailable"
                ) from exc

            reconciled = 0
            for record_path in record_paths:
                if record_path.is_symlink() or not record_path.is_file():
                    continue
                try:
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    # An unreadable record is already unclaimable through
                    # _disk_record.  Do not trust it enough to derive a path.
                    continue
                if not isinstance(record, dict):
                    continue
                capability_id = record.get("capability_id")
                if (
                    not isinstance(capability_id, str)
                    or record_path.name != f"{capability_id}.json"
                ):
                    # Never derive a write target from persisted record data.
                    continue
                state = record.get("state")
                interrupted = state == "claimed" or (
                    state == "issued"
                    and os.path.lexists(self._claim_path(capability_id))
                )
                if not interrupted:
                    continue
                self._write_disk_state_at(record_path, record, "unknown")
                reconciled += 1
            return reconciled

    def issue(self, capability: OperationCapability) -> None:
        record = {**asdict(capability), "state": "issued"}
        with self._lock:
            if self.root is None:
                if capability.capability_id in self._records:
                    raise AuthorityDeniedError("capability id collision")
                self._records[capability.capability_id] = record
                return
            self.root.mkdir(parents=True, exist_ok=True)
            record_path = self._record_path(capability.capability_id)
            try:
                descriptor = os.open(
                    record_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
            except FileExistsError as exc:
                raise AuthorityDeniedError("capability id collision") from exc
            try:
                os.write(
                    descriptor,
                    json.dumps(record, sort_keys=True).encode("utf-8"),
                )
            finally:
                os.close(descriptor)

    def claim(self, capability: OperationCapability, *, now: float) -> None:
        with self._lock:
            if self.root is None:
                record = self._memory_record(capability.capability_id)
                self._validate_record(record, capability)
                if record["state"] != "issued":
                    raise AuthorityDeniedError(
                        f"capability replay denied from state {record['state']}"
                    )
                if now >= float(record["expires_at"]):
                    record["state"] = "expired"
                    raise AuthorityDeniedError("capability expired")
                record["state"] = "claimed"
                return
            self.root.mkdir(parents=True, exist_ok=True)
            claim_path = self._claim_path(capability.capability_id)
            try:
                descriptor = os.open(
                    claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
            except FileExistsError as exc:
                raise AuthorityDeniedError("capability replay denied") from exc
            os.close(descriptor)
            record = self._disk_record(capability.capability_id)
            self._validate_record(record, capability)
            if record["state"] != "issued":
                raise AuthorityDeniedError(
                    f"capability replay denied from state {record['state']}"
                )
            if now >= float(record["expires_at"]):
                self._write_disk_state(record, "expired")
                raise AuthorityDeniedError("capability expired")
            self._write_disk_state(record, "claimed")

    def revoke_active(self, capability_id: str) -> None:
        """Invalidate an issued/claimed record by its broker-owned identifier."""

        with self._lock:
            record = (
                self._memory_record(capability_id)
                if self.root is None
                else self._disk_record(capability_id)
            )
            if record["state"] not in {"issued", "claimed"}:
                return
            if self.root is None:
                record["state"] = "revoked"
            else:
                self._write_disk_state(record, "revoked")

    def transition(
        self, capability: OperationCapability, state: CapabilityState
    ) -> None:
        if state not in _TERMINAL_STATES:
            raise ValueError("capability terminal transition is required")
        with self._lock:
            if self.root is None:
                record = self._memory_record(capability.capability_id)
                self._validate_record(record, capability)
                allowed_from = {"claimed"}
                if state == "revoked":
                    allowed_from.add("issued")
                if record["state"] not in allowed_from:
                    raise AuthorityDeniedError(
                        f"capability transition denied from state {record['state']}"
                    )
                record["state"] = state
                return
            record = self._disk_record(capability.capability_id)
            self._validate_record(record, capability)
            allowed_from = {"claimed"}
            if state == "revoked":
                allowed_from.add("issued")
            if record["state"] not in allowed_from:
                raise AuthorityDeniedError(
                    f"capability transition denied from state {record['state']}"
                )
            self._write_disk_state(record, state)

    def state(self, capability_id: str) -> CapabilityState:
        with self._lock:
            record = (
                self._memory_record(capability_id)
                if self.root is None
                else self._disk_record(capability_id)
            )
            return str(record["state"])  # type: ignore[return-value]

    def _record_path(self, capability_id: str) -> Path:
        if self.root is None:
            raise RuntimeError("ledger is memory-only")
        return self.root / f"{capability_id}.json"

    def _claim_path(self, capability_id: str) -> Path:
        if self.root is None:
            raise RuntimeError("ledger is memory-only")
        return self.root / f"{capability_id}.claim"

    def _memory_record(self, capability_id: str) -> dict[str, Any]:
        try:
            return self._records[capability_id]
        except KeyError as exc:
            raise AuthorityDeniedError("capability is unknown") from exc

    def _disk_record(self, capability_id: str) -> dict[str, Any]:
        try:
            value = json.loads(
                self._record_path(capability_id).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AuthorityDeniedError(
                "capability ledger record is unavailable"
            ) from exc
        if not isinstance(value, dict):
            raise AuthorityDeniedError("capability ledger record is invalid")
        return value

    def _write_disk_state(self, record: dict[str, Any], state: CapabilityState) -> None:
        path = self._record_path(str(record["capability_id"]))
        self._write_disk_state_at(path, record, state)

    @staticmethod
    def _write_disk_state_at(
        path: Path, record: dict[str, Any], state: CapabilityState
    ) -> None:
        updated = {**record, "state": state}
        temporary = path.with_suffix(f".{os.getpid()}.{secrets.token_hex(4)}.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(
                descriptor,
                json.dumps(updated, sort_keys=True).encode("utf-8"),
            )
        finally:
            os.close(descriptor)
        os.replace(temporary, path)

    @staticmethod
    def _validate_record(
        record: dict[str, Any], capability: OperationCapability
    ) -> None:
        for key, expected in asdict(capability).items():
            if record.get(key) != expected:
                raise AuthorityDeniedError(
                    "capability does not match its ledger record"
                )


class AuthorityBroker:
    """Compile resource requests into bound, single-use operation grants."""

    def __init__(
        self,
        policy: AuthorityPolicy,
        *,
        ledger: CapabilityLedger | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.policy = policy
        self.ledger = ledger or CapabilityLedger()
        self._clock = clock

    @classmethod
    def from_environment(
        cls,
        *,
        ledger: CapabilityLedger | None = None,
        environment: dict[str, str] | None = None,
    ) -> "AuthorityBroker":
        return cls(
            AuthorityPolicy.from_environment(environment),
            ledger=ledger,
        )

    def prepare_graph(
        self,
        spec: CadSpecV2,
        *,
        session_id: str,
        provider: str,
        target_bindings_by_operation: dict[str, tuple[CadTargetBinding, ...]]
        | None = None,
    ) -> PreparedOperationGraph:
        if not session_id.strip():
            raise AuthorityDeniedError("session id is required for operation authority")
        if not provider.strip():
            raise AuthorityDeniedError("provider is required for operation authority")
        provided_targets = target_bindings_by_operation or {}
        producer_map = cad_graph_target_producers(spec)
        operation_ids = {operation.id for operation in spec.operations}
        unknown_target_operations = set(provided_targets) - operation_ids
        if unknown_target_operations:
            raise AuthorityDeniedError(
                "CAD target bindings reference unknown operations: "
                + ", ".join(sorted(unknown_target_operations))
            )
        spec_digest = _model_digest(spec)
        pending: list[BoundOperation] = []
        for operation in spec.operations:
            operation_digest = _model_digest(operation)
            host_path = self._bind_host_path(operation)
            target_bindings = _validated_target_bindings(
                operation,
                provided_targets.get(operation.id, ()),
                expected_producers=producer_map.get(operation.id, {}),
            )
            proof = None
            if host_path is not None or target_bindings:
                proof = BindingProof(
                    algorithm="sha256",
                    digest=_binding_digest(
                        host_path,
                        target_bindings,
                        spec_digest=spec_digest,
                        operation_digest=operation_digest,
                        session_id=session_id,
                        provider=provider,
                    ),
                )
            pending.append(
                BoundOperation(
                    operation=operation,
                    spec_digest=spec_digest,
                    operation_digest=operation_digest,
                    session_id=session_id,
                    provider=provider,
                    host_path=host_path,
                    target_bindings=target_bindings,
                    proof=proof,
                )
            )

        issued_at = self._clock()
        bound_operations: list[BoundOperation] = []
        for bound in pending:
            if bound.proof is None:
                bound_operations.append(bound)
                continue
            direction: Direction = (
                bound.host_path.direction if bound.host_path is not None else "cad"
            )
            capability = OperationCapability(
                capability_id=secrets.token_urlsafe(24),
                direction=direction,
                root_id=bound.host_path.root_id if bound.host_path is not None else "",
                canonical_path=(
                    bound.host_path.canonical_path
                    if bound.host_path is not None
                    else ""
                ),
                spec_digest=bound.spec_digest,
                operation_digest=bound.operation_digest,
                session_id=session_id,
                provider=provider,
                overwrite=(
                    bound.host_path.overwrite if bound.host_path is not None else False
                ),
                issued_at=issued_at,
                expires_at=issued_at + self.policy.capability_ttl_seconds,
                binding_digest=bound.proof.digest,
            )
            self.ledger.issue(capability)
            bound_operations.append(replace(bound, capability=capability))
        return PreparedOperationGraph(
            spec_digest=spec_digest,
            session_id=session_id,
            provider=provider,
            operations=tuple(bound_operations),
        )

    def validate_host_requests(self, spec: CadSpecV2) -> None:
        """Validate every host path and produced reference before provider reads."""

        cad_graph_target_producers(spec)
        for operation in spec.operations:
            self._bind_host_path(operation)

    def prepare_operation(
        self,
        spec: CadSpecV2,
        operation: OperationSpec,
        *,
        session_id: str,
        provider: str,
        target_bindings: tuple[CadTargetBinding, ...] = (),
    ) -> BoundOperation:
        """Issue one just-in-time grant while retaining the complete graph digest.

        The full graph is revalidated before each issuance.  This lets a target
        produced by an earlier operation be materialized and resolved only
        after its producer succeeds, without reducing the capability to a
        caller-selected one-operation spec.
        """

        if not session_id.strip():
            raise AuthorityDeniedError("session id is required for operation authority")
        if not provider.strip():
            raise AuthorityDeniedError("provider is required for operation authority")
        operations_by_id = {item.id: item for item in spec.operations}
        canonical_operation = operations_by_id.get(operation.id)
        if canonical_operation is None or canonical_operation != operation:
            raise AuthorityDeniedError(
                "just-in-time capability operation is outside the validated graph"
            )
        producer_map = cad_graph_target_producers(spec)
        spec_digest = _model_digest(spec)
        operation_digest = _model_digest(operation)
        host_path = self._bind_host_path(operation)
        validated_targets = _validated_target_bindings(
            operation,
            target_bindings,
            expected_producers=producer_map.get(operation.id, {}),
        )
        proof = None
        if host_path is not None or validated_targets:
            proof = BindingProof(
                algorithm="sha256",
                digest=_binding_digest(
                    host_path,
                    validated_targets,
                    spec_digest=spec_digest,
                    operation_digest=operation_digest,
                    session_id=session_id,
                    provider=provider,
                ),
            )
        bound = BoundOperation(
            operation=operation,
            spec_digest=spec_digest,
            operation_digest=operation_digest,
            session_id=session_id,
            provider=provider,
            host_path=host_path,
            target_bindings=validated_targets,
            proof=proof,
        )
        if proof is None:
            return bound
        issued_at = self._clock()
        capability = OperationCapability(
            capability_id=secrets.token_urlsafe(24),
            direction=(host_path.direction if host_path is not None else "cad"),
            root_id=host_path.root_id if host_path is not None else "",
            canonical_path=host_path.canonical_path if host_path is not None else "",
            spec_digest=spec_digest,
            operation_digest=operation_digest,
            session_id=session_id,
            provider=provider,
            overwrite=host_path.overwrite if host_path is not None else False,
            issued_at=issued_at,
            expires_at=issued_at + self.policy.capability_ttl_seconds,
            binding_digest=proof.digest,
        )
        self.ledger.issue(capability)
        return replace(bound, capability=capability)

    def validate_legacy_output_requests(
        self, operations: tuple[LegacyOutputOperation, ...]
    ) -> None:
        """Validate deprecated output paths before any Fusion identity read."""

        if len({operation.id for operation in operations}) != len(operations):
            raise AuthorityDeniedError("legacy output operation ids must be unique")
        for operation in operations:
            if operation.kind == "legacy.capture" and operation.format != "png":
                raise AuthorityDeniedError("legacy capture format must be png")
            _resolve_host_path(
                self.policy,
                direction="export",
                root_id=None,
                requested_path=operation.path,
                format_name=operation.format,
                overwrite=operation.overwrite,
                explicit_ref=False,
            )

    def prepare_legacy_output_graph(
        self,
        operations: tuple[LegacyOutputOperation, ...],
        *,
        session_id: str,
        provider: str,
        target_bindings_by_operation: dict[str, tuple[CadTargetBinding, ...]]
        | None = None,
    ) -> PreparedOperationGraph:
        """Bind all deprecated v1 outputs before the first provider dispatch."""

        if not session_id.strip():
            raise AuthorityDeniedError("session id is required for operation authority")
        if not provider.strip():
            raise AuthorityDeniedError("provider is required for operation authority")
        self.validate_legacy_output_requests(operations)
        provided_targets = target_bindings_by_operation or {}
        operation_ids = {operation.id for operation in operations}
        if set(provided_targets) != operation_ids:
            raise AuthorityDeniedError(
                "legacy host outputs require one live target proof per operation"
            )
        spec_digest = _json_digest(
            {
                "schema_version": "fusion_agent.legacy_host_io.v1",
                "operations": [asdict(operation) for operation in operations],
            }
        )
        pending: list[BoundOperation] = []
        for operation in operations:
            if operation.kind == "legacy.capture" and operation.format != "png":
                raise AuthorityDeniedError("legacy capture format must be png")
            operation_digest = _model_digest(operation)
            host_path = _resolve_host_path(
                self.policy,
                direction="export",
                root_id=None,
                requested_path=operation.path,
                format_name=operation.format,
                overwrite=operation.overwrite,
                explicit_ref=False,
            )
            target_bindings = _validated_legacy_target_bindings(
                operation,
                provided_targets.get(operation.id, ()),
            )
            proof = BindingProof(
                algorithm="sha256",
                digest=_binding_digest(
                    host_path,
                    target_bindings,
                    spec_digest=spec_digest,
                    operation_digest=operation_digest,
                    session_id=session_id,
                    provider=provider,
                ),
            )
            pending.append(
                BoundOperation(
                    operation=operation,
                    spec_digest=spec_digest,
                    operation_digest=operation_digest,
                    session_id=session_id,
                    provider=provider,
                    host_path=host_path,
                    target_bindings=target_bindings,
                    proof=proof,
                )
            )

        issued_at = self._clock()
        bound_operations: list[BoundOperation] = []
        try:
            for bound in pending:
                assert bound.host_path is not None and bound.proof is not None
                capability = OperationCapability(
                    capability_id=secrets.token_urlsafe(24),
                    direction="export",
                    root_id=bound.host_path.root_id,
                    canonical_path=bound.host_path.canonical_path,
                    spec_digest=bound.spec_digest,
                    operation_digest=bound.operation_digest,
                    session_id=session_id,
                    provider=provider,
                    overwrite=bound.host_path.overwrite,
                    issued_at=issued_at,
                    expires_at=issued_at + self.policy.capability_ttl_seconds,
                    binding_digest=bound.proof.digest,
                )
                self.ledger.issue(capability)
                bound_operations.append(replace(bound, capability=capability))
        except Exception:
            for issued in bound_operations:
                self.revoke(issued)
            raise
        return PreparedOperationGraph(
            spec_digest=spec_digest,
            session_id=session_id,
            provider=provider,
            operations=tuple(bound_operations),
        )

    def claim(self, bound: BoundOperation) -> None:
        try:
            self.validate(bound)
        except Exception:
            try:
                if bound.capability is not None:
                    self.ledger.revoke_active(bound.capability.capability_id)
            except AuthorityDeniedError:
                pass
            raise
        if bound.capability is None:
            raise AuthorityDeniedError("operation has no capability to claim")
        self.ledger.claim(bound.capability, now=self._clock())

    def complete(self, bound: BoundOperation, *, outcome: CapabilityState) -> None:
        if bound.capability is None:
            return
        self.ledger.transition(bound.capability, outcome)

    def revoke(self, bound: BoundOperation) -> None:
        if bound.capability is None:
            return
        self.ledger.revoke_active(bound.capability.capability_id)

    def fail(self, bound: BoundOperation, *, outcome_unknown: bool) -> None:
        """Close a failed grant without ever making it replayable."""

        if bound.capability is None:
            return
        state = self.ledger.state(bound.capability.capability_id)
        if state == "issued":
            self.ledger.transition(bound.capability, "revoked")
        elif state == "claimed":
            self.ledger.transition(
                bound.capability, "unknown" if outcome_unknown else "revoked"
            )

    def validate(self, bound: BoundOperation) -> None:
        operation = bound.operation
        requires_authority = isinstance(operation, LegacyOutputOperation) or (
            not isinstance(operation, LegacyOutputOperation)
            and _operation_requires_authority(operation)
        )
        if not requires_authority:
            if (
                bound.host_path is not None
                or bound.capability is not None
                or bound.proof is not None
                or bound.target_bindings
            ):
                raise AuthorityDeniedError(
                    "read-only operation carries mutation authority"
                )
            return
        if bound.capability is None or bound.proof is None:
            raise AuthorityDeniedError(
                "mutating operation requires a complete bound capability"
            )
        requires_host_path = isinstance(
            operation, (ImportOperation, ExportOperation, LegacyOutputOperation)
        )
        if requires_host_path and bound.host_path is None:
            raise AuthorityDeniedError("host I/O requires a bound host path")
        if not requires_host_path and bound.host_path is not None:
            raise AuthorityDeniedError("CAD-only operation carries host authority")
        if _model_digest(operation) != bound.operation_digest:
            raise AuthorityDeniedError("operation changed after authority was issued")
        if isinstance(operation, LegacyOutputOperation):
            _validated_legacy_target_bindings(operation, bound.target_bindings)
        else:
            _validated_target_bindings(operation, bound.target_bindings)
        expected_binding = _binding_digest(
            bound.host_path,
            bound.target_bindings,
            spec_digest=bound.spec_digest,
            operation_digest=bound.operation_digest,
            session_id=bound.session_id,
            provider=bound.provider,
        )
        if bound.proof.algorithm != "sha256" or bound.proof.digest != expected_binding:
            raise AuthorityDeniedError("operation binding proof does not match")
        capability = bound.capability
        expected_capability = (
            bound.host_path.direction if bound.host_path is not None else "cad",
            bound.host_path.root_id if bound.host_path is not None else "",
            bound.host_path.canonical_path if bound.host_path is not None else "",
            bound.spec_digest,
            bound.operation_digest,
            bound.session_id,
            bound.provider,
            bound.host_path.overwrite if bound.host_path is not None else False,
            expected_binding,
        )
        actual_capability = (
            capability.direction,
            capability.root_id,
            capability.canonical_path,
            capability.spec_digest,
            capability.operation_digest,
            capability.session_id,
            capability.provider,
            capability.overwrite,
            capability.binding_digest,
        )
        if actual_capability != expected_capability:
            raise AuthorityDeniedError("capability is not bound to this operation")
        if bound.host_path is not None:
            revalidate_host_path(bound.host_path)

    def _bind_host_path(self, operation: OperationSpec) -> HostPathBinding | None:
        if isinstance(operation, ImportOperation):
            return _resolve_host_path(
                self.policy,
                direction="import",
                root_id=(operation.file_ref.root_id if operation.file_ref else None),
                requested_path=(
                    operation.file_ref.relative_path
                    if operation.file_ref
                    else operation.path or ""
                ),
                format_name=operation.format,
                overwrite=False,
                explicit_ref=operation.file_ref is not None,
            )
        if isinstance(operation, ExportOperation):
            return _resolve_host_path(
                self.policy,
                direction="export",
                root_id=(operation.file_ref.root_id if operation.file_ref else None),
                requested_path=(
                    operation.file_ref.relative_path
                    if operation.file_ref
                    else operation.path or ""
                ),
                format_name=operation.format,
                overwrite=operation.overwrite,
                explicit_ref=operation.file_ref is not None,
            )
        return None


def revalidate_host_path(binding: HostPathBinding) -> None:
    """Repeat canonicalization and resource identity checks at the sink."""

    root = Path(binding.canonical_root)
    try:
        canonical_root = root.resolve(strict=True)
    except OSError as exc:
        raise AuthorityDeniedError("approved root changed after authorization") from exc
    if str(canonical_root) != binding.canonical_root:
        raise AuthorityDeniedError("approved root canonical identity changed")
    target = Path(binding.canonical_path)
    canonical_target, existed = _canonical_target(target, binding.direction)
    if str(canonical_target) != binding.canonical_path:
        raise AuthorityDeniedError("authorized path changed before dispatch")
    _require_contained(canonical_root, canonical_target)
    if existed != binding.existed_at_issue:
        raise AuthorityDeniedError("authorized path existence changed before dispatch")
    current_fingerprint = _resource_fingerprint(
        canonical_target, direction=binding.direction, existed=existed
    )
    if current_fingerprint != binding.resource_fingerprint:
        raise AuthorityDeniedError("authorized host resource changed before dispatch")


def _load_roots(
    value: object,
    direction: Direction,
    allowed_formats: frozenset[str],
) -> tuple[AuthorityRoot, ...]:
    if not isinstance(value, list):
        raise AuthorityDeniedError(f"{direction}_roots must be an array")
    roots: list[AuthorityRoot] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise AuthorityDeniedError(f"{direction}_roots[{index}] must be an object")
        unknown = set(item) - {"id", "path", "formats", "default"}
        if unknown:
            raise AuthorityDeniedError(
                f"{direction}_roots[{index}] has unknown fields: "
                + ", ".join(sorted(unknown))
            )
        root_id = item.get("id")
        if not isinstance(root_id, str) or not _ROOT_ID_RE.fullmatch(root_id):
            raise AuthorityDeniedError(
                f"{direction}_roots[{index}].id is not a valid root id"
            )
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise AuthorityDeniedError(
                f"{direction}_roots[{index}].path must be a non-empty string"
            )
        _reject_device_path(raw_path)
        path = Path(raw_path)
        if not path.is_absolute():
            raise AuthorityDeniedError(
                f"{direction}_roots[{index}].path must be absolute"
            )
        try:
            canonical = path.resolve(strict=True)
        except OSError as exc:
            raise AuthorityDeniedError(
                f"{direction}_roots[{index}].path is unavailable"
            ) from exc
        if not canonical.is_dir():
            raise AuthorityDeniedError(
                f"{direction}_roots[{index}].path must be a directory"
            )
        raw_formats = item.get("formats")
        if not isinstance(raw_formats, list) or not raw_formats:
            raise AuthorityDeniedError(
                f"{direction}_roots[{index}].formats must be a non-empty array"
            )
        if not all(isinstance(fmt, str) for fmt in raw_formats):
            raise AuthorityDeniedError(
                f"{direction}_roots[{index}].formats must contain only strings"
            )
        formats = frozenset(str(fmt).strip().lower() for fmt in raw_formats)
        unsupported = formats - allowed_formats
        if unsupported:
            raise AuthorityDeniedError(
                f"{direction}_roots[{index}] has unsupported formats: "
                + ", ".join(sorted(unsupported))
            )
        default = item.get("default", False)
        if not isinstance(default, bool):
            raise AuthorityDeniedError(
                f"{direction}_roots[{index}].default must be boolean"
            )
        roots.append(
            AuthorityRoot(
                id=root_id,
                canonical_path=str(canonical),
                formats=formats,
                default=default,
            )
        )
    if len({root.id for root in roots}) != len(roots):
        raise AuthorityDeniedError(f"{direction} root ids must be unique")
    if sum(root.default for root in roots) > 1:
        raise AuthorityDeniedError(f"{direction} roots may have at most one default")
    return tuple(roots)


def _resolve_host_path(
    policy: AuthorityPolicy,
    *,
    direction: Direction,
    root_id: str | None,
    requested_path: str,
    format_name: str,
    overwrite: bool,
    explicit_ref: bool,
) -> HostPathBinding:
    roots = policy.import_roots if direction == "import" else policy.export_roots
    if not roots:
        raise AuthorityDeniedError(
            f"real host {direction} is disabled by authority policy"
        )
    _validate_requested_text(requested_path)
    if explicit_ref:
        if root_id is None:
            raise AuthorityDeniedError("HostFileRef requires root_id")
        matches = [root for root in roots if root.id == root_id]
        if len(matches) != 1:
            raise AuthorityDeniedError(f"unknown approved {direction} root id")
        root = matches[0]
        relative_path = _normalize_relative_path(requested_path)
        candidate = Path(root.canonical_path) / relative_path
    else:
        if _is_unc_path(requested_path) and not _unc_matches_approved_root(
            requested_path, roots
        ):
            raise AuthorityDeniedError(
                "legacy UNC path does not match an approved UNC root"
            )
        candidate_path = Path(requested_path)
        if candidate_path.is_absolute():
            _reject_device_path(requested_path)
            canonical_candidate, _ = _canonical_target(candidate_path, direction)
            containing = [
                item
                for item in roots
                if _is_contained(Path(item.canonical_path), canonical_candidate)
            ]
            if len(containing) != 1:
                raise AuthorityDeniedError(
                    f"legacy absolute path must match exactly one approved {direction} root"
                )
            root = containing[0]
            candidate = candidate_path
        else:
            relative_path = _normalize_relative_path(requested_path)
            defaults = [item for item in roots if item.default]
            if not defaults and len(roots) == 1:
                defaults = [roots[0]]
            if len(defaults) != 1:
                raise AuthorityDeniedError(
                    f"legacy relative path requires one default approved {direction} root"
                )
            root = defaults[0]
            candidate = Path(root.canonical_path) / relative_path
    if format_name not in root.formats:
        raise AuthorityDeniedError(
            f"format {format_name!r} is not allowed by approved root {root.id!r}"
        )
    canonical, existed = _canonical_target(candidate, direction)
    canonical_root = Path(root.canonical_path)
    _require_contained(canonical_root, canonical)
    _require_windows_case_exact(
        canonical_root,
        candidate,
        final_exists=existed,
    )
    suffix = canonical.suffix.lower()
    if suffix not in _FORMAT_EXTENSIONS.get(format_name, frozenset()):
        raise AuthorityDeniedError(
            f"path extension {suffix!r} does not match format {format_name!r}"
        )
    if direction == "import":
        if not existed or not canonical.is_file():
            raise AuthorityDeniedError("approved import path must be an existing file")
    else:
        if existed and not canonical.is_file():
            raise AuthorityDeniedError(
                "approved export path must be a file destination"
            )
        if existed and not (policy.allow_overwrite and overwrite):
            raise AuthorityDeniedError(
                "export overwrite requires both policy and operation opt-in"
            )
    return HostPathBinding(
        direction=direction,
        root_id=root.id,
        canonical_root=str(canonical_root),
        canonical_path=str(canonical),
        relative_path=canonical.relative_to(canonical_root).as_posix(),
        format=format_name,
        overwrite=bool(direction == "export" and overwrite),
        existed_at_issue=existed,
        resource_fingerprint=_resource_fingerprint(
            canonical, direction=direction, existed=existed
        ),
    )


def _canonical_target(path: Path, direction: Direction) -> tuple[Path, bool]:
    existed = path.exists()
    try:
        if existed or direction == "import":
            return path.resolve(strict=True), existed
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise AuthorityDeniedError("host path or its parent is unavailable") from exc
    if not parent.is_dir():
        raise AuthorityDeniedError("export parent must be an existing directory")
    return parent / path.name, False


def _resource_fingerprint(path: Path, *, direction: Direction, existed: bool) -> str:
    if direction == "import" or existed:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise AuthorityDeniedError(
                "host resource could not be opened safely"
            ) from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise AuthorityDeniedError("host resource must be a regular file")
            content_digest: str | None = None
            if direction == "import":
                digest = hashlib.sha256()
                while chunk := os.read(descriptor, 1024 * 1024):
                    digest.update(chunk)
                content_digest = digest.hexdigest()
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        try:
            current = path.lstat()
        except OSError as exc:
            raise AuthorityDeniedError("host resource changed during binding") from exc
        identity = (int(before.st_dev), int(before.st_ino))
        if identity != (int(after.st_dev), int(after.st_ino)) or identity != (
            int(current.st_dev),
            int(current.st_ino),
        ):
            raise AuthorityDeniedError("host resource changed during binding")
        if int(before.st_size) != int(after.st_size) or int(before.st_mtime_ns) != int(
            after.st_mtime_ns
        ):
            raise AuthorityDeniedError("host resource changed during binding")
        payload: dict[str, object] = {
            "device": int(before.st_dev),
            "inode": int(before.st_ino),
            "size": int(before.st_size),
            "mtime_ns": int(before.st_mtime_ns),
        }
        if content_digest is not None:
            payload["sha256"] = content_digest
    else:
        parent_stat = path.parent.stat()
        payload = {
            "parent_device": int(parent_stat.st_dev),
            "parent_inode": int(parent_stat.st_ino),
            "destination_absent": True,
        }
    return _json_digest(payload)


def _validate_requested_text(path: str) -> None:
    if not path or path != path.strip():
        raise AuthorityDeniedError(
            "host path must be non-empty without outer whitespace"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise AuthorityDeniedError("host path contains control characters")
    _reject_device_path(path)
    windows = PureWindowsPath(path)
    posix = PurePosixPath(path.replace("\\", "/"))
    if ".." in (*windows.parts, *posix.parts):
        raise AuthorityDeniedError(
            "host relative path must not contain parent traversal"
        )


def _normalize_relative_path(path: str) -> Path:
    windows = PureWindowsPath(path)
    posix = PurePosixPath(path.replace("\\", "/"))
    if windows.is_absolute() or posix.is_absolute() or windows.drive or windows.root:
        raise AuthorityDeniedError("HostFileRef relative path must not be absolute")
    if ".." in (*windows.parts, *posix.parts):
        raise AuthorityDeniedError(
            "HostFileRef relative path must not contain parent traversal"
        )
    parts = tuple(part for part in posix.parts if part not in {"", "."})
    if not parts:
        raise AuthorityDeniedError("HostFileRef relative path must name a file")
    return Path(*parts)


def _reject_device_path(path: str) -> None:
    normalized = path.replace("/", "\\")
    if normalized.startswith(("\\\\?\\", "\\\\.\\", "\\??\\", "\\\\??\\")):
        raise AuthorityDeniedError("Windows device paths are not accepted")


def _is_unc_path(path: str) -> bool:
    normalized = path.replace("/", "\\")
    return normalized.startswith("\\\\")


def _unc_matches_approved_root(
    requested_path: str,
    roots: tuple[AuthorityRoot, ...],
) -> bool:
    requested = PureWindowsPath(requested_path)
    for root in roots:
        if not _is_unc_path(root.canonical_path):
            continue
        try:
            requested.relative_to(PureWindowsPath(root.canonical_path))
        except ValueError:
            continue
        return True
    return False


def _require_windows_case_exact(
    root: Path,
    candidate: Path,
    *,
    final_exists: bool,
) -> None:
    """Reject case-folded aliases while allowing a new export filename."""

    if os.name != "nt":
        return
    absolute = Path(os.path.abspath(str(candidate)))
    root_parts = root.parts
    candidate_parts = absolute.parts
    if len(candidate_parts) < len(root_parts) or any(
        requested != approved
        for requested, approved in zip(candidate_parts, root_parts, strict=False)
    ):
        raise AuthorityDeniedError("host path case does not match approved root")
    relative_parts = candidate_parts[len(root_parts) :]
    check_count = len(relative_parts) if final_exists else len(relative_parts) - 1
    current = root
    for component in relative_parts[: max(check_count, 0)]:
        try:
            names = [entry.name for entry in os.scandir(current)]
        except OSError as exc:
            raise AuthorityDeniedError(
                "host path case could not be revalidated"
            ) from exc
        if component in names:
            current /= component
            continue
        if any(os.path.normcase(name) == os.path.normcase(component) for name in names):
            raise AuthorityDeniedError("host path case does not match filesystem")
        raise AuthorityDeniedError("host path changed during case validation")


def _require_contained(root: Path, target: Path) -> None:
    if not _is_contained(root, target):
        raise AuthorityDeniedError("host path is outside its approved root")


def _is_contained(root: Path, target: Path) -> bool:
    try:
        return os.path.commonpath((str(root), str(target))) == str(root)
    except ValueError:
        return False


def _validated_target_bindings(
    operation: OperationSpec,
    bindings: tuple[CadTargetBinding, ...],
    *,
    expected_producers: dict[tuple[str, str], str] | None = None,
) -> tuple[CadTargetBinding, ...]:
    if not _operation_requires_authority(operation):
        if bindings:
            raise AuthorityDeniedError(
                "read-only operation carries CAD target authority"
            )
        return ()
    expected_targets = cad_operation_target_requirements(operation)
    if isinstance(operation, ExportOperation):
        if len(bindings) != 1:
            raise AuthorityDeniedError(
                "export operation requires exactly one resolved CAD target binding"
            )
        binding = bindings[0]
        expected_ref = str(operation.target_ref)
        if (
            binding.reference_kind != "export_target"
            or binding.requested_ref != expected_ref
        ):
            raise AuthorityDeniedError(
                "CAD target binding does not match export reference"
            )
    else:
        expected = (("active_document", "active_document"), *expected_targets)
        if len(bindings) != len(expected):
            raise AuthorityDeniedError(
                "mutating operation lacks exact document/entity target bindings"
            )
        if not all(
            _binding_matches_requirement(binding, requirement)
            for binding, requirement in zip(bindings, expected, strict=True)
        ):
            raise AuthorityDeniedError(
                "CAD target bindings do not match the operation references"
            )
        document_identity = bindings[0].document_identity
        if any(
            binding.document_identity != document_identity for binding in bindings[1:]
        ):
            raise AuthorityDeniedError(
                "CAD target bindings do not belong to the bound document"
            )
        if expected_producers is not None:
            for binding, requirement in zip(
                bindings[1:], expected_targets, strict=True
            ):
                expected_producer = expected_producers.get(requirement)
                if binding.producer_operation_id != expected_producer:
                    raise AuthorityDeniedError(
                        "CAD target binding producer proof does not match the validated graph"
                    )
    for binding in bindings:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                binding.document_identity,
                binding.entity_identity,
                binding.fingerprint,
            )
        ):
            raise AuthorityDeniedError(
                "CAD target binding identity proof is incomplete"
            )
        if not all(
            re.fullmatch(r"[0-9a-f]{64}", value)
            for value in (
                binding.document_identity,
                binding.entity_identity,
                binding.fingerprint,
            )
        ):
            raise AuthorityDeniedError("CAD target binding proof is invalid")
    return bindings


def _binding_matches_requirement(
    binding: CadTargetBinding,
    requirement: tuple[str, str],
) -> bool:
    reference_kind, requested_ref = requirement
    if binding.requested_ref != requested_ref:
        return False
    if reference_kind == "parameter_target":
        return binding.reference_kind in {"parameter_existing", "parameter_absent"}
    return binding.reference_kind == reference_kind


def cad_operation_target_requirements(
    operation: OperationSpec,
) -> tuple[tuple[str, str], ...]:
    """Return every pre-existing CAD reference a mutation can dereference.

    The ordered registry is shared by authority issuance and provider target
    resolution.  Result names and newly-created output names are deliberately
    excluded; they are bound by the producing operation digest.  Unknown
    operation kinds fail closed so a new mutator cannot silently inherit only
    document-level authority.
    """

    kind = str(operation.kind)

    def one(reference_kind: str, value: Any) -> tuple[tuple[str, str], ...]:
        if value is None:
            return ()
        return ((reference_kind, str(value)),)

    def many(reference_kind: str, values: Any) -> tuple[tuple[str, str], ...]:
        return tuple((reference_kind, str(value)) for value in values or ())

    if kind == "parameter.set":
        return one("parameter_target", getattr(operation, "name"))
    if kind == "io.import":
        return ()
    if kind == "component.create":
        return one("component", getattr(operation, "parent_ref", None))
    if kind == "sketch.create":
        return one("component", getattr(operation, "component_ref"))
    if kind in {"sketch.rectangle", "sketch.circle"}:
        return one("sketch", getattr(operation, "sketch_ref"))
    if kind == "feature.extrude":
        return (
            *one("component", getattr(operation, "component_ref")),
            *one("profile", getattr(operation, "profile_ref")),
            *one(
                "body",
                getattr(operation, "target_body_ref", None)
                if getattr(operation, "operation", None) != "new_body"
                else None,
            ),
        )
    if kind in {"sketch.constraint", "sketch.dimension"}:
        sketch_ref = str(getattr(operation, "sketch_ref"))
        return (
            ("sketch", sketch_ref),
            *tuple(
                ("sketch_entity", f"{sketch_ref}::{entity_ref}")
                for entity_ref in getattr(operation, "entity_refs")
            ),
        )
    if kind == "feature.revolve":
        return (
            *one("component", getattr(operation, "component_ref")),
            *one("profile", getattr(operation, "profile_ref")),
            *one("axis", getattr(operation, "axis_ref")),
            *one(
                "body",
                getattr(operation, "target_body_ref", None)
                if getattr(operation, "operation", None) != "new_body"
                else None,
            ),
        )
    if kind == "feature.sweep":
        return (
            *one("component", getattr(operation, "component_ref")),
            *one("profile", getattr(operation, "profile_ref")),
            *one("path", getattr(operation, "path_ref")),
            *one(
                "body",
                getattr(operation, "target_body_ref", None)
                if getattr(operation, "operation", None) != "new_body"
                else None,
            ),
        )
    if kind == "feature.loft":
        return (
            *one("component", getattr(operation, "component_ref")),
            *many("profile", getattr(operation, "profile_refs")),
            *many("path", getattr(operation, "guide_refs")),
            *one(
                "body",
                getattr(operation, "target_body_ref", None)
                if getattr(operation, "operation", None) != "new_body"
                else None,
            ),
        )
    if kind == "feature.pattern":
        return (
            *many("geometry", getattr(operation, "target_refs")),
            *one("axis", getattr(operation, "axis_ref", None)),
            *one("path", getattr(operation, "path_ref", None)),
        )
    if kind == "feature.mirror":
        return (
            *many("geometry", getattr(operation, "target_refs")),
            *one("plane", getattr(operation, "plane_ref")),
        )
    if kind == "feature.boolean":
        return (
            *one("body", getattr(operation, "target_ref")),
            *many("body", getattr(operation, "tool_refs")),
        )
    if kind == "assembly.joint":
        return (
            *one("occurrence", getattr(operation, "parent_ref")),
            *one("occurrence", getattr(operation, "child_ref")),
        )
    if kind == "assembly.rigid_group":
        return many("occurrence", getattr(operation, "occurrence_refs"))
    if kind in {"experimental.sheet_metal", "experimental.cam"}:
        return one("geometry", getattr(operation, "target_ref"))
    if kind == "io.export":
        return one("export_target", getattr(operation, "target_ref"))
    if kind.startswith("analysis."):
        return ()
    raise AuthorityDeniedError(
        f"operation target binding registry does not support {kind}"
    )


def cad_graph_target_producers(
    spec: CadSpecV2,
) -> dict[str, dict[tuple[str, str], str]]:
    """Bind in-graph references to a unique, declared producer operation.

    Only outputs whose identity can be resolved after materialization are
    registered.  A consumer must transitively depend on the producer; a name
    collision cannot silently turn a planned output into an external target.
    """

    produced: dict[tuple[str, str], str] = {}
    dependencies: dict[str, frozenset[str]] = {}
    result: dict[str, dict[tuple[str, str], str]] = {}
    for operation in spec.operations:
        ancestors: set[str] = set()
        for dependency in operation.depends_on:
            ancestors.add(str(dependency))
            ancestors.update(dependencies.get(str(dependency), frozenset()))
        dependencies[operation.id] = frozenset(ancestors)

        operation_producers: dict[tuple[str, str], str] = {}
        for requirement in cad_operation_target_requirements(operation):
            producer = produced.get(requirement)
            if producer is None:
                continue
            if producer not in ancestors:
                raise AuthorityDeniedError(
                    f"operation {operation.id} references planned target "
                    f"{requirement[1]!r} without a declared dependency on {producer}"
                )
            operation_producers[requirement] = producer
        result[operation.id] = operation_producers

        for output in cad_operation_produced_targets(operation):
            previous = produced.get(output)
            if previous is not None:
                raise AuthorityDeniedError(
                    f"CAD graph target {output[1]!r} has multiple producers: "
                    f"{previous}, {operation.id}"
                )
            produced[output] = operation.id
    return result


def cad_operation_produced_targets(
    operation: OperationSpec,
) -> tuple[tuple[str, str], ...]:
    kind = str(operation.kind)
    if kind == "component.create":
        return (("component", str(getattr(operation, "name"))),)
    if kind == "sketch.create":
        return (("sketch", str(getattr(operation, "name"))),)
    if kind in {"sketch.rectangle", "sketch.circle"}:
        return (("profile", str(getattr(operation, "result_ref"))),)
    if (
        kind
        in {
            "feature.extrude",
            "feature.revolve",
            "feature.sweep",
            "feature.loft",
        }
        and getattr(operation, "operation", None) == "new_body"
    ):
        result_name = str(getattr(operation, "result_name"))
        return (("body", result_name), ("geometry", result_name))
    if kind == "io.import":
        component_name = str(getattr(operation, "component_name"))
        return (("component", component_name),)
    return ()


def _operation_requires_authority(operation: OperationSpec) -> bool:
    return not str(operation.kind).startswith("analysis.")


def _validated_legacy_target_bindings(
    operation: LegacyOutputOperation,
    bindings: tuple[CadTargetBinding, ...],
) -> tuple[CadTargetBinding, ...]:
    if len(bindings) != 1:
        raise AuthorityDeniedError(
            "legacy host output requires exactly one target binding"
        )
    binding = bindings[0]
    expected_kind = (
        "export_target" if operation.kind == "legacy.export" else "active_document"
    )
    expected_ref = (
        operation.target_identity
        if operation.kind == "legacy.export"
        else "active_document"
    )
    if binding.reference_kind != expected_kind or binding.requested_ref != expected_ref:
        raise AuthorityDeniedError(
            "legacy output target binding does not match the operation"
        )
    if not all(
        isinstance(value, str) and value.strip()
        for value in (
            binding.document_identity,
            binding.entity_identity,
            binding.fingerprint,
        )
    ):
        raise AuthorityDeniedError("legacy output target identity proof is incomplete")
    if not re.fullmatch(r"[0-9a-f]{64}", binding.document_identity):
        raise AuthorityDeniedError("legacy document binding fingerprint is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", binding.entity_identity):
        raise AuthorityDeniedError("legacy entity binding fingerprint is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", binding.fingerprint):
        raise AuthorityDeniedError("legacy target binding fingerprint is invalid")
    return bindings


def _binding_digest(
    host_path: HostPathBinding | None,
    targets: tuple[CadTargetBinding, ...],
    *,
    spec_digest: str,
    operation_digest: str,
    session_id: str,
    provider: str,
) -> str:
    return _json_digest(
        {
            "host_path": asdict(host_path) if host_path is not None else None,
            "targets": [asdict(target) for target in targets],
            "spec_digest": spec_digest,
            "operation_digest": operation_digest,
            "session_id": session_id,
            "provider": provider,
        }
    )


def _model_digest(model: Any) -> str:
    if is_dataclass(model):
        return _json_digest(asdict(cast(Any, model)))
    return _json_digest(model.model_dump(mode="json", exclude_none=False))


def _json_digest(value: Any) -> str:
    serialized = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _root_payload(root: AuthorityRoot) -> dict[str, Any]:
    return {
        "id": root.id,
        "path": root.canonical_path,
        "formats": sorted(root.formats),
        "default": root.default,
    }


__all__ = [
    "AuthorityBroker",
    "AuthorityDeniedError",
    "AuthorityPolicy",
    "AuthorityRoot",
    "BindingProof",
    "BoundOperation",
    "CadTargetBinding",
    "CapabilityLedger",
    "HostPathBinding",
    "LegacyOutputOperation",
    "OperationCapability",
    "PreparedOperationGraph",
    "cad_graph_target_producers",
    "cad_operation_target_requirements",
    "revalidate_host_path",
]
