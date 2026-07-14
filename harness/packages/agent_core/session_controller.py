"""End-to-end session orchestration."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from agent_core.executor import ExecutionContext, ExecutionResult, Executor
from agent_core.fusion_scripts import compact_snapshot_script, hub_inventory_script, safe_delete_apply_script, safe_visibility_apply_script
from agent_core.guardrails import compact_mock_snapshot, classify_safe_change, diff_snapshots, normalize_operation
from agent_core.planner import PlanningRequest, RuleBasedPlanner
from agent_core.repair_loop import RepairLoop
from fusion_mcp_adapter.adapter import FusionMcpAdapter
from fusion_mcp_adapter.manifest_store import ManifestStore
from fusion_mcp_adapter.mock_client import MOCK_NATIVE_TOOLS, MockMcpClient
from fusion_mcp_adapter.policy import ToolPolicy
from fusion_mcp_adapter.real_client import RealMcpClient
from fusion_tool_facade.facade import FusionFacade
from fusion_tool_facade.policy import MOCK_FACADE_NATIVE_MAP
from fusion_tool_facade.vendor_facade import VENDOR_FACADE_NATIVE_TOOLS, VendorFusionFacade, is_vendor_manifest
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
        real_client: RealMcpClient | None = None,
        manifest_store: ManifestStore | None = None,
    ) -> None:
        self.planner = planner or RuleBasedPlanner()
        self.real_client = real_client or RealMcpClient()
        self._owns_real_client = real_client is None
        self.manifest_store = manifest_store

    def _real_client(self) -> RealMcpClient:
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
        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        workspace_root = options.workspace_root
        output_dir = options.output_dir
        memory_store = MemoryStore(workspace_root=workspace_root)
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

        journal = SessionJournal(workspace_root, options.project, session_id)
        trace_logger = JsonlTraceLogger(journal.trace_path)
        execution_context = ExecutionContext(mode=mode, project=options.project, output_dir=output_dir, dry_run=options.dry_run)

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
            executor = Executor(facade)
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
        journal.write_text("prompt.md", user_prompt)

        memory_updates = MemoryWriter(memory_store).write_session_memory(
            project=options.project,
            session_id=session_id,
            prompt=user_prompt,
            verification=verification,
        )
        journal.write_json("verification.json", verification)
        journal_path = journal.finalize(
            mode=mode,
            user_prompt=user_prompt,
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
            repaired=any(attempt.success for attempt in (repair_loop.attempts if repair_loop else [])),
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
        facade = await self._build_facade(mode, options=options, trace_logger=trace_logger, session_id=session_id)
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
        retrieved = MemoryRetriever(memory_store).retrieve(user_prompt, project=options.project)
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
        facade = await self._build_facade(mode, options=options, trace_logger=trace_logger, session_id=session_id)
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
        options.output_dir.mkdir(parents=True, exist_ok=True)
        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        journal = SessionJournal(options.workspace_root, options.project, session_id)
        trace_logger = JsonlTraceLogger(journal.trace_path)
        facade = await self._build_facade(mode, options=options, trace_logger=trace_logger, session_id=session_id)

        filename = name if name.lower().endswith(".png") else f"{name}.png"
        path = options.output_dir / filename
        capture_payload = await facade.capture_viewport(
            name=Path(filename).stem,
            path=path,
            view=view,
            isolate_prefix=isolate_prefix,
            width=width,
            height=height,
        )
        status = "success"
        verification = VerificationResult.pass_result(metrics={"capture": capture_payload.get("screenshot", capture_payload)})
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

    async def discover_tools(self, mode: str = "real", options: SessionOptions | None = None):
        """Discover and save native MCP tools."""

        options = options or SessionOptions(mode=mode)
        client = MockMcpClient() if mode == "mock" else self._real_client()
        manifest_store = self._manifest_store(options.manifest_dir)
        adapter = FusionMcpAdapter(
            client=client,
            manifest_store=manifest_store,
            policy=ToolPolicy.from_manifest(MOCK_NATIVE_TOOLS if mode == "mock" else set()),
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

    async def session_health(self, mode: str = "real", options: SessionOptions | None = None) -> dict:
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
            "launcher_python": os.getenv("FUSION_AGENT_PYTHON") or "",
            "python_executable": sys.executable,
            "fusion_mcp_endpoint": os.getenv("FUSION_MCP_ENDPOINT") or "",
            "default_mode": os.getenv("FUSION_AGENT_DEFAULT_MODE") or "",
            "require_real": os.getenv("FUSION_AGENT_REQUIRE_REAL") or "",
            "allow_dry_run": os.getenv("FUSION_AGENT_ALLOW_DRY_RUN") or "",
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
                    raise RuntimeError("live MCP manifest unavailable after health ping")
                live_client_diagnostics = client.diagnostics
            else:
                discovered = await client.list_tools()
                live_client_diagnostics = {}
            names = sorted(discovered.names())
            cached_fingerprint = manifest.fingerprint if manifest else None
            live_fingerprint = discovered.fingerprint
            manifest_drift = bool(
                live_client_diagnostics.get("manifest_drift")
                or (cached_fingerprint is not None and cached_fingerprint != live_fingerprint)
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
            trace_logger = JsonlTraceLogger(Path("logs") / f"snapshot_{session_id}.jsonl")
            facade = await self._build_facade(mode, options=options, trace_logger=trace_logger, session_id=session_id)
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
        trace_logger = JsonlTraceLogger(Path("logs") / f"hub_inventory_{session_id}.jsonl")
        facade = await self._build_facade(mode, options=options, trace_logger=trace_logger, session_id=session_id)
        if not isinstance(facade, VendorFusionFacade):
            raise RuntimeError("hub inventory requires the Fusion CRUD script profile")
        result = await facade._execute_trusted_read_script_json(
            hub_inventory_script({"query": query, "max_results": max_results, "enrich": enrich})
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
        classification = classify_safe_change(operation, targets, policy, baseline_snapshot)
        if not baseline_complete:
            classification = {
                **classification,
                "allow_apply": False,
                "blocked": True,
                "classification": "incomplete_baseline",
                "risk_level": "high",
                "reasons": list(classification.get("reasons") or [])
                + ["The bounded baseline is incomplete; increase the explicit inspection budget before applying any change."],
            }
        preview_id = datetime.now(timezone.utc).strftime("preview_%Y%m%dT%H%M%S%fZ")
        preview_dir = options.output_dir / "safe_change_previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        preview_path = preview_dir / f"{preview_id}.json"
        payload = {
            "preview_id": preview_id,
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
            "negative_impact": False,
        }
        preview_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
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
            raise ValueError("save_after must be false; save only after a separate verified audit")
        if "/" in preview_id or "\\" in preview_id or ".." in preview_id:
            raise ValueError("preview_id must be a simple identifier")
        options = options or SessionOptions(mode=mode, project=project or "default")
        options.mode = mode
        options.project = project or options.project
        preview_path = options.output_dir / "safe_change_previews" / f"{preview_id}.json"
        if not preview_path.exists():
            raise FileNotFoundError(preview_path)
        preview = json.loads(preview_path.read_text(encoding="utf-8"))
        operation = normalize_operation(preview["operation"])
        classification = dict(preview.get("classification") or {})
        if classification.get("blocked"):
            return _aborted_change(preview, "preview_blocked", "Review the preview classification and adjust targets/policy before applying.")
        before_payload = _read_json_path(Path(preview["before_snapshot_path"]))
        before_snapshot = before_payload.get("snapshot", before_payload)
        if not _snapshot_is_complete(before_snapshot):
            return _aborted_change(
                preview,
                "incomplete_baseline",
                "No change was applied. Capture a complete baseline with a larger explicit budget before retrying.",
            )
        if operation == "delete":
            if not confirm_destructive:
                return _aborted_change(preview, "confirm_destructive_required", "Set confirm_destructive=true only after reviewing the baseline and targets.")
            if batch_size > 5:
                return _aborted_change(preview, "delete_batch_too_large", "Run the first destructive batch with batch_size<=5.")
            before = before_payload
            batch_targets = list(preview.get("targets") or [])[: max(1, int(batch_size))]
            session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            if mode == "mock":
                applied = {"success": True, "deleted": [], "deleted_count": 0, "skipped": []}
            else:
                trace_logger = JsonlTraceLogger(Path("logs") / f"safe_change_delete_{session_id}.jsonl")
                facade = await self._build_facade(mode, options=options, trace_logger=trace_logger, session_id=session_id)
                if not isinstance(facade, VendorFusionFacade):
                    return _aborted_change(preview, "unsupported_facade", "Safe delete apply requires the Fusion CRUD script profile.")
                applied = await facade._execute_script_json(safe_delete_apply_script({"targets": batch_targets}))
            if int(applied.get("deleted_count", 0)) == 0:
                return {
                    **preview,
                    "applied": applied,
                    "status": "aborted_before_verification",
                    "negative_impact": False,
                    "abort_reason": "no_delete_targets_applied",
                    "recovery_instructions": "No scoped delete target was applied. Review skipped targets, use component/body keys, and rerun preview.",
                }
            after = await self.compact_snapshot(
                project=options.project,
                mode=mode,
                options=options,
                max_occurrences=int(preview.get("policy", {}).get("max_occurrences", 500)),
                max_bodies=int(preview.get("policy", {}).get("max_bodies", 500)),
                include_transforms=bool(preview.get("policy", {}).get("include_transforms", False)),
                max_entities_visited=int(preview.get("policy", {}).get("max_entities_visited", 1000)),
                deadline_ms=int(preview.get("policy", {}).get("deadline_ms", 1500)),
                max_response_bytes=int(preview.get("policy", {}).get("max_response_bytes", 1024 * 1024)),
                label="after",
            )
            if not _snapshot_is_complete(after.get("snapshot", after)):
                return {
                    **preview,
                    "applied": applied,
                    "status": "applied_unverified",
                    "verification_complete": False,
                    "negative_impact": False,
                    "after_snapshot_path": after["snapshot_path"],
                    "abort_reason": "incomplete_readback",
                    "recovery_instructions": "Do not save. The bounded readback was incomplete; inspect with a larger explicit budget before deciding whether to undo.",
                }
            diff = diff_snapshots(before.get("snapshot", before), after.get("snapshot", after))
            if diff["negative_impact"]:
                return {
                    **preview,
                    **diff,
                    "applied": applied,
                    "status": "aborted_after_verification",
                    "abort_reason": "visible_snapshot_regression",
                    "after_snapshot_path": after["snapshot_path"],
                    "recovery_instructions": "Do not save. Use Fusion Undo for this destructive batch, then rerun fusion_agent_compact_snapshot.",
                }
            return {
                **preview,
                **diff,
                "applied": applied,
                "status": "applied_verified",
                "after_snapshot_path": after["snapshot_path"],
                "recovery_instructions": "",
            }
        if operation in {"move", "componentize"}:
            return _aborted_change(preview, f"{operation}_execution_not_implemented", "Use preview output to build a specialized reversible workflow.")
        if operation != "visibility":
            return _aborted_change(preview, "unsupported_operation", "Only reversible visibility apply is currently executable.")

        before = before_payload
        batch_targets = list(preview.get("targets") or [])[: max(1, int(batch_size))]
        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        applied: dict
        if mode == "mock":
            applied = {"success": True, "changed": [], "changed_count": 0}
        else:
            trace_logger = JsonlTraceLogger(Path("logs") / f"safe_change_apply_{session_id}.jsonl")
            facade = await self._build_facade(mode, options=options, trace_logger=trace_logger, session_id=session_id)
            if not isinstance(facade, VendorFusionFacade):
                return _aborted_change(preview, "unsupported_facade", "Safe-change apply requires the Fusion CRUD script profile.")
            applied = await facade._execute_script_json(safe_visibility_apply_script({"targets": batch_targets}))
        after = await self.compact_snapshot(
            project=options.project,
            mode=mode,
            options=options,
            max_occurrences=int(preview.get("policy", {}).get("max_occurrences", 500)),
            max_bodies=int(preview.get("policy", {}).get("max_bodies", 500)),
            include_transforms=bool(preview.get("policy", {}).get("include_transforms", False)),
            max_entities_visited=int(preview.get("policy", {}).get("max_entities_visited", 1000)),
            deadline_ms=int(preview.get("policy", {}).get("deadline_ms", 1500)),
            max_response_bytes=int(preview.get("policy", {}).get("max_response_bytes", 1024 * 1024)),
            label="after",
        )
        if not _snapshot_is_complete(after.get("snapshot", after)):
            return {
                **preview,
                "applied": applied,
                "status": "applied_unverified",
                "verification_complete": False,
                "negative_impact": False,
                "after_snapshot_path": after["snapshot_path"],
                "abort_reason": "incomplete_readback",
                "recovery_instructions": "Do not save. The bounded readback was incomplete; inspect with a larger explicit budget before deciding whether to undo.",
            }
        diff = diff_snapshots(before.get("snapshot", before), after.get("snapshot", after))
        if diff["negative_impact"]:
            return {
                **preview,
                **diff,
                "applied": applied,
                "status": "aborted_after_verification",
                "abort_reason": "visible_snapshot_regression",
                "after_snapshot_path": after["snapshot_path"],
                "recovery_instructions": "Do not save. Use Fusion Undo for the last batch, then rerun fusion_agent_compact_snapshot.",
            }
        return {
            **preview,
            **diff,
            "applied": applied,
            "status": "applied_verified",
            "after_snapshot_path": after["snapshot_path"],
            "recovery_instructions": "",
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
            policy = ToolPolicy.from_manifest(VENDOR_FACADE_NATIVE_TOOLS & manifest_names)
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

        policy = ToolPolicy.from_manifest(set(MOCK_FACADE_NATIVE_MAP.values()) & manifest_names)
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
            "planned_features": sum(len(component.features) for component in spec.components),
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


def _aborted_change(preview: dict, reason: str, recovery: str) -> dict:
    return {
        **preview,
        "status": "aborted_before_apply",
        "negative_impact": False,
        "abort_reason": reason,
        "recovery_instructions": recovery,
    }
