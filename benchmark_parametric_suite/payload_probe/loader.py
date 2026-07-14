"""Strict loader for the protected-payload experiment matrix."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import HistoricalObservation, PayloadProbeMatrix, PayloadTarget


EXPECTED_TARGETS = (
    20480,
    24576,
    28672,
    31744,
    32512,
    32767,
    32768,
    32769,
    33024,
    36864,
    37976,
    40960,
)
REQUIRED_ABORT_CONDITIONS = {
    "partial",
    "contaminated",
    "document_drift",
    "restoration_failure",
}


class PayloadMatrixError(ValueError):
    pass


def load_probe_matrix(path: str | Path) -> PayloadProbeMatrix:
    matrix_path = Path(path).resolve()
    try:
        payload = json.loads(matrix_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PayloadMatrixError(f"cannot read payload-probe matrix: {exc}") from exc
    if not isinstance(payload, dict):
        raise PayloadMatrixError("payload-probe matrix root must be an object")
    allowed = {
        "schema_version",
        "experiment_id",
        "payload_metric",
        "warmups",
        "repetitions",
        "seed",
        "process_modes",
        "targets",
        "historical_observations",
        "safety",
    }
    _reject_extra(payload, allowed, "matrix")
    _require(payload.get("schema_version") == "fusion_executor_payload_probe.v1", "unsupported schema_version")
    _require(_nonempty(payload.get("experiment_id")), "experiment_id must be non-empty")
    _require(payload.get("payload_metric") == "protected_utf8_bytes", "payload_metric must be protected_utf8_bytes")
    warmups = payload.get("warmups")
    _require(type(warmups) is int and warmups >= 0, "warmups must be a non-negative integer")
    repetitions = payload.get("repetitions")
    _require(type(repetitions) is int and repetitions > 0, "repetitions must be a positive integer")
    seed = payload.get("seed")
    _require(type(seed) is int, "seed must be an integer")
    process_modes = payload.get("process_modes")
    _require(
        process_modes == ["same_process", "fresh_process"],
        "process_modes must be exactly ['same_process', 'fresh_process']",
    )
    targets = _load_targets(payload.get("targets"))
    historical = _load_history(payload.get("historical_observations"))
    safety = payload.get("safety")
    _require(isinstance(safety, dict), "safety must be an object")
    _reject_extra(
        safety,
        {"retry_policy", "mutating_dispatches_per_trial", "abort_on"},
        "safety",
    )
    _require(safety.get("retry_policy") == "never", "retry_policy must be never")
    _require(
        safety.get("mutating_dispatches_per_trial") == 1,
        "mutating_dispatches_per_trial must be exactly one",
    )
    abort_on = safety.get("abort_on")
    _require(isinstance(abort_on, list) and all(_nonempty(item) for item in abort_on), "abort_on must be a string list")
    _require(REQUIRED_ABORT_CONDITIONS <= set(abort_on), "abort_on is missing a fail-closed condition")
    return PayloadProbeMatrix(
        schema_version=payload["schema_version"],
        experiment_id=payload["experiment_id"],
        payload_metric=payload["payload_metric"],
        warmups=warmups,
        repetitions=repetitions,
        seed=seed,
        process_modes=tuple(process_modes),
        targets=tuple(targets),
        historical_observations=tuple(historical),
        retry_policy=safety["retry_policy"],
        mutating_dispatches_per_trial=1,
        abort_on=tuple(abort_on),
    )


def _load_targets(value: Any) -> list[PayloadTarget]:
    _require(isinstance(value, list), "targets must be an array")
    targets: list[PayloadTarget] = []
    for index, item in enumerate(value):
        _require(isinstance(item, dict), f"targets[{index}] must be an object")
        _reject_extra(item, {"id", "target_protected_bytes"}, f"targets[{index}]")
        _require(_nonempty(item.get("id")), f"targets[{index}].id must be non-empty")
        size = item.get("target_protected_bytes")
        _require(type(size) is int and size > 0, f"targets[{index}].target_protected_bytes must be positive")
        targets.append(PayloadTarget(id=item["id"], target_protected_bytes=size))
    sizes = tuple(target.target_protected_bytes for target in targets)
    _require(sizes == EXPECTED_TARGETS, f"targets must match the canonical ordered matrix {EXPECTED_TARGETS}")
    _require(len({target.id for target in targets}) == len(targets), "target ids must be unique")
    return targets


def _load_history(value: Any) -> list[HistoricalObservation]:
    _require(isinstance(value, list), "historical_observations must be an array")
    observations: list[HistoricalObservation] = []
    for index, item in enumerate(value):
        _require(isinstance(item, dict), f"historical_observations[{index}] must be an object")
        _reject_extra(
            item,
            {
                "protected_payload_bytes",
                "observation_label",
                "source",
                "eligible_as_expectation",
                "eligible_as_oracle",
            },
            f"historical_observations[{index}]",
        )
        _require(type(item.get("protected_payload_bytes")) is int, "historical size must be an integer")
        _require(_nonempty(item.get("observation_label")), "historical observation_label must be non-empty")
        _require(_nonempty(item.get("source")), "historical source must be non-empty")
        _require(item.get("eligible_as_expectation") is False, "history cannot be an expectation")
        _require(item.get("eligible_as_oracle") is False, "history cannot be an oracle")
        observations.append(HistoricalObservation(**item))
    sizes = {item.protected_payload_bytes for item in observations}
    _require({25468, 37976} <= sizes, "history must record the 25468 B and 37976 B observations")
    return observations


def _reject_extra(value: dict[str, Any], allowed: set[str], label: str) -> None:
    extras = sorted(set(value) - allowed)
    if extras:
        raise PayloadMatrixError(f"{label} has unknown fields: {', '.join(extras)}")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PayloadMatrixError(message)


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
