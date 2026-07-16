from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from fusion_agent_mcp import mcp_surface
from fusion_mcp_adapter import tool_result
from fusion_mcp_adapter.tool_result import ToolDefinition, ToolManifest, ToolResult
from verifier.result_models import (
    DecisionReasonCode,
    DecisionResult,
    DecisionStatus,
    EvidenceEnvelope,
    FailureCode,
    VerificationIssue,
    VerificationResult,
)


def _surface(**overrides: Any) -> mcp_surface.SurfaceSpec:
    values: dict[str, Any] = {
        "kind": "resource",
        "name": "coverage-resource",
        "profiles": ("normal",),
        "risk": "read",
        "data_class": "coverage",
        "resource_family": "coverage",
        "resource_path": (),
    }
    values.update(overrides)
    return mcp_surface.SurfaceSpec(**values)


def test_surface_registry_rejects_incomplete_or_unknown_declarations() -> None:
    with pytest.raises(ValueError, match="at least one profile"):
        _surface(profiles=())
    with pytest.raises(ValueError, match="unknown profiles"):
        _surface(profiles=("root",))
    with pytest.raises(ValueError, match="declare a family"):
        _surface(resource_family=None)
    with pytest.raises(ValueError, match="exact path"):
        _surface(resource_path=None)
    with pytest.raises(ValueError, match="schemas, handler, and projector"):
        _surface(
            kind="tool",
            resource_family=None,
            resource_path=None,
        )
    with pytest.raises(ValueError, match="projector text"):
        _surface(
            kind="prompt",
            resource_family=None,
            resource_path=None,
            prompt_workflow=None,
        )


def test_surface_authorization_rejects_malformed_and_ambiguous_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="fusion-agent scheme"):
        mcp_surface.authorize_resource("https://capabilities", "normal")
    with pytest.raises(FileNotFoundError):
        mcp_surface.authorize_resource("fusion-agent://capabilities#fragment", "normal")
    with pytest.raises(FileNotFoundError):
        mcp_surface.authorize_resource("fusion-agent://unknown", "normal")

    duplicate = mcp_surface._RESOURCE_SPECS[0]
    monkeypatch.setattr(
        mcp_surface,
        "_RESOURCE_SPECS",
        (duplicate, duplicate, *mcp_surface._RESOURCE_SPECS[1:]),
    )
    with pytest.raises(RuntimeError, match="ambiguous"):
        mcp_surface.authorize_resource("fusion-agent://capabilities", "normal")


@pytest.mark.parametrize(
    ("family", "segments", "query_fields"),
    [
        ("wrong", ("valid",), frozenset()),
        ("coverage", ("valid", "extra"), frozenset()),
        ("coverage", ("valid",), frozenset({"unexpected"})),
        ("coverage", (".",), frozenset()),
        ("coverage", ("..",), frozenset()),
        ("coverage", ("bad\\segment",), frozenset()),
        ("coverage", ("bad/segment",), frozenset()),
        ("coverage", ("bad\0segment",), frozenset()),
    ],
)
def test_resource_route_matching_rejects_unbound_identifiers(
    family: str,
    segments: tuple[str, ...],
    query_fields: frozenset[str],
) -> None:
    spec = _surface(
        resource_path=("{identifier}",),
        resource_query_fields=("offset",),
    )
    assert not mcp_surface._resource_route_matches(spec, family, segments, query_fields)


def test_resource_route_matching_requires_literal_equality_and_declared_shape() -> None:
    literal = _surface(resource_path=("summary",))
    assert not mcp_surface._resource_route_matches(
        literal, "coverage", ("details",), frozenset()
    )
    assert mcp_surface._resource_route_matches(
        literal, "coverage", ("summary",), frozenset()
    )
    no_shape = _surface(resource_path=())
    object.__setattr__(no_shape, "resource_path", None)
    assert not mcp_surface._resource_route_matches(
        no_shape, "coverage", (), frozenset()
    )


def test_surface_profile_error_and_prompt_validation_are_bounded() -> None:
    error = mcp_surface.SurfaceProfileError(
        kind="prompt",
        name="private-name",
        profile="normal",
        available_profiles=("benchmark",),
    )
    assert "private-name" not in str(error)
    assert error.code in str(error)

    with pytest.raises(KeyError, match="unknown Fusion Agent prompt"):
        mcp_surface.render_prompt("unknown", None, profile="normal")
    with pytest.raises(ValueError, match="missing required prompt arguments"):
        mcp_surface.render_prompt(
            "fusion-benchmark-case",
            {},
            profile="benchmark",
        )


