from __future__ import annotations

import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def load_runbook() -> dict:
    path = Path(__file__).resolve().parents[1] / "app" / "config" / "agent_runbook.standard.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_phase_a_uses_main_chain_agents_in_order() -> None:
    runbook = load_runbook()
    phase_a = next(phase for phase in runbook["phases"] if phase["phase_id"] == "phase_a_main_chain")

    assert phase_a["required_agents"] == [
        "agent_intake_asset",
        "agent_drawing_parser",
        "agent_intent_ai",
        "agent_rules_planner",
        "agent_bim_executor",
        "agent_validation_delivery",
    ]


def test_phase_b_only_starts_after_export() -> None:
    runbook = load_runbook()
    task_b1 = next(task for task in runbook["tasks"] if task["task_id"] == "task_b1_feedback_loop")

    assert task_b1["depends_on"] == ["task_a6_validate_and_export"]


def test_phase_a_release_gate_requires_formal_export_bundle_outputs() -> None:
    runbook = load_runbook()
    phase_a = next(phase for phase in runbook["phases"] if phase["phase_id"] == "phase_a_main_chain")
    export_gate = next(gate for gate in phase_a["release_gates"] if gate["gate_id"] == "gate_release_export")

    assert export_gate["owner_agent"] == "agent_validation_delivery"
    assert "model.ifc" in export_gate["required_outputs"]
    assert "validation.json" in export_gate["required_outputs"]
    assert "intent.json" in export_gate["required_outputs"]
    assert export_gate["block_on_fail"] is True


def test_drawing_parser_task_requires_continuing_existing_results() -> None:
    runbook = load_runbook()
    task_a2 = next(task for task in runbook["tasks"] if task["task_id"] == "task_a2_parse_drawings")

    assert task_a2["owner_agent"] == "agent_drawing_parser"
    assert task_a2["must_continue_existing_results"] is True
