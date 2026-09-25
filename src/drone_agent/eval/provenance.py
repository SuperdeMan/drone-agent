"""Audit exported source records independently of the console and flight verdicts (P0).

独立于页面与飞行判定，审计导出的来源记录（P0）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from drone_agent.fleet.provenance import RunProvenance, export
from drone_agent.mission.registry import Registry


def audit_view(view: dict, sha: str, *, planning_source: str, root: Path) -> dict:
    """Check every version and acquisition, preserving source and flight-result separation.

    核对每个版本和采集，保持来源与飞行结果分离。
    """
    problems, sources, seen_versions = [], [], set()
    mission_id = (view.get("mission") or view).get("mission_id")
    images = {}
    expected_scene = hashlib.sha256((root / "configs/scenarios/m2_campus_v2.yaml").read_bytes()).hexdigest()
    expected_platform = hashlib.sha256((root / "configs/platforms/px4_sitl_multirotor.yaml").read_bytes()).hexdigest()
    expected_prompt = hashlib.sha256((root / "src/drone_agent/planner/prompts/planner-v1.md").read_bytes()).hexdigest()
    expected_registry = Registry(root, scene=root / "configs/scenarios/m2_campus_v2.yaml").sha256
    for version in view.get("versions", []):
        number = version.get("version")
        if number in seen_versions:
            problems.append(f"duplicate_version:{number}")
        seen_versions.add(number)
        try:
            expected = export({"mission_id": mission_id, "version": number, "decision": version.get("decision")})
            if version.get("provenance") != expected:
                problems.append(f"projection_mismatch:{number}")
            value = RunProvenance.model_validate({k: v for k, v in expected.items() if k != "sha256"})
            if value.status != "recorded" or value.run is None:
                problems.append(f"missing_source:{number}")
                continue
            run = value.run
            if run.software_sha != sha or run.dirty_sha256 is not None:
                problems.append(f"wrong_revision:{number}")
            if run.execution_backend != "px4_sitl" or run.default_image_source != "sim_render":
                problems.append(f"wrong_backend:{number}")
            if (run.scenario_sha256, run.platform_sha256) != (expected_scene, expected_platform):
                problems.append(f"wrong_configuration:{number}")
            if run.registry_sha256 != expected_registry:
                problems.append(f"wrong_registry:{number}")
            required = "deterministic" if version.get("origin") == "replan" else planning_source
            if value.planning.source != required:
                problems.append(f"wrong_planning_source:{number}")
            if required != "deterministic" and (value.planning.prompt_sha256 != expected_prompt
                                                or not value.planning.model_id or not value.planning.input_sha256):
                problems.append(f"incomplete_model_binding:{number}")
            if "unknown" in value.analysis_sources or "legacy_unknown" in value.analysis_sources:
                problems.append(f"unknown_analysis_source:{number}")
            for image in value.imagery:
                if image.evidence_id in images:
                    problems.append(f"duplicate_acquisition:{image.evidence_id}")
                if image.run_id != run.run_id or image.source != "sim_render":
                    problems.append(f"wrong_image_source:{image.evidence_id}")
                images[image.evidence_id] = (number, image)
            sources.append(expected)
        except (ValueError, TypeError, KeyError) as error:
            problems.append(f"invalid_source:{number}:{type(error).__name__}")
    if not seen_versions or not mission_id:
        problems.append("missing_mission_versions")
    evidence = view.get("evidence", [])
    if len({e["evidence_id"] for e in evidence}) != len(evidence):
        problems.append("duplicate_evidence")
    if set(images) != {e["evidence_id"] for e in evidence}:
        problems.append("acquisition_coverage_mismatch")
    for item in evidence:
        recorded = images.get(item["evidence_id"])
        if recorded is None:
            continue
        number, image = recorded
        if (number, image.media_sha256) != (item["version"], item["sha256"]):
            problems.append(f"wrong_acquisition_binding:{item['evidence_id']}")
        if item.get("provenance") != image.model_dump(mode="json"):
            problems.append(f"evidence_projection_mismatch:{item['evidence_id']}")
    report = view.get("report")
    if report is not None and report.get("provenance") != sources:
        problems.append("report_source_mismatch")
    return {"status": "passed" if not problems else "failed", "mission_id": mission_id,
            "versions": len(seen_versions), "acquisitions": len(images), "planning_source": planning_source,
            "problems": problems, "projection_digests": [s["sha256"] for s in sources]}