def test_tool_manifest_migrates_v1_and_refreshes_schema_fingerprint() -> None:
    manifest = ToolManifest(
        schema_version=1,
        tools=[
            ToolDefinition(name="b", description="second"),
            ToolDefinition(name="a", description="first"),
        ],
    )
    original = manifest.fingerprint
    assert manifest.schema_version == 2
    assert len(original) == 64
    manifest.tools[0].description = "changed"
    assert manifest.refresh_fingerprint() != original


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"ok": False, "error": "failure"}, "failure"),
        ({"success": False, "error_message": "failure"}, "failure"),
        ({"ok": False, "message": {"ok": False, "error": "nested"}}, "nested"),
        ({"ok": False, "message": "plain failure"}, "plain failure"),
        ({"ok": False}, "Fusion operation returned a negative acknowledgement"),
    ],
)
def test_semantic_failure_parser_covers_all_negative_acknowledgements(
    payload: dict[str, Any], expected: str
) -> None:
    assert tool_result._semantic_failure_message(payload) == expected


def test_tool_result_parsing_bounds_depth_and_ignores_non_text_content() -> None:
    assert tool_result._semantic_failure_from_value({}, depth=6) is None
    assert tool_result._semantic_failure_from_value(7, depth=0) is None
    assert tool_result._semantic_failure_from_value("not json", depth=0) is None
    assert (
        tool_result._content_text(
            [
                "not-a-block",
                {"type": "image", "text": "ignored"},
                {"type": "text", "text": 7},
                {"type": "text", "text": "kept"},
            ]
        )
        == "kept"
    )

    positive = ToolResult.from_mcp(
        {
            "content": [{"type": "image", "data": "opaque"}],
            "safe": "value",
            "_meta": "not-a-dict",
        }
    )
    assert positive.ok
    assert positive.data == {"safe": "value"}


def _evidence(**overrides: Any) -> EvidenceEnvelope:
    values: dict[str, Any] = {
        "producer": "coverage",
        "complete": True,
        "counts_exact": True,
        "truncated": False,
        "stop_reason": "complete",
        "metrics_finite": True,
        "assertion_ids": ["body_count"],
        "assertion_count": 1,
        "evaluated_count": 1,
    }
    values.update(overrides)
    return EvidenceEnvelope(**values)


def test_evidence_conclusive_requires_every_completeness_dimension() -> None:
    complete = _evidence()
    assert complete.conclusive
    assert len(complete.sha256()) == 64
    for update in (
        {"complete": False},
        {"counts_exact": False},
        {"truncated": True},
        {"stop_reason": "deadline"},
        {"metrics_finite": False},
        {"evaluated_count": 0},
    ):
        assert not complete.model_copy(update=update).conclusive


def test_verification_result_infers_typed_defaults_and_helpers() -> None:
    passed = VerificationResult(passed=True)
    failed = VerificationResult(passed=False)
    incomplete = VerificationResult(
        passed=False,
        status=DecisionStatus.INCOMPLETE,
    )
    assert passed.reason_codes == [DecisionReasonCode.VERIFIED]
    assert failed.reason_codes == [DecisionReasonCode.ASSERTION_FAILED]
    assert incomplete.reason_codes == [DecisionReasonCode.INCOMPLETE_INSPECTION]
    assert VerificationResult.pass_result().metrics == {}

    issue = VerificationIssue(code=FailureCode.INCOMPLETE_INSPECTION, message="partial")
    evidence = _evidence(complete=False, evaluated_count=0)
    built = VerificationResult.incomplete_result(
        evidence=evidence,
        issues=[issue],
        metrics={"count": 1},
    )
    assert built.issues == [issue]
    assert built.metrics == {"count": 1}


def test_verification_result_rejects_status_reason_and_digest_inconsistency() -> None:
    with pytest.raises(ValidationError, match="passed may be true"):
        VerificationResult(passed=True, status=DecisionStatus.FAILED)

    with pytest.raises(ValidationError, match="decision must match"):
        VerificationResult(
            passed=False,
            status=DecisionStatus.FAILED,
            reason_codes=[DecisionReasonCode.ASSERTION_FAILED],
            decision=DecisionResult(
                status=DecisionStatus.INCOMPLETE,
                reason_codes=[DecisionReasonCode.ASSERTION_FAILED],
            ),
        )

    evidence = _evidence()
    with pytest.raises(ValidationError, match="digest does not match"):
        VerificationResult(
            passed=True,
            status=DecisionStatus.PASSED,
            reason_codes=[DecisionReasonCode.VERIFIED],
            evidence=evidence,
            decision=DecisionResult(
                status=DecisionStatus.PASSED,
                reason_codes=[DecisionReasonCode.VERIFIED],
                evidence_sha256="0" * 64,
            ),
        )
