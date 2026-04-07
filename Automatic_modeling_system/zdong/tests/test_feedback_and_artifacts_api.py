from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zdong.app.main import create_app
from zdong.app.store import InMemoryStore


def _create_version(client: TestClient) -> tuple[str, dict]:
    project = client.post(
        "/api/projects",
        json={"name": "Feedback API", "building_type": "office", "region": "CN-SH"},
    )
    assert project.status_code == 200
    project_id = project.json()["project_id"]

    version = client.post(
        f"/api/projects/{project_id}/modeling-requests",
        json={
            "prompt": "生成一个 6 层办公楼草案并输出 IFC。",
            "building_type": "office",
            "region": "CN-SH",
            "source_mode_hint": "text_only",
            "floors": 6,
            "standard_floor_height_m": 3.6,
            "first_floor_height_m": 4.2,
            "site_area_sqm": 2400,
            "far": 2.0,
            "assets": [],
        },
    )
    assert version.status_code == 200
    return project_id, version.json()


def test_feedback_endpoint_records_feedback_file(tmp_path: Path) -> None:
    app = create_app(
        runtime_root=tmp_path / "runtime",
        store=InMemoryStore(),
        export_root=tmp_path / "exports",
        asset_root=tmp_path / "assets",
    )
    client = TestClient(app)
    project_id, version = _create_version(client)
    version_id = version["source_bundle"]["version_id"]

    response = client.post(
        f"/api/projects/{project_id}/versions/{version_id}/feedbacks",
        json={"topic": "issue", "comment": "窗的替换结果需要复核。"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["feedback_id"].startswith("fbk_")

    feedback_dir = tmp_path / "exports" / project_id / version_id / "feedback"
    saved_file = feedback_dir / f"{payload['feedback_id']}.json"
    assert saved_file.is_file()
    saved_payload = json.loads(saved_file.read_text(encoding="utf-8"))
    assert saved_payload["version_id"] == version_id
    assert saved_payload["topic"] == "issue"
    assert saved_payload["comment"] == "窗的替换结果需要复核。"


def test_artifact_endpoint_streams_exported_file(tmp_path: Path) -> None:
    app = create_app(
        runtime_root=tmp_path / "runtime",
        store=InMemoryStore(),
        export_root=tmp_path / "exports",
        asset_root=tmp_path / "assets",
    )
    client = TestClient(app)
    project_id, version = _create_version(client)
    version_id = version["source_bundle"]["version_id"]
    artifact_name = next(
        artifact["name"]
        for artifact in version["export_bundle"]["artifacts"]
        if artifact["name"] == "intent.json"
    )

    response = client.get(
        f"/api/projects/{project_id}/versions/{version_id}/artifacts/{artifact_name}"
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert '"project_id": "proj_' in response.text
