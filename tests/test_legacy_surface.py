"""Compatibility lock for the 31-tool public surface recovered from 0.1.0."""

from __future__ import annotations

import hashlib
import json

from fusion_agent_mcp.server import list_tool_definitions


LEGACY_SCHEMA_HASHES = {
    "fusion_agent_capture_viewport": "95e6084194e3151cead39a4bff0799286e87b98378d828cc94242fa4c0306d1b",
    "fusion_agent_discover_tools": "d5e6bef98c3b05e7e5c71b93e1d63440bd3d736d2f4f8d32cd017f127fdf047f",
    "fusion_agent_doctor": "d746974fa9afd5e951f76f9af38954b0ad7f436f2120dc974da65e5ee39f856f",
    # CadSpec v2 intentionally extends these two inputs with an exclusive
    # prompt/spec_json choice while retaining every legacy prompt field.
    "fusion_agent_dry_run_session": "183a3ab0797142f8035701aabf8427f2cee61f06fdf280085ce390fb5a51f2a4",
    "fusion_agent_export_spec_json": "7733a3184e47da097235e0ed9d9ec5ff3869b9e053c3b3e9f714b9712f7bec84",
    "fusion_agent_hub_inventory": "d1f4171db617980bfbe4ac9db93e95c0947010393dc01c4539196dbcdf422a22",
    "fusion_agent_list_benchmarks": "d746974fa9afd5e951f76f9af38954b0ad7f436f2120dc974da65e5ee39f856f",
    "fusion_agent_list_sessions": "992fc65b90ff75f1eb8e2ac43a8173b019346061902debb082d2e629b36d472f",
    "fusion_agent_memory_list_project": "3a960332eb096b6113061734b5d1cc9be2e6bb79cc8f7974eb47207bdfe75c20",
    "fusion_agent_memory_search": "571b5d7085a5bd81bd6e61a31c3a593fef9114c4c703aab9ef347c338c785a88",
    # Memory v2 adds source/provenance controls and taint-safe record kinds.
    "fusion_agent_memory_write": "feebbaab96f8ff15720e871b23d334897425c3f9a00e8a8a870b0a6ecac6e465",
    "fusion_agent_plan_spec": "aa58f8991072f0472c16d2f52ac4df525d6652d23cb494934adaa2c4add0a212",
    # Endpoint policy removes model-supplied probe URLs in every profile.
    "fusion_agent_probe": "d746974fa9afd5e951f76f9af38954b0ad7f436f2120dc974da65e5ee39f856f",
    "fusion_agent_propose_mapping": "d746974fa9afd5e951f76f9af38954b0ad7f436f2120dc974da65e5ee39f856f",
    "fusion_agent_read_manifest": "fb4c5c211fac3b3d72da77484aeb2763d51b278c78c1761e5984ff4b1c91cb0f",
    # CadSpec v2 persists typed execution.json and independent readback.json artifacts.
    "fusion_agent_read_session_artifact": "fc2a017e8e533425045c189733022e6be21f993449b7478b5a95259ae6134ebd",
    "fusion_agent_read_trace": "60a557814303a0ec40c56f8e25ef8784a85397bf5dbe89a469c311aead6a8cdd",
    "fusion_agent_readiness_report": "d746974fa9afd5e951f76f9af38954b0ad7f436f2120dc974da65e5ee39f856f",
    "fusion_agent_run_session": "8a9f541add785a18895561890c279b68b6774e8e2e2b28a3fd24e46018a32490",
    "fusion_agent_safe_change_apply": "0247cd714e5dc5767b1bac3559f3aaf2492e5788cddf052d3c071095c610a3fd",
    "fusion_agent_safe_change_preview": "0403bcada14c6c00fb095b9005f85fedd288798f09bbeed7d47f2cbc4f563ece",
    "fusion_agent_session_health": "d5e6bef98c3b05e7e5c71b93e1d63440bd3d736d2f4f8d32cd017f127fdf047f",
    "fusion_agent_skills_get": "c70d2daf71667bd20bdb983d31b4f01657a3308970b3c3d0d7f3bcf3b5d9bf20",
    "fusion_agent_skills_list": "d746974fa9afd5e951f76f9af38954b0ad7f436f2120dc974da65e5ee39f856f",
    "fusion_agent_skills_rank": "b2b960ab9bf50808112135e9d0fba74f1eb97461ef3fbb7e90a117ef76a5ef3f",
    "fusion_agent_validate_spec": "973c1a236c76a87bc6bc811b187e40cc1dcf0af3bceba07d579bced52435dfdf",
    "fusion_agent_verify_active_design": "28524b2f5d97453bf1f0ccb90053c3dc78b4a115ae6a9c361202b0bf3f7e40ad",
}

LEGACY_EXPANDED_SCHEMAS = {
    "fusion_agent_inspect": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "mode": {"type": "string", "enum": ["mock", "real"], "default": "mock"},
        },
        "required": [],
    },
    "fusion_agent_compact_snapshot": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "mode": {"type": "string", "enum": ["mock", "real"], "default": "real"},
            "project": {"type": "string"},
            "max_occurrences": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100000,
                "default": 500,
            },
            "max_bodies": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100000,
                "default": 500,
            },
            "include_transforms": {"type": "boolean", "default": False},
        },
        "required": ["project"],
    },
    "fusion_agent_run_benchmark": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "suite": {"type": "string"},
            "mode": {"type": "string", "enum": ["mock", "real"], "default": "mock"},
            "dry_run": {"type": "boolean", "default": False},
            "project": {"type": "string"},
        },
        "required": [],
    },
    "fusion_agent_read_benchmark_report": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"path": {"type": "string"}},
        "required": [],
    },
}


def _schema_hash(schema: dict) -> str:
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_legacy_31_tool_surface_is_compatibility_locked() -> None:
    definitions = {tool.name: tool for tool in list_tool_definitions("all")}
    legacy_names = set(LEGACY_SCHEMA_HASHES) | set(LEGACY_EXPANDED_SCHEMAS)

    assert len(legacy_names) == 31
    assert legacy_names.issubset(definitions)
    for name, expected_hash in LEGACY_SCHEMA_HASHES.items():
        assert _schema_hash(definitions[name].inputSchema) == expected_hash, name

    # P2 intentionally adds optional benchmark fields. Every 0.1.0 property and
    # requirement remains byte-for-byte compatible inside the expanded schema.
    for name, legacy in LEGACY_EXPANDED_SCHEMAS.items():
        current = definitions[name].inputSchema
        assert current["type"] == legacy["type"]
        assert current.get("required", []) == legacy["required"]
        for property_name, property_schema in legacy["properties"].items():
            assert current["properties"][property_name] == property_schema
