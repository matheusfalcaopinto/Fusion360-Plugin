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
import threading
import time
from dataclasses import asdict, dataclass, is_dataclass, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Literal, cast

from cad_spec.v2 import CadSpecV2, ExportOperation, ImportOperation, OperationSpec


Direction = Literal["import", "export"]
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
        return bool(self.import_roots or self.export_roots)

    @property
    def root_ids(self) -> dict[str, tuple[str, ...]]:
        return {
            "import": tuple(root.id for root in self.import_roots),
            "export": tuple(root.id for root in self.export_roots),
        }

    def safe_summary(self) -> dict[str, object]:
        """Return the only policy fields suitable for public diagnostics."""

        return {
            "digest": self.digest,
            "io_enabled": self.io_enabled,
            "root_ids": {
                direction: list(root_ids)
                for direction, root_ids in self.root_ids.items()
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
        updated = {**record, "state": state}
        path = self._record_path(str(record["capability_id"]))
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
                operation, provided_targets.get(operation.id, ())
            )
            proof = None
            if host_path is not None:
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
            if bound.host_path is None or bound.proof is None:
                bound_operations.append(bound)
                continue
            capability = OperationCapability(
                capability_id=secrets.token_urlsafe(24),
                direction=bound.host_path.direction,
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
        return PreparedOperationGraph(
            spec_digest=spec_digest,
            session_id=session_id,
            provider=provider,
            operations=tuple(bound_operations),
        )

    def validate_host_requests(self, spec: CadSpecV2) -> None:
        """Validate every host path before any provider-side target resolution."""

        for operation in spec.operations:
            self._bind_host_path(operation)

    def prepare_legacy_output_graph(
        self,
        operations: tuple[LegacyOutputOperation, ...],
        *,
        session_id: str,
        provider: str,
    ) -> PreparedOperationGraph:
        """Bind all deprecated v1 outputs before the first provider dispatch."""

        if not session_id.strip():
            raise AuthorityDeniedError("session id is required for operation authority")
        if not provider.strip():
            raise AuthorityDeniedError("provider is required for operation authority")
        if len({operation.id for operation in operations}) != len(operations):
            raise AuthorityDeniedError("legacy output operation ids must be unique")
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
            target_bindings = _legacy_target_bindings(
                operation,
                spec_digest,
                session_id=session_id,
                provider=provider,
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
        if not isinstance(
            operation, (ImportOperation, ExportOperation, LegacyOutputOperation)
        ):
            if bound.host_path is not None or bound.capability is not None:
                raise AuthorityDeniedError("non-I/O operation carries host authority")
            return
        if bound.host_path is None or bound.capability is None or bound.proof is None:
            raise AuthorityDeniedError("host I/O requires a complete bound capability")
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
            bound.host_path.direction,
            bound.host_path.root_id,
            bound.host_path.canonical_path,
            bound.spec_digest,
            bound.operation_digest,
            bound.session_id,
            bound.provider,
            bound.host_path.overwrite,
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
        stat = path.stat()
        payload = {
            "device": int(stat.st_dev),
            "inode": int(stat.st_ino),
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        }
    else:
        stat = path.parent.stat()
        payload = {
            "parent_device": int(stat.st_dev),
            "parent_inode": int(stat.st_ino),
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
) -> tuple[CadTargetBinding, ...]:
    if not isinstance(operation, ExportOperation):
        if bindings:
            raise AuthorityDeniedError(
                "non-export operation carries CAD target authority"
            )
        return ()
    if len(bindings) != 1:
        raise AuthorityDeniedError(
            "host export requires exactly one resolved CAD target binding"
        )
    binding = bindings[0]
    expected_ref = str(operation.target_ref)
    if (
        binding.reference_kind != "export_target"
        or binding.requested_ref != expected_ref
    ):
        raise AuthorityDeniedError("CAD target binding does not match export reference")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (
            binding.document_identity,
            binding.entity_identity,
            binding.fingerprint,
        )
    ):
        raise AuthorityDeniedError("CAD target binding identity proof is incomplete")
    if not re.fullmatch(r"[0-9a-f]{64}", binding.fingerprint):
        raise AuthorityDeniedError("CAD target binding fingerprint is invalid")
    return bindings


def _legacy_target_bindings(
    operation: LegacyOutputOperation,
    spec_digest: str,
    *,
    session_id: str,
    provider: str,
) -> tuple[CadTargetBinding, ...]:
    document_identity = _json_digest(
        {
            "scope": "legacy-output-session",
            "session_id": session_id,
            "provider": provider,
            "spec_digest": spec_digest,
        }
    )
    return (
        CadTargetBinding(
            reference_kind=operation.kind,
            requested_ref=operation.target_identity,
            document_identity=document_identity,
            entity_identity=operation.target_identity,
            fingerprint=_json_digest(
                {
                    "reference_kind": operation.kind,
                    "requested_ref": operation.target_identity,
                    "spec_digest": spec_digest,
                }
            ),
        ),
    )


def _validated_legacy_target_bindings(
    operation: LegacyOutputOperation,
    bindings: tuple[CadTargetBinding, ...],
) -> tuple[CadTargetBinding, ...]:
    if len(bindings) != 1:
        raise AuthorityDeniedError(
            "legacy host output requires exactly one target binding"
        )
    binding = bindings[0]
    if (
        binding.reference_kind != operation.kind
        or binding.requested_ref != operation.target_identity
        or binding.entity_identity != operation.target_identity
    ):
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
    if not re.fullmatch(r"[0-9a-f]{64}", binding.fingerprint):
        raise AuthorityDeniedError("legacy target binding fingerprint is invalid")
    return bindings


def _binding_digest(
    host_path: HostPathBinding,
    targets: tuple[CadTargetBinding, ...],
    *,
    spec_digest: str,
    operation_digest: str,
    session_id: str,
    provider: str,
) -> str:
    return _json_digest(
        {
            "host_path": asdict(host_path),
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
    "revalidate_host_path",
]
