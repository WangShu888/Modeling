from __future__ import annotations

import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def load_agent_plan() -> dict:
    config_path = Path(__file__).resolve().parents[1] / "app" / "config" / "agent_plan.standard.json"
    return json.loads(config_path.read_text(encoding="utf-8"))


def test_standard_agent_plan_covers_all_required_modules() -> None:
    plan = load_agent_plan()

    required = set(plan["coverage_required"])
    covered = {
        module_id
        for agent in plan["agents"]
        for module_id in agent["assigned_modules"]
    }

    assert covered == required


def test_standard_agent_plan_has_closed_primary_delivery_chain() -> None:
    plan = load_agent_plan()

    chain = plan["primary_delivery_chain"]
    contracts = {
        (contract["from"], contract["to"]): contract["object"]
        for contract in plan["handoff_contracts"]
    }

    assert len(chain) == 7
    assert chain[0] == "agent_intake_asset"
    assert chain[-1] == "agent_workbench_feedback"

    for upstream, downstream in zip(chain, chain[1:]):
        assert (upstream, downstream) in contracts


def test_drawing_parser_agent_is_configured_to_continue_existing_results() -> None:
    plan = load_agent_plan()

    drawing_agent = next(agent for agent in plan["agents"] if agent["agent_id"] == "agent_drawing_parser")

    assert drawing_agent["execution_mode"] == "continue_existing_results"
    assert "zdong/app/drawing_parser.py" in drawing_agent["existing_assets"]
    assert "zdong/tests/test_dwg_conversion.py" in drawing_agent["existing_assets"]


def test_validation_delivery_agent_owns_release_gate_outputs() -> None:
    plan = load_agent_plan()

    validation_agent = next(
        agent for agent in plan["agents"] if agent["agent_id"] == "agent_validation_delivery"
    )

    assert validation_agent["assigned_modules"] == ["4.7", "4.8", "4.9"]
    assert "model.ifc" in validation_agent["outputs"]
    assert "validation.json" in validation_agent["outputs"]
    assert "intent.json" in validation_agent["outputs"]
