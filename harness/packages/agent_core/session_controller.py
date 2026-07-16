"""End-to-end session orchestration."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any
from collections.abc import Mapping

from pydantic import BaseModel, Field

from agent_core.authority import (
    AuthorityBroker,
    AuthorityDeniedError,
    AuthorityPolicy,
)
from agent_core.executor import ExecutionContext, ExecutionResult, Executor
from agent_core.fusion_scripts import (
    compact_snapshot_script,
    hub_inventory_script,
    safe_delete_apply_script,
    safe_visibility_apply_script,
)
from agent_core.guardrails import (
    bind_safe_change_targets,
    canonical_snapshot_fingerprint,
    classify_safe_change,
    compact_mock_snapshot,
    diff_snapshots,
    normalize_operation,
    snapshot_document_identity,
)
from agent_core.planner import PlanningRequest, RuleBasedPlanner
from agent_core.repair_loop import RepairLoop
from cad_spec.models import CadSpec
from fusion_mcp_adapter.adapter import FusionMcpAdapter
from fusion_mcp_adapter.backend import create_fusion_client
from fusion_mcp_adapter.errors import ErrorCode
from fusion_mcp_adapter.manifest_store import ManifestStore
from fusion_mcp_adapter.mock_client import MOCK_NATIVE_TOOLS, MockMcpClient
from fusion_mcp_adapter.policy import ToolPolicy
from fusion_mcp_adapter.tool_result import PublicError
from fusion_tool_facade.facade import FusionFacade
from fusion_tool_facade.policy import MOCK_FACADE_NATIVE_MAP
from fusion_tool_facade.vendor_facade import (
    VENDOR_FACADE_NATIVE_TOOLS,
    VendorFusionFacade,
    is_vendor_manifest,
)
from memory.gate import MemoryGate
from memory.retriever import MemoryRetriever
from memory.store import MemoryStore
from memory.writer import MemoryWriter
from skills.loader import SkillLoader
from skills.router import SkillRouter
from telemetry.journal import SessionJournal
from telemetry.trace import JsonlTraceLogger
from verifier.geometry import GeometryVerifier
from verifier.result_models import VerificationResult


class SessionOptions(BaseModel):
    """Session configuration."""

    mode: str = "mock"
    project: str = "default"
    max_repairs: int = 5
    workspace_root: Path = Path("workspace")
    output_dir: Path = Path("outputs")
    manifest_dir: Path = Path("manifests")
    dry_run: bool = False

    model_config = {"arbitrary_types_allowed": True}


class SessionResult(BaseModel):
    """End-to-end session result."""

    session_id: str
    status: str
    cad_spec_path: Path
    journal_path: Path
    trace_path: Path
    execution: ExecutionResult
    verification: VerificationResult
    repair_attempts: list = Field(default_factory=list)
    memory_updates: list[str] = Field(default_factory=list)
    dry_run: bool = False

    model_config = {"arbitrary_types_allowed": True}


class VerifyActiveResult(BaseModel):
    """Verifier-only active design result."""

    session_id: str
    status: str
    cad_spec_path: Path
    journal_path: Path
    trace_path: Path
    verification: VerificationResult

    model_config = {"arbitrary_types_allowed": True}


class CaptureViewportResult(BaseModel):
    """Safe viewport capture result."""

    session_id: str
    status: str
    path: Path
    journal_path: Path
    trace_path: Path
    capture: dict

    model_config = {"arbitrary_types_allowed": True}


class SessionController:
    """Coordinate memory retrieval, planning, execution, verification, and journaling."""

    def __init__(
        self,
        planner: RuleBasedPlanner | None = None,
        *,
        real_client: Any | None = None,
        manifest_store: ManifestStore | None = None,
        environment_snapshot: Mapping[str, str] | None = None,
        authority_broker: AuthorityBroker | None = None,
        authority_provider: str = "legacy-facade",
    ) -> None:
        self.planner = planner or RuleBasedPlanner()
        self.real_client = real_client or create_fusion_client()
        self._owns_real_client = real_client is None
        self.manifest_store = manifest_store
        if authority_broker is None:
            try:
                authority_broker = AuthorityBroker(AuthorityPolicy.from_environment())
            except AuthorityDeniedError:
                authority_broker = AuthorityBroker(AuthorityPolicy.deny_all())
        self.authority_broker = authority_broker
        self.authority_provider = authority_provider
        self._safe_change_locks: dict[str, asyncio.Lock] = {}
        captured = (
            dict(environment_snapshot)
            if environment_snapshot is not None
            else {
                "launcher_python": os.getenv("FUSION_AGENT_PYTHON") or "",
                "fusion_mcp_endpoint": os.getenv("FUSION_MCP_ENDPOINT") or "",
                "default_mode": os.getenv("FUSION_AGENT_DEFAULT_MODE") or "",
                "require_real": os.getenv("FUSION_AGENT_REQUIRE_REAL") or "",
                "allow_dry_run": os.getenv("FUSION_AGENT_ALLOW_DRY_RUN") or "",
            }
        )
        self._environment_snapshot = MappingProxyType(captured)

    def _real_client(self) -> Any:
        return self.real_client

    async def aclose(self) -> None:
        """Close a client created by this controller.

        Runtime-owned clients are injected and closed by the runtime lifespan;
        standalone controller users own one lazy client for their whole command.
        """

        if self._owns_real_client:
            await self.real_client.aclose(timeout_seconds=2.0)

    async def __aenter__(self) -> "SessionController":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def _manifest_store(self, root: Path | str) -> ManifestStore:
        return self.manifest_store or ManifestStore(root)

    async def run(
        self,
        user_prompt: str,
        project: str | None = None,
        mode: str = "mock",
        options: SessionOptions | None = None,
    ) -> SessionResult:
        """Run one complete modeling session."""

        options = options or SessionOptions(mode=mode, project=project or "default")
        options.mode = mode
        options.project = project or options.project
        memory_store = MemoryStore(workspace_root=options.workspace_root)
        memory_store.seed_global()
        retriever = MemoryRetriever(memory_store)
        retrieved = retriever.retrieve(user_prompt, project=options.project)
        gated_memory = MemoryGate().filter(retrieved, user_prompt)

        skills = SkillRouter(SkillLoader().load()).rank(user_prompt)
        spec = await self.planner.plan(
            PlanningRequest(
                user_prompt=user_prompt,
                project=options.project,
                memory=gated_memory,
                skills=[skill.name for skill in skills],
            )
        )

        return await self.run_spec(
            spec,
            user_prompt=user_prompt,
            project=options.project,
            mode=mode,
            options=options,
            memory_store=memory_store,
        )

    async def run_spec(
        self,
        spec: CadSpec,
        *,
        user_prompt: str | None = None,
        project: str | None = None,
        mode: str = "mock",
        options: SessionOptions | None = None,
        memory_store: MemoryStore | None = None,
    ) -> SessionResult:
        """Run an already validated legacy CadSpec without replanning it.

        This compatibility boundary ensures that a caller-supplied CadSpec v1
        is the document that is executed.  It is never replaced by a new plan
        derived from the spec's intent.
        """

        options = options or SessionOptions(mode=mode, project=project or "default")
        options.mode = mode
        options.project = project or options.project
        prompt_text = user_prompt or spec.intent
        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        workspace_root = options.workspace_root
        output_dir = options.output_dir
        memory_store = memory_store or MemoryStore(workspace_root=workspace_root)
        memory_store.seed_global()

        journal = SessionJournal(workspace_root, options.project, session_id)
        trace_logger = JsonlTraceLogger(journal.trace_path)
        execution_context = ExecutionContext(
            mode=mode,
            project=options.project,
            output_dir=output_dir,
            dry_run=options.dry_run,
            session_id=session_id,
        )

        if options.dry_run:
            trace_logger.log(
                {
                    "session_id": session_id,
                    "event": "dry_run_skipped_execution",
                    "mode": mode,
                    "project": options.project,
                }
            )
            executor = Executor()
            execution = await executor.execute(spec, execution_context)
            verification = _simulated_verification(spec)
            repair_loop: RepairLoop | None = None
            status = "simulated"
        else:
            facade = await self._build_facade(
                mode,
                options=options,
                trace_logger=trace_logger,
                session_id=session_id,
            )
            executor = Executor(
                facade,
                authority_broker=self.authority_broker,
                authority_provider=self.authority_provider,
            )
            verifier = GeometryVerifier(facade)
            repair_loop = RepairLoop(
                verifier,
                max_total_attempts=options.max_repairs,
                executor=executor,
                trace_logger=trace_logger,
                session_id=session_id,
            )
            execution = await executor.execute(spec, execution_context)
            verification = await repair_loop.run(spec, context=execution_context)
            status = "success" if verification.passed else "failed"

        cad_spec_path = journal.write_text("cad_spec.json", spec.to_json_text())
        journal.write_text("prompt.md", prompt_text)

        memory_updates = MemoryWriter(memory_store).write_session_memory(
            project=options.project,
            session_id=session_id,
            prompt=prompt_text,
            verification=verification,
        )
        journal.write_json("verification.json", verification)
        journal_path = journal.finalize(
            mode=mode,
            user_prompt=prompt_text,
            cad_spec_path=cad_spec_path,
            verification=verification,
            final_status=status,
            summary=(
                f"Session {status}. "
                f"{'No MCP calls executed (dry-run). ' if options.dry_run else ''}"
                f"Created {len(execution.created_objects)} objects."
            ),
            memory_updates=memory_updates,
            exports=execution.exports,
            simulated=options.dry_run,
            repair_attempts=repair_loop.attempts if repair_loop else [],
            repaired=any(
                attempt.success
                for attempt in (repair_loop.attempts if repair_loop else [])
            ),
        )

        return SessionResult(
            session_id=session_id,
            status=status,
            cad_spec_path=cad_spec_path,
            journal_path=journal_path,
            trace_path=journal.trace_path,
            execution=execution,
            verification=verification,
            repair_attempts=repair_loop.attempts if repair_loop else [],
            memory_updates=memory_updates,
            dry_run=options.dry_run,
        )

    async def inspect(
        self,
        mode: str = "mock",
        options: SessionOptions | None = None,
        inspection_options: dict | None = None,
    ) -> dict:
        """Inspect a design through the configured facade."""

        options = options or SessionOptions(mode=mode)
        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        trace_logger = JsonlTraceLogger(Path("logs") / f"inspect_{session_id}.jsonl")
        facade = await self._build_facade(
            mode, options=options, trace_logger=trace_logger, session_id=session_id
        )
        if isinstance(facade, VendorFusionFacade):
            return await facade.inspect_design(inspection_options or {})
        return await facade.inspect_design()

    async def verify_active(
        self,
        user_prompt: str,
        project: str | None = None,
        mode: str = "mock",
        options: SessionOptions | None = None,
    ) -> VerifyActiveResult:
        """Plan a CadSpec for the prompt and verify it against the active design without executing geometry."""

        options = options or SessionOptions(mode=mode, project=project or "default")
        options.mode = mode
        options.project = project or options.project
        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

        memory_store = MemoryStore(workspace_root=options.workspace_root)
        memory_store.seed_global()
        retrieved = MemoryRetriever(memory_store).retrieve(
            user_prompt, project=options.project
        )
        gated_memory = MemoryGate().filter(retrieved, user_prompt)
        skills = SkillRouter(SkillLoader().load()).rank(user_prompt)
        spec = await self.planner.plan(
            PlanningRequest(
                user_prompt=user_prompt,
                project=options.project,
                memory=gated_memory,
                skills=[skill.name for skill in skills],
            )
        )

        journal = SessionJournal(options.workspace_root, options.project, session_id)
        trace_logger = JsonlTraceLogger(journal.trace_path)
        facade = await self._build_facade(
            mode, options=options, trace_logger=trace_logger, session_id=session_id
        )
        verification = await GeometryVerifier(facade).verify(spec)
        status = "success" if verification.passed else "failed"

        cad_spec_path = journal.write_text("cad_spec.json", spec.to_json_text())
        journal.write_text("prompt.md", user_prompt)
        journal.write_json("verification.json", verification)
        journal_path = journal.finalize(
            mode=mode,
            user_prompt=user_prompt,
            cad_spec_path=cad_spec_path,
            verification=verification,
            final_status=status,
            summary=f"Verifier-only session {status}. No geometry execution was performed.",
            exports=[],
            simulated=False,
        )
        return VerifyActiveResult(
            session_id=session_id,
            status=status,
            cad_spec_path=cad_spec_path,
            journal_path=journal_path,
            trace_path=journal.trace_path,
            verification=verification,
        )

    async def capture_viewport(
        self,
        *,
        project: str | None = None,
        mode: str = "mock",
        options: SessionOptions | None = None,
        output_dir: Path | str | None = None,
        name: str = "active_design_capture",
        view: str = "isometric",
        isolate_prefix: str | None = None,
        width: int = 1600,
        height: int = 1100,
    ) -> CaptureViewportResult:
        """Capture the active Fusion viewport through the safe facade."""

        options = options or SessionOptions(mode=mode, project=project or "default")
        options.mode = mode
        options.project = project or options.project
        if output_dir is not None:
            options.output_dir = Path(output_dir)
        # A real capture destination must already exist beneath an approved
        # export root. Creating a caller-selected directory before authority
        # validation would itself be an unauthorized host mutation.
        if mode != "real":
            options.output_dir.mkdir(parents=True, exist_ok=True)
        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        journal = SessionJournal(options.workspace_root, options.project, session_id)
        trace_logger = JsonlTraceLogger(journal.trace_path)
        facade = await self._build_facade(
            mode, options=options, trace_logger=trace_logger, session_id=session_id
        )

        filename = name if name.lower().endswith(".png") else f"{name}.png"
        path = options.output_dir / filename
        executor = Executor(
            facade,
            authority_broker=self.authority_broker,
            authority_provider=self.authority_provider,
        )
        capture_payload = await executor.capture_viewport(
            context=ExecutionContext(
                mode=mode,
                project=options.project,
                output_dir=options.output_dir,
                session_id=session_id,
            ),
            name=Path(filename).stem,
            path=filename,
            view=view,
            isolate_prefix=isolate_prefix,
            width=width,
            height=height,
        )
        status = "success"
        verification = VerificationResult.pass_result(
            metrics={"capture": capture_payload.get("screenshot", capture_payload)}
        )
        cad_spec_path = journal.write_text("cad_spec.json", "{}\n")
        journal.write_text("prompt.md", f"capture {view} viewport")
        journal.write_json("verification.json", verification)
        journal.write_json("capture.json", capture_payload)
        journal_path = journal.finalize(
            mode=mode,
            user_prompt=f"capture {view} viewport",
            cad_spec_path=cad_spec_path,
            verification=verification,
            final_status=status,
            summary=f"Viewport capture {status}: {path}",
            exports=[str(path)],
            simulated=False,
        )
        return CaptureViewportResult(
            session_id=session_id,
            status=status,
            path=path,
            journal_path=journal_path,
            trace_path=journal.trace_path,
            capture=capture_payload,
        )

    async def discover_tools(
        self, mode: str = "real", options: SessionOptions | None = None
    ):
        """Discover and save native MCP tools."""

        options = options or SessionOptions(mode=mode)
        client = MockMcpClient() if mode == "mock" else self._real_client()
        manifest_store = self._manifest_store(options.manifest_dir)
        adapter = FusionMcpAdapter(
            client=client,
            manifest_store=manifest_store,
            policy=ToolPolicy.from_manifest(
                MOCK_NATIVE_TOOLS if mode == "mock" else set()
            ),
        )
        manifest = await adapter.discover(save=False)
        try:
            manifest_store.save_if_changed(manifest)
        except Exception:
            # Discovery is valid even when OneDrive temporarily prevents the
            # diagnostic manifest from being persisted. The store exposes the
            # persistence error through session health.
            pass
        return manifest

    async def session_health(
        self, mode: str = "real", options: SessionOptions | None = None
    ) -> dict:
        """Report launcher, manifest, endpoint, and native tool-surface health."""

        options = options or SessionOptions(mode=mode)
        options.mode = mode
        manifest_store = self._manifest_store(options.manifest_dir)
        manifest_load_error: str | None = None
        try:
            manifest = manifest_store.load_latest(mode)
        except Exception as exc:
            # Health is the recovery surface for corrupt or partially synced
            # OneDrive diagnostics, so it must report rather than propagate.
            manifest = None
            manifest_load_error = f"{type(exc).__name__}: {exc}"
        diagnostics: dict = {
            "mode": mode,
            "launcher_ok": True,
            "launcher_python": self._environment_snapshot.get("launcher_python", ""),
            "python_executable": sys.executable,
            "fusion_mcp_endpoint": self._environment_snapshot.get(
                "fusion_mcp_endpoint", ""
            ),
            "default_mode": self._environment_snapshot.get("default_mode", ""),
            "require_real": self._environment_snapshot.get("require_real", ""),
            "allow_dry_run": self._environment_snapshot.get("allow_dry_run", ""),
            "manifest_ok": manifest is not None,
            "manifest_error": manifest_load_error,
            "manifest_source": manifest.source if manifest else None,
            "manifest_tool_count": len(manifest.tools) if manifest else 0,
            "manifest_status": manifest_store.latest_status(),
            "mcp_server_ok": False,
            "real_endpoint_ok": None,
            "native_tools_attached": False,
            "native_tool_count": 0,
            "native_tool_sample": [],
        }
        client = MockMcpClient() if mode == "mock" else self._real_client()
        try:
            if mode == "real":
                await client.ping()
                discovered = client.current_manifest
                if discovered is None:  # defensive: ping connects and discovers
                    raise RuntimeError(
                        "live MCP manifest unavailable after health ping"
                    )
                live_client_diagnostics = client.diagnostics
            else:
                discovered = await client.list_tools()
                live_client_diagnostics = {}
            names = sorted(discovered.names())
            cached_fingerprint = manifest.fingerprint if manifest else None
            live_fingerprint = discovered.fingerprint
            manifest_drift = bool(
                live_client_diagnostics.get("manifest_drift")
                or (
                    cached_fingerprint is not None
                    and cached_fingerprint != live_fingerprint
                )
            )
            diagnostics.update(
                {
                    "mcp_server_ok": True,
                    "real_endpoint_ok": True if mode == "real" else None,
                    "native_tools_attached": bool(names),
                    "native_tool_count": len(names),
                    "native_tool_sample": names[:20],
                    "live_manifest_fingerprint": live_fingerprint,
                    "cached_manifest_fingerprint": cached_fingerprint,
                    "manifest_drift": manifest_drift,
                    "connection": dict(live_client_diagnostics),
                }
            )
        except Exception as exc:  # noqa: BLE001 - diagnostic surface must normalize failures
            diagnostics.update(
                {
                    "mcp_server_ok": False,
                    "real_endpoint_ok": False if mode == "real" else None,
                    "native_error": f"{type(exc).__name__}: {exc}",
                }
            )
        diagnostics["healthy"] = bool(
            diagnostics["launcher_ok"]
            and diagnostics["mcp_server_ok"]
            and diagnostics["manifest_ok"]
            and diagnostics["native_tools_attached"]
            and not diagnostics.get("manifest_drift", False)
            and (mode != "real" or diagnostics["real_endpoint_ok"])
        )
        return diagnostics

    async def compact_snapshot(
        self,
        *,
        project: str | None = None,
        mode: str = "real",
        options: SessionOptions | None = None,
        max_occurrences: int = 500,
        max_bodies: int = 500,
        include_transforms: bool = False,
        max_entities_visited: int = 1000,
        deadline_ms: int = 1500,
        max_response_bytes: int = 1024 * 1024,
        label: str = "snapshot",
    ) -> dict:
        """Capture a capped programmatic snapshot for large-model evidence."""

        if max_entities_visited < 1 or max_entities_visited > 5000:
            raise ValueError("max_entities_visited must be between 1 and 5000")
        if deadline_ms < 50 or deadline_ms > 5000:
            raise ValueError("deadline_ms must be between 50 and 5000")
        if max_response_bytes < 4096 or max_response_bytes > 1024 * 1024:
            raise ValueError("max_response_bytes must be between 4096 and 1048576")
        options = options or SessionOptions(mode=mode, project=project or "default")
        options.mode = mode
        options.project = project or options.project
        options.output_dir.mkdir(parents=True, exist_ok=True)
        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        if mode == "mock":
            inspection = await self.inspect(mode=mode, options=options)
            snapshot = compact_mock_snapshot(
                inspection,
                max_occurrences=max_occurrences,
                max_bodies=max_bodies,
                max_entities_visited=max_entities_visited,
                max_response_bytes=max_response_bytes,
            )
            raw_result = {"success": True, "snapshot": snapshot}
        else:
            trace_logger = JsonlTraceLogger(
                Path("logs") / f"snapshot_{session_id}.jsonl"
            )
            facade = await self._build_facade(
                mode, options=options, trace_logger=trace_logger, session_id=session_id
            )
            if isinstance(facade, VendorFusionFacade):
                raw_result = await facade._execute_trusted_read_script_json(
                    compact_snapshot_script(
                        {
                            "project": options.project,
                            "max_occurrences": max_occurrences,
                            "max_bodies": max_bodies,
                            "include_transforms": include_transforms,
                            "max_entities_visited": max_entities_visited,
                            "deadline_ms": deadline_ms,
                            "max_response_bytes": max_response_bytes,
                        }
                    )
                )
            else:
                inspection = await facade.inspect_design()
                raw_result = {
                    "success": True,
                    "snapshot": compact_mock_snapshot(
                        inspection,
                        max_occurrences=max_occurrences,
                        max_bodies=max_bodies,
                        max_entities_visited=max_entities_visited,
                        max_response_bytes=max_response_bytes,
                    ),
                }
        snapshot = raw_result.get("snapshot", raw_result)
        snapshot_id = f"{label}_{session_id}"
        snapshot_dir = options.output_dir / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        path = snapshot_dir / f"{snapshot_id}.json"
        payload = {
            "snapshot_id": snapshot_id,
            "project": options.project,
            "mode": mode,
            "max_occurrences": max_occurrences,
            "max_bodies": max_bodies,
            "include_transforms": include_transforms,
            "max_entities_visited": max_entities_visited,
            "deadline_ms": deadline_ms,
            "max_response_bytes": max_response_bytes,
            "snapshot": snapshot,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return {**payload, "snapshot_path": str(path)}

    async def hub_inventory(
        self,
        *,
        mode: str = "real",
        query: str = "",
        max_results: int = 50,
        enrich: bool = True,
        options: SessionOptions | None = None,
    ) -> dict:
        """Run a metadata-first hub inventory without DataFolder traversal."""

        options = options or SessionOptions(mode=mode)
        if mode == "mock":
            return {
                "mode": "mock",
                "strategy": {
                    "primary": "metadata_search",
                    "enrichment": "mock",
                    "direct_datafolder_traversal": False,
                },
                "results": [],
            }
        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        trace_logger = JsonlTraceLogger(
            Path("logs") / f"hub_inventory_{session_id}.jsonl"
        )
        facade = await self._build_facade(
            mode, options=options, trace_logger=trace_logger, session_id=session_id
        )
        if not isinstance(facade, VendorFusionFacade):
            raise RuntimeError("hub inventory requires the Fusion CRUD script profile")
        result = await facade._execute_trusted_read_script_json(
            hub_inventory_script(
                {"query": query, "max_results": max_results, "enrich": enrich}
            )
        )
        return {"mode": mode, **result}

    async def safe_change_preview(
        self,
        *,
        project: str | None = None,
        mode: str = "real",
        operation: str,
        targets: list[dict],
        policy: dict | None = None,
        options: SessionOptions | None = None,
    ) -> dict:
        """Create a baseline-backed safe-change preview."""

        operation = normalize_operation(operation)
        options = options or SessionOptions(mode=mode, project=project or "default")
        options.mode = mode
        options.project = project or options.project
        policy = policy or {}
        baseline = await self.compact_snapshot(
            project=options.project,
            mode=mode,
            options=options,
            max_occurrences=int(policy.get("max_occurrences", 500)),
            max_bodies=int(policy.get("max_bodies", 500)),
            include_transforms=bool(policy.get("include_transforms", False)),
            max_entities_visited=int(policy.get("max_entities_visited", 1000)),
            deadline_ms=int(policy.get("deadline_ms", 1500)),
            max_response_bytes=int(policy.get("max_response_bytes", 1024 * 1024)),
            label="before",
        )
        baseline_snapshot = baseline.get("snapshot", {})
        baseline_complete = _snapshot_is_complete(baseline_snapshot)
        document_identity = snapshot_document_identity(baseline_snapshot)
        state_fingerprint = canonical_snapshot_fingerprint(baseline_snapshot)
        bound_targets, binding_errors = bind_safe_change_targets(
            targets, baseline_snapshot
        )
        classification = classify_safe_change(
            operation,
            bound_targets if operation == "delete" else targets,
            policy,
            baseline_snapshot,
        )
        if not baseline_complete:
            classification = {
                **classification,
                "allow_apply": False,
                "blocked": True,
                "classification": "incomplete_baseline",
                "risk_level": "high",
                "reasons": list(classification.get("reasons") or [])
                + [
                    "The bounded baseline is incomplete; increase the explicit inspection budget before applying any change."
                ],
            }
        elif not document_identity or not state_fingerprint:
            classification = {
                **classification,
                "allow_apply": False,
                "blocked": True,
                "classification": "document_identity_unavailable",
                "risk_level": "high",
                "reasons": list(classification.get("reasons") or [])
                + [
                    "The active document has no stable data-file or unsaved-session identity."
                ],
            }
        elif binding_errors:
            classification = {
                **classification,
                "allow_apply": False,
                "blocked": True,
                "classification": "target_binding_failed",
                "risk_level": "high",
                "reasons": list(classification.get("reasons") or [])
                + [
                    "Every mutation target must resolve to exactly one stable snapshot entity."
                ],
            }
        preview_id = datetime.now(timezone.utc).strftime("preview_%Y%m%dT%H%M%S%fZ")
        preview_dir = options.output_dir / "safe_change_previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        preview_path = preview_dir / f"{preview_id}.json"
        payload = {
            "schema_version": "safe_change_preview.v2",
            "preview_id": preview_id,
            "preview_status": "ready",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "project": options.project,
            "mode": mode,
            "operation": operation,
            "targets": targets,
            "policy": policy,
            "classification": classification,
            "blocked": bool(classification.get("blocked")),
            "baseline_complete": baseline_complete,
            "baseline_stop_reason": baseline_snapshot.get("stop_reason"),
            "baseline_id": baseline["snapshot_id"],
            "before_snapshot_path": baseline["snapshot_path"],
            "document_identity": document_identity,
            "state_fingerprint": state_fingerprint,
            "bound_targets": bound_targets,
            "binding_errors": binding_errors,
            "inspection_budget": _snapshot_budget(policy),
            "requirements": _normalize_safe_change_requirements(
                policy.get("requirements") or []
            ),
            "negative_impact": False,
        }
        _atomic_write_json(preview_path, payload)
        return {**payload, "preview_path": str(preview_path)}

    async def safe_change_apply(
        self,
        *,
        project: str | None = None,
        mode: str = "real",
        preview_id: str,
        batch_size: int,
        confirm_destructive: bool,
        save_after: bool = False,
        options: SessionOptions | None = None,
    ) -> dict:
        """Apply one small previewed batch and verify for visible regressions."""

        if save_after:
            raise ValueError(
                "save_after must be false; save only after a separate verified audit"
            )
        if "/" in preview_id or "\\" in preview_id or ".." in preview_id:
            raise ValueError("preview_id must be a simple identifier")
        options = options or SessionOptions(mode=mode, project=project or "default")
        options.mode = mode
        options.project = project or options.project
        preview_path = (
            options.output_dir / "safe_change_previews" / f"{preview_id}.json"
        )
        if not preview_path.exists():
            raise FileNotFoundError(preview_path)
        lock_key = str(preview_path.resolve())
        lock = self._safe_change_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            preview = json.loads(preview_path.read_text(encoding="utf-8"))
            if preview.get("schema_version") != "safe_change_preview.v2":
                return _aborted_change(
                    preview,
                    "legacy_preview_requires_refresh",
                    "No change was applied. Create and review a new v2 preview.",
                )
            if preview.get("project") != options.project or preview.get("mode") != mode:
                return _aborted_change(
                    preview,
                    "preview_context_mismatch",
                    "Apply the preview in its original project and mode.",
                )
            if preview.get("preview_status") == "applying":
                if preview.get("dispatch_phase") == "claimed":
                    # The durable claim was written, but the backend invocation
                    # marker was not.  This is a proven pre-dispatch crash: do
                    # not reuse the old intent against potentially drifted state.
                    preview.update(
                        {
                            "preview_status": "stale",
                            "stale_at": datetime.now(timezone.utc).isoformat(),
                            "dispatched": False,
                            "may_have_applied": False,
                            "post_dispatch_replay_suppressed": False,
                            "mutation_outcome": "known",
                        }
                    )
                    _atomic_write_json(preview_path, preview)
                    preview_path.with_suffix(".claim").unlink(missing_ok=True)
                    return _aborted_change(
                        preview,
                        "interrupted_before_backend_invocation",
                        "No mutation was dispatched. Inspect current state and create a fresh preview.",
                    )
                return {
                    **preview,
                    "status": "mutation_outcome_unknown",
                    "error_code": "MUTATION_OUTCOME_UNKNOWN",
                    "dispatched": bool(preview.get("dispatched", False)),
                    "may_have_applied": True,
                    "post_dispatch_replay_suppressed": bool(
                        preview.get("post_dispatch_replay_suppressed", True)
                    ),
                    "mutation_outcome": "unknown",
                    "mutation_status": "outcome_unknown",
                    "assertion_status": "not_run",
                    "intent_coverage": "none",
                    "verification_level": "assertions_only",
                    "recovery_instructions": (
                        "Do not replay this preview. Perform a bounded readback of the bound targets; "
                        "then create a fresh preview only if the mutation is proven absent."
                    ),
                }
            if preview.get("preview_status") != "ready":
                return _aborted_change(
                    preview,
                    f"preview_{preview.get('preview_status') or 'invalid'}",
                    "This preview cannot be replayed. Create a new preview after inspecting the active design.",
                )

            operation = normalize_operation(preview["operation"])
            classification = dict(preview.get("classification") or {})
            if classification.get("blocked"):
                return _aborted_change(
                    preview,
                    "preview_blocked",
                    "Review the preview classification and adjust targets/policy before applying.",
                )
            if operation == "delete" and not confirm_destructive:
                return _aborted_change(
                    preview,
                    "confirm_destructive_required",
                    "Set confirm_destructive=true only after reviewing the baseline and targets.",
                )
            if operation == "delete" and batch_size > 5:
                return _aborted_change(
                    preview,
                    "delete_batch_too_large",
                    "Run the first destructive batch with batch_size<=5.",
                )
            if operation in {"move", "componentize"}:
                return _aborted_change(
                    preview,
                    f"{operation}_execution_not_implemented",
                    "Use preview output to build a specialized reversible workflow.",
                )
            if operation not in {"visibility", "delete"}:
                return _aborted_change(
                    preview,
                    "unsupported_operation",
                    "Only visibility and bounded delete applies are currently executable.",
                )

            before_payload = _read_json_path(Path(preview["before_snapshot_path"]))
            before_snapshot = before_payload.get("snapshot", before_payload)
            if not _snapshot_is_complete(before_snapshot):
                return _aborted_change(
                    preview,
                    "incomplete_baseline",
                    "Capture a complete baseline with a larger explicit budget before retrying.",
                )
            if canonical_snapshot_fingerprint(before_snapshot) != preview.get(
                "state_fingerprint"
            ) or snapshot_document_identity(before_snapshot) != preview.get(
                "document_identity"
            ):
                preview.update(
                    {
                        "preview_status": "stale",
                        "stale_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                _atomic_write_json(preview_path, preview)
                return _aborted_change(
                    preview,
                    "preview_baseline_integrity_failed",
                    "No mutation was dispatched. Create a new preview and keep its baseline artifact unchanged.",
                )

            preapply = await self.compact_snapshot(
                project=options.project,
                mode=mode,
                options=options,
                **_snapshot_budget(
                    preview.get("inspection_budget") or preview.get("policy") or {}
                ),
                label="preapply",
            )
            preapply_snapshot = preapply.get("snapshot", preapply)
            fresh_bindings, fresh_binding_errors = bind_safe_change_targets(
                preview.get("targets") or [], preapply_snapshot
            )
            guard = {
                "complete": _snapshot_is_complete(preapply_snapshot),
                "document_matches": snapshot_document_identity(preapply_snapshot)
                == preview.get("document_identity"),
                "fingerprint_matches": canonical_snapshot_fingerprint(preapply_snapshot)
                == preview.get("state_fingerprint"),
                "bindings_match": _binding_identities(fresh_bindings)
                == _binding_identities(preview.get("bound_targets") or []),
                "binding_errors": fresh_binding_errors,
            }
            guard["passed"] = bool(
                guard["complete"]
                and guard["document_matches"]
                and guard["fingerprint_matches"]
                and guard["bindings_match"]
                and not fresh_binding_errors
            )
            if not guard["passed"]:
                preview.update(
                    {
                        "preview_status": "stale",
                        "stale_at": datetime.now(timezone.utc).isoformat(),
                        "preapply_snapshot_id": preapply.get("snapshot_id"),
                        "preapply_snapshot_path": preapply.get("snapshot_path"),
                        "preapply_guard": guard,
                    }
                )
                _atomic_write_json(preview_path, preview)
                return _aborted_change(
                    preview,
                    "preview_state_drift",
                    "No mutation was dispatched. Create a new preview from the current design state.",
                )

            binding_by_index = {
                int(binding["target_index"]): binding
                for binding in fresh_bindings
                if isinstance(binding, dict) and "target_index" in binding
            }
            batch_targets = []
            for target_index, target in enumerate(
                list(preview.get("targets") or [])[: max(1, int(batch_size))]
            ):
                binding = binding_by_index.get(target_index, {})
                if operation == "delete":
                    batch_targets.append(dict(binding))
                else:
                    batch_targets.append(
                        {
                            **target,
                            "entity_token": binding.get("entity_token")
                            or target.get("entity_token"),
                            "key": binding.get("key") or target.get("key"),
                            "path": binding.get("path") or target.get("path"),
                        }
                    )
            session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            facade = None
            if mode != "mock":
                trace_logger = JsonlTraceLogger(
                    Path("logs") / f"safe_change_{operation}_{session_id}.jsonl"
                )
                facade = await self._build_facade(
                    mode,
                    options=options,
                    trace_logger=trace_logger,
                    session_id=session_id,
                )
                if not isinstance(facade, VendorFusionFacade):
                    return _aborted_change(
                        preview,
                        "unsupported_facade",
                        "Safe-change apply requires the Fusion CRUD script profile.",
                    )

            claim_path = preview_path.with_suffix(".claim")
            try:
                claim_fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(claim_fd)
            except FileExistsError:
                return _aborted_change(
                    preview,
                    "preview_already_claimed",
                    "This preview is already applying or consumed; create a new preview.",
                )

            preview.update(
                {
                    "preview_status": "applying",
                    "dispatch_phase": "claimed",
                    "applying_at": datetime.now(timezone.utc).isoformat(),
                    "dispatch_operation_id": f"safe-change:{preview_id}",
                    "preapply_snapshot_id": preapply.get("snapshot_id"),
                    "preapply_snapshot_path": preapply.get("snapshot_path"),
                    "preapply_guard": guard,
                }
            )
            _atomic_write_json(preview_path, preview)

            applied: dict[str, object]
            error_code: str | None = None
            explicit_transport: dict[str, object] = {}
            if mode == "mock":
                applied = {
                    "success": True,
                    "changed": [] if operation == "visibility" else None,
                    "changed_count": 0,
                    "deleted": [] if operation == "delete" else None,
                    "deleted_count": 0,
                }
            else:
                try:
                    script = (
                        safe_delete_apply_script({"targets": batch_targets})
                        if operation == "delete"
                        else safe_visibility_apply_script({"targets": batch_targets})
                    )
                    preview.update(
                        {
                            "dispatch_phase": "backend_invocation_started",
                            "backend_invocation_started_at": datetime.now(
                                timezone.utc
                            ).isoformat(),
                        }
                    )
                    _atomic_write_json(preview_path, preview)
                    applied = await facade._execute_script_json(  # type: ignore[union-attr]
                        script,
                        operation_id=str(preview["dispatch_operation_id"]),
                    )
                    value = applied.get("fusion_agent_transport")
                    if isinstance(value, dict):
                        explicit_transport = value
                except RuntimeError as exc:
                    candidate = str(getattr(exc, "error_code", ""))
                    error_code = (
                        candidate
                        if candidate in {item.value for item in ErrorCode}
                        else ErrorCode.FUSION_OPERATION_FAILED.value
                    )
                    value = getattr(exc, "transport", None)
                    if isinstance(value, dict):
                        explicit_transport = value
                    applied = {"success": False, "error_code": error_code}

            transport = _safe_change_transport_fields(
                mode=mode,
                diagnostics=(
                    self.real_client.diagnostics
                    if mode != "mock" and hasattr(self.real_client, "diagnostics")
                    else {}
                ),
                invoked=mode != "mock",
                error_code=error_code,
                expected_operation_id=str(preview.get("dispatch_operation_id") or ""),
                explicit_transport=explicit_transport,
            )
            must_consume = bool(
                mode == "mock"
                or transport["dispatched"]
                or transport["may_have_applied"]
            )
            preview.update(
                {
                    "preview_status": "consumed" if must_consume else "ready",
                    "consumed_at": datetime.now(timezone.utc).isoformat()
                    if must_consume
                    else None,
                    **transport,
                }
            )
            _atomic_write_json(preview_path, preview)
            if preview["preview_status"] == "ready":
                claim_path.unlink(missing_ok=True)
            if error_code:
                outcome_unknown = transport["mutation_outcome"] == "unknown"
                status = (
                    "mutation_outcome_unknown"
                    if outcome_unknown
                    else "execution_failed"
                )
                public_code = (
                    ErrorCode.MUTATION_OUTCOME_UNKNOWN.value
                    if outcome_unknown
                    else error_code
                )
                public_error = PublicError.downstream_failure(public_code).model_dump(
                    mode="json"
                )
                applied["error"] = public_error
                return {
                    **preview,
                    "status": status,
                    "error_code": public_code,
                    "error": public_error,
                    "applied": applied,
                    "negative_impact": False,
                    "recovery_instructions": "Do not replay this preview. Inspect the active design before deciding whether to undo."
                    if transport["dispatched"]
                    else "Correct the pre-dispatch failure and create a fresh preview.",
                }

            after = await self.compact_snapshot(
                project=options.project,
                mode=mode,
                options=options,
                **_snapshot_budget(preview.get("inspection_budget") or {}),
                label="after",
            )
            after_snapshot = after.get("snapshot", after)
            if not _snapshot_is_complete(after_snapshot):
                return {
                    **preview,
                    "applied": applied,
                    "status": "applied_unverified",
                    "verification_complete": False,
                    "mutation_status": (
                        "outcome_unknown"
                        if transport["mutation_outcome"] == "unknown"
                        else "unknown"
                    ),
                    "assertion_status": "incomplete",
                    "intent_coverage": "none",
                    "verification_level": "assertions_only",
                    "negative_impact": False,
                    "after_snapshot_path": after["snapshot_path"],
                    "abort_reason": "incomplete_readback",
                    "recovery_instructions": "Do not save. The bounded readback was incomplete; inspect with a larger explicit budget before deciding whether to undo.",
                }

            diff = diff_snapshots(before_snapshot, after_snapshot)
            verification = _safe_change_verification(
                preview, applied, diff, len(batch_targets)
            )
            verification["mutation_status"] = "observed_in_readback"
            if transport["mutation_outcome"] == "unknown":
                verification["mutation_status"] = "outcome_unknown"
                verification["contract_verified"] = False
                nested = verification.get("verification")
                if isinstance(nested, dict):
                    nested["transport_outcome_unknown"] = True
                status = "mutation_outcome_unknown"
                recovery = "Do not replay this preview. Preserve the readback and resolve the unknown transport outcome explicitly."
            elif diff["negative_impact"]:
                status = "aborted_after_verification"
                recovery = "Do not save. Use Fusion Undo for the last batch, then capture a fresh compact snapshot."
            elif verification["contract_verified"]:
                status = "applied_verified"
                recovery = ""
            elif verification["assertion_status"] == "passed":
                status = "applied_partially_verified"
                recovery = "Review uncovered intent requirements before saving."
            else:
                status = "applied_unverified"
                recovery = "Do not save until the intended result has been independently verified."
            return {
                **preview,
                **diff,
                **verification,
                "applied": applied,
                "status": status,
                "error_code": (
                    "MUTATION_OUTCOME_UNKNOWN"
                    if status == "mutation_outcome_unknown"
                    else None
                ),
                "after_snapshot_path": after["snapshot_path"],
                "recovery_instructions": recovery,
            }

    async def _build_facade(
        self,
        mode: str,
        options: SessionOptions,
        trace_logger: JsonlTraceLogger,
        session_id: str,
    ) -> FusionFacade:
        if mode == "mock":
            client = MockMcpClient()
            policy = ToolPolicy.from_manifest(MOCK_NATIVE_TOOLS)
            manifest_store = self._manifest_store(options.manifest_dir)
            return FusionFacade(
                FusionMcpAdapter(
                    client=client,
                    manifest_store=manifest_store,
                    policy=policy,
                    trace_logger=trace_logger,
                    session_id=session_id,
                )
            )

        manifest_store = self._manifest_store(options.manifest_dir)
        client = self._real_client()
        # The live negotiated surface is authoritative. Calling list_tools here
        # is the explicit revalidation boundary after a reconnect drift; a stale
        # OneDrive/disk manifest can never construct the callable facade.
        manifest = await client.list_tools()
        try:
            manifest_store.save_if_changed(manifest)
        except Exception:
            # A persistence outage must not discard a valid live connection.
            pass
        manifest_names = manifest.names()
        if is_vendor_manifest(manifest_names):
            policy = ToolPolicy.from_manifest(
                VENDOR_FACADE_NATIVE_TOOLS & manifest_names
            )
            return VendorFusionFacade(
                FusionMcpAdapter(
                    client=client,
                    manifest=manifest,
                    manifest_store=manifest_store,
                    policy=policy,
                    trace_logger=trace_logger,
                    session_id=session_id,
                ),
                available_tools=manifest_names,
            )

        policy = ToolPolicy.from_manifest(
            set(MOCK_FACADE_NATIVE_MAP.values()) & manifest_names
        )
        return FusionFacade(
            FusionMcpAdapter(
                client=client,
                manifest=manifest,
                manifest_store=manifest_store,
                policy=policy,
                trace_logger=trace_logger,
                session_id=session_id,
            )
        )


def _simulated_verification(spec) -> VerificationResult:
    """Return a deterministic pass result for dry-run sessions."""

    return VerificationResult(
        passed=True,
        metrics={
            "simulated": True,
            "planned_components": len(spec.components),
            "planned_features": sum(
                len(component.features) for component in spec.components
            ),
            "planned_parameters": len(spec.parameters),
            "planned_exports": [
                feature.name
                for component in spec.components
                for feature in component.features
                if feature.type == "export"
            ],
        },
    )


def _read_json_path(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _snapshot_is_complete(snapshot: dict) -> bool:
    """Return whether a snapshot is safe to use as a mutation oracle.

    Snapshot v1 did not expose the new completeness fields, so it remains
    compatible when it was not explicitly payload-capped. Snapshot v2 fails
    closed on any incomplete traversal, inexact count, or truncation.
    """

    if not isinstance(snapshot, dict) or not snapshot:
        return False
    if "complete" not in snapshot:
        return not bool(snapshot.get("payload_capped", False))
    return bool(
        snapshot.get("complete")
        and snapshot.get("counts_exact", False)
        and not snapshot.get("truncated", False)
        and not snapshot.get("stop_reason")
    )


def _snapshot_budget(source: dict) -> dict[str, int | bool]:
    """Normalize the exact bounded-inspection budget reused by preview/apply."""

    return {
        "max_occurrences": int(source.get("max_occurrences", 500)),
        "max_bodies": int(source.get("max_bodies", 500)),
        "include_transforms": bool(source.get("include_transforms", False)),
        "max_entities_visited": int(source.get("max_entities_visited", 1000)),
        "deadline_ms": int(source.get("deadline_ms", 1500)),
        "max_response_bytes": int(source.get("max_response_bytes", 1024 * 1024)),
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Persist a preview transition atomically within its output directory."""

    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    os.replace(temporary, path)


