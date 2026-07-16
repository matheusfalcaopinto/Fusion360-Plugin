"""Bounded repair loop."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agent_core.executor import ExecutionContext
from cad_spec.models import CadSpec
from telemetry.trace import JsonlTraceLogger
from verifier.geometry import GeometryVerifier
from verifier.result_models import (
    DecisionStatus,
    FailureCode,
    VerificationIssue,
    VerificationResult,
)


class RepairAttempt(BaseModel):
    """One repair attempt record."""

    attempt: int
    code: FailureCode
    action: str
    success: bool = False
    action_applied: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class RepairLoop:
    """Run bounded verification and repair cycles."""

    def __init__(
        self,
        verifier: GeometryVerifier,
        max_attempts_per_transaction: int = 2,
        max_total_attempts: int = 5,
        executor: Any | None = None,
        trace_logger: JsonlTraceLogger | None = None,
        session_id: str = "standalone",
    ) -> None:
        self.verifier = verifier
        self.max_attempts_per_transaction = max_attempts_per_transaction
        self.max_total_attempts = max_total_attempts
        self.executor = executor
        self.trace_logger = trace_logger
        self.session_id = session_id
        self.attempts: list[RepairAttempt] = []

    async def run(
        self, spec: CadSpec, context: ExecutionContext | None = None
    ) -> VerificationResult:
        """Verify and apply bounded repairs when possible."""

        verification = await self.verifier.verify(spec)
        attempt = 0
        action_counts: dict[str, int] = defaultdict(int)

        # Incomplete evidence is not a failed assertion and must never become
        # authority to mutate.  Only a conclusive failed verdict may enter a
        # repair recipe.
        while (
            verification.status is DecisionStatus.FAILED
            and attempt < self.max_total_attempts
        ):
            issue = verification.issues[0]
            action = self._action_for(issue.code)
            if action == "stop_no_safe_recipe":
                self._record_attempt(
                    attempt + 1,
                    issue,
                    action,
                    success=False,
                    applied=False,
                    spec=spec,
                    reason="no_safe_recipe",
                )
                break
            if action_counts[action] >= self.max_attempts_per_transaction:
                self._record_attempt(
                    attempt + 1,
                    issue,
                    action,
                    success=False,
                    applied=False,
                    spec=spec,
                    reason="max_attempts_per_transaction_reached",
                )
                break

            action_counts[action] += 1
            if action == "inspect_units":
                applied = await self._repair_unit_mismatch(spec, issue)
            elif action == "activate_component":
                applied = await self._repair_activate_component(spec, context, issue)
            elif action == "replay_features":
                applied = await self._repair_replay_features(spec, context)
            elif action == "replay_exports":
                applied = await self._repair_replay_exports(spec, issue, context)
            else:
                applied = False

            attempt += 1
            self._record_attempt(
                attempt, issue, action, success=applied, applied=applied, spec=spec
            )
            if not applied:
                break

            verification = await self.verifier.verify(spec)

        return verification

    def _record_attempt(
        self,
        attempt: int,
        issue: VerificationIssue,
        action: str,
        *,
        success: bool,
        applied: bool,
        spec: CadSpec,
        reason: str | None = None,
    ) -> None:
        attempt_record = RepairAttempt(
            attempt=attempt,
            code=issue.code,
            action=action,
            success=success,
            action_applied=applied,
            details={
                "issue": issue.message,
                "reason": reason,
            },
        )
        self.attempts.append(attempt_record)
        if self.trace_logger:
            self.trace_logger.log(
                {
                    "session_id": self.session_id,
                    "event": "repair_action",
                    "code": issue.code,
                    "action": action,
                    "action_applied": applied,
                    "planned_components": len(spec.components),
                    "planned_features": sum(
                        len(component.features) for component in spec.components
                    ),
                }
            )

    def _action_for(self, code: FailureCode) -> str:
        if code == FailureCode.UNIT_MISMATCH:
            return "inspect_units"
        if code == FailureCode.OPEN_PROFILE:
            return "replay_features"
        if code == FailureCode.MISSING_PROFILE:
            return "replay_features"
        if code == FailureCode.WRONG_ACTIVE_COMPONENT:
            return "activate_component"
        if code == FailureCode.EXPORT_FAILED:
            return "replay_exports"
        return "stop_no_safe_recipe"

    async def _repair_unit_mismatch(
        self, spec: CadSpec, issue: VerificationIssue
    ) -> bool:
        # safe default: do not mutate geometry/parameters automatically
        issue_details = issue.details or {}
        expected = issue_details.get("expected")
        actual = issue_details.get("actual")
        _ = self._classify_unit_ratio(expected, actual)
        return False

    async def _repair_activate_component(
        self, spec: CadSpec, context: ExecutionContext | None, issue: VerificationIssue
    ) -> bool:
        if not self.executor or not spec.components:
            return False
        target = spec.components[0].name
        try:
            return bool(await self.executor.activate_component(target))
        except Exception:
            return False

    async def _repair_replay_features(
        self, spec: CadSpec, context: ExecutionContext | None
    ) -> bool:
        if not self.executor:
            return False
        try:
            execution_context = context or ExecutionContext()
            return bool(await self.executor.replay_features(spec, execution_context))
        except Exception:
            return False

    async def _repair_replay_exports(
        self, spec: CadSpec, issue: VerificationIssue, context: ExecutionContext | None
    ) -> bool:
        if not self.executor:
            return False
        try:
            missing = (issue.details or {}).get("missing", [])
            for export_path in missing or []:
                if not isinstance(export_path, str):
                    continue
                Path(export_path).parent.mkdir(parents=True, exist_ok=True)
            execution_context = context or ExecutionContext()
            return bool(await self.executor.replay_exports(spec, execution_context))
        except Exception:
            return False

    def _classify_unit_ratio(self, expected: Any, actual: Any) -> str:
        try:
            expected_values = [float(value) for value in (expected or [])]
            actual_values = [float(value) for value in (actual or [])]
        except (TypeError, ValueError):
            return "invalid_scale"

        if len(expected_values) != len(actual_values) or not expected_values:
            return "unknown_scale"
        ratios = [
            actual / expected
            for actual, expected in zip(actual_values, expected_values, strict=True)
            if expected
        ]
        for ratio in ratios:
            if abs(ratio - 10.0) <= 0.05:
                return "x10"
            if abs(ratio - 25.4) <= 0.1:
                return "x25.4"
            if abs(ratio - 0.1) <= 0.001:
                return "x0.1"
            if abs(ratio - (1 / 25.4)) <= 0.0001:
                return "x1/25.4"
        return "unknown_scale"