def _binding_identities(bindings: list[dict]) -> list[tuple[str, ...]]:
    return sorted(
        (
            str(binding.get("kind") or ""),
            str(binding.get("identifier") or ""),
            str(binding.get("binding_fingerprint") or ""),
            str(binding.get("visible")),
            str(binding.get("is_root")),
            str(binding.get("is_referenced")),
            str(binding.get("is_imported")),
            str(binding.get("shared_definition")),
        )
        for binding in bindings
        if isinstance(binding, dict)
    )


def _normalize_safe_change_requirements(requirements: object) -> list[dict]:
    if not isinstance(requirements, list):
        raise ValueError("policy.requirements must be an array")
    normalized: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(requirements):
        if not isinstance(item, dict):
            raise ValueError(f"policy.requirements[{index}] must be an object")
        requirement_id = str(item.get("id") or "").strip()
        assertion_ids = item.get("assertion_ids") or []
        if not requirement_id or requirement_id in seen:
            raise ValueError("policy requirement ids must be present and unique")
        if not isinstance(assertion_ids, list) or not all(
            isinstance(value, str) and value for value in assertion_ids
        ):
            raise ValueError(
                f"policy.requirements[{index}].assertion_ids must be an array of ids"
            )
        oracle = str(item.get("oracle") or "contract")
        if oracle not in {"contract", "independent_oracle"}:
            raise ValueError(
                f"policy.requirements[{index}].oracle must be contract or independent_oracle"
            )
        seen.add(requirement_id)
        normalized.append(
            {
                "id": requirement_id,
                "description": str(item.get("description") or ""),
                "required": bool(item.get("required", True)),
                "assertion_ids": list(dict.fromkeys(assertion_ids)),
                "oracle": oracle,
            }
        )
    return normalized


def _safe_change_transport_fields(
    *,
    mode: str,
    diagnostics: dict,
    invoked: bool,
    error_code: str | None = None,
    error: str | None = None,
    expected_operation_id: str,
    explicit_transport: dict[str, object] | None = None,
) -> dict[str, object]:
    # ``error`` is a compatibility-only alias for callers from 0.4.0.  Treat it
    # as a code only when it is an exact member of the public error registry;
    # arbitrary exception text must never influence or escape this boundary.
    if error_code is None and error in {item.value for item in ErrorCode}:
        error_code = error
    explicit_transport = (
        explicit_transport if isinstance(explicit_transport, dict) else {}
    )
    diagnostic_call = (
        diagnostics.get("last_call_outcome") if isinstance(diagnostics, dict) else None
    )
    diagnostic_call = diagnostic_call if isinstance(diagnostic_call, dict) else {}
    last_call = explicit_transport or diagnostic_call
    event_matches = bool(
        last_call
        and (
            not last_call.get("operation_id")
            or last_call.get("operation_id") == expected_operation_id
        )
        and (not last_call.get("semantics") or last_call.get("semantics") == "mutating")
    )
    dispatched = (
        bool(event_matches and last_call.get("dispatched")) if mode != "mock" else False
    )
    outcome_event_missing = bool(mode != "mock" and invoked and not event_matches)
    unknown = bool(
        (event_matches and last_call.get("mutation_outcome") == "unknown")
        or error_code == ErrorCode.MUTATION_OUTCOME_UNKNOWN.value
        or outcome_event_missing
    )
    replay_suppressed = bool(
        (dispatched or outcome_event_missing)
        and last_call.get("post_dispatch_replay_suppressed", True)
    )
    return {
        "dispatched": dispatched,
        "may_have_applied": bool(unknown and (dispatched or outcome_event_missing)),
        "post_dispatch_replay_suppressed": replay_suppressed,
        "mutation_outcome": "unknown" if unknown else "known",
        "dispatch_event_correlated": event_matches,
        "dispatch_operation_id": expected_operation_id,
    }


def _safe_change_verification(
    preview: dict,
    applied: dict,
    diff: dict,
    expected_target_count: int,
) -> dict[str, object]:
    actual_count = int(
        applied.get("deleted_count", applied.get("changed_count", 0)) or 0
    )
    assertions = [
        {"id": "readback_complete", "passed": True},
        {
            "id": "no_visible_regression",
            "passed": not bool(diff.get("negative_impact")),
        },
        {
            "id": "expected_target_count",
            "passed": actual_count == expected_target_count,
            "expected": expected_target_count,
            "actual": actual_count,
        },
    ]
    assertions_by_id = {item["id"]: item for item in assertions}
    requirements = preview.get("requirements") or []
    required = [item for item in requirements if item.get("required", True)]
    requirement_results = []
    for requirement in requirements:
        assertion_ids = list(requirement.get("assertion_ids") or [])
        covered = bool(assertion_ids) and all(
            assertion_id in assertions_by_id for assertion_id in assertion_ids
        )
        passed = covered and all(
            assertions_by_id[assertion_id]["passed"] for assertion_id in assertion_ids
        )
        independent = requirement.get("oracle") == "independent_oracle"
        requirement_results.append(
            {
                **requirement,
                "covered": covered and not independent,
                "passed": passed and not independent,
                **({"oracle_evidence": "not_available"} if independent else {}),
            }
        )
    required_results = [
        item for item in requirement_results if item.get("required", True)
    ]
    if not required:
        coverage = "none"
    elif all(item["covered"] for item in required_results):
        coverage = "complete"
    else:
        coverage = (
            "partial" if any(item["covered"] for item in required_results) else "none"
        )
    assertion_status = (
        "passed"
        if assertions and all(item["passed"] for item in assertions)
        else "failed"
    )
    contract_verified = bool(
        required
        and coverage == "complete"
        and assertion_status == "passed"
        and all(item["passed"] for item in required_results)
    )
    independent_declared = any(
        item.get("oracle") == "independent_oracle" for item in required_results
    )
    if independent_declared:
        contract_verified = False
    verification_level = (
        "independent_oracle"
        if independent_declared
        else "contract"
        if required
        else "assertions_only"
    )
    return {
        "mutation_status": "observed_in_readback",
        "assertion_status": assertion_status,
        "intent_coverage": coverage,
        "verification_level": verification_level,
        "contract_verified": contract_verified,
        "verification": {
            "assertions": assertions,
            "requirements": requirement_results,
            "readback_complete": True,
        },
    }


def _aborted_change(preview: dict, reason: str, recovery: str) -> dict:
    return {
        **preview,
        "status": "aborted_before_apply",
        "dispatched": False,
        "may_have_applied": False,
        "post_dispatch_replay_suppressed": False,
        "mutation_outcome": "known",
        "mutation_status": "not_dispatched",
        "assertion_status": "not_run",
        "intent_coverage": "none",
        "verification_level": "assertions_only",
        "negative_impact": False,
        "abort_reason": reason,
        "recovery_instructions": recovery,
    }
