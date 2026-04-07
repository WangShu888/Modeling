import shutil
from pathlib import Path

import fitz
from fastapi.testclient import TestClient

from jianmo.app import main


def make_client(name: str) -> TestClient:
    runtime_root = Path.cwd() / "test_runtime" / name
    shutil.rmtree(runtime_root, ignore_errors=True)
    runtime_root.mkdir(parents=True, exist_ok=True)
    app = main.create_app(runtime_root=runtime_root)
    return TestClient(app)


def make_client_for_runtime(runtime_root: Path) -> TestClient:
    runtime_root.mkdir(parents=True, exist_ok=True)
    app = main.create_app(runtime_root=runtime_root)
    return TestClient(app)


def build_pdf_bytes(path: Path) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((48, 64), "Standard Floor")
    page.insert_text((48, 90), "3600 mm")
    doc.save(path)
    doc.close()
    return path.read_bytes()


def test_project_asset_request_and_version_flow() -> None:
    client = make_client("api_flow")
    runtime_root = Path.cwd() / "test_runtime" / "api_flow"

    create_response = client.post(
        "/api/projects",
        json={"name": "API 测试项目", "building_type": "office", "region": "CN-SZ"},
    )
    assert create_response.status_code == 200
    project = create_response.json()

    upload_response = client.post(
        f"/api/projects/{project['project_id']}/assets",
        files={
            "file": (
                "facade.pdf",
                build_pdf_bytes(runtime_root / "facade.pdf"),
                "application/pdf",
            )
        },
        data={"description": "立面参考 PDF"},
    )
    assert upload_response.status_code == 200
    asset = upload_response.json()
    assert asset["filename"] == "facade.pdf"
    assert asset["project_id"] == project["project_id"]

    assets_response = client.get(f"/api/projects/{project['project_id']}/assets")
    assert assets_response.status_code == 200
    assert assets_response.json()[0]["asset_id"] == asset["asset_id"]

    request_response = client.post(
        f"/api/projects/{project['project_id']}/requests",
        json={
            "prompt": "生成一栋 8 层办公楼，首层层高 4.5m，标准层层高 3.9m，输出 IFC。",
            "building_type": "office",
            "region": "CN-SZ",
            "floors": 8,
            "standard_floor_height_m": 3.9,
            "first_floor_height_m": 4.5,
            "site_area_sqm": 5600,
            "far": 3.2,
            "asset_ids": [asset["asset_id"]],
        },
    )
    assert request_response.status_code == 200
    request = request_response.json()
    assert request["asset_ids"] == [asset["asset_id"]]

    parse_response = client.post(
        f"/api/projects/{project['project_id']}/requests/{request['request_id']}/parse"
    )
    assert parse_response.status_code == 200
    parsed = parse_response.json()
    assert parsed["assets_count"] == 1
    assert parsed["asset_kinds"] == ["pdf"]
    assert parsed["dimension_entities"] >= 1
    assert parsed["unresolved_entities"] == []

    modeling_response = client.post(
        f"/api/projects/{project['project_id']}/requests/{request['request_id']}/run"
    )
    assert modeling_response.status_code == 200
    snapshot = modeling_response.json()

    assert snapshot["design_intent"]["building_type"] == "office"
    assert snapshot["source_bundle"]["request_id"] == request["request_id"]
    assert snapshot["source_bundle"]["assets"][0]["asset_id"] == asset["asset_id"]

    request_detail_response = client.get(
        f"/api/projects/{project['project_id']}/requests/{request['request_id']}"
    )
    assert request_detail_response.status_code == 200
    assert request_detail_response.json()["latest_version_id"] == snapshot["source_bundle"]["version_id"]

    version_response = client.get(
        f"/api/projects/{project['project_id']}/versions/{snapshot['source_bundle']['version_id']}"
    )
    assert version_response.status_code == 200
    assert version_response.json()["source_bundle"]["version_id"] == snapshot["source_bundle"]["version_id"]


def test_sqlite_persistence_survives_app_recreation() -> None:
    runtime_root = Path.cwd() / "test_runtime" / "api_persistence"
    shutil.rmtree(runtime_root, ignore_errors=True)
    client_a = make_client_for_runtime(runtime_root)

    create_response = client_a.post(
        "/api/projects",
        json={"name": "持久化测试项目", "building_type": "residential", "region": "CN-SH"},
    )
    assert create_response.status_code == 200
    project = create_response.json()

    request_response = client_a.post(
        f"/api/projects/{project['project_id']}/requests",
        json={
            "prompt": "生成一栋 6 层住宅楼，标准层层高 3.0m，输出 IFC。",
            "building_type": "residential",
            "region": "CN-SH",
            "floors": 6,
            "standard_floor_height_m": 3.0,
            "first_floor_height_m": 3.3,
            "site_area_sqm": 2800,
            "far": 1.8,
        },
    )
    assert request_response.status_code == 200
    request = request_response.json()

    run_response = client_a.post(
        f"/api/projects/{project['project_id']}/requests/{request['request_id']}/run"
    )
    assert run_response.status_code == 200
    snapshot = run_response.json()

    client_b = make_client_for_runtime(runtime_root)

    projects_response = client_b.get("/api/projects")
    assert projects_response.status_code == 200
    assert any(item["project_id"] == project["project_id"] for item in projects_response.json())

    request_detail_response = client_b.get(
        f"/api/projects/{project['project_id']}/requests/{request['request_id']}"
    )
    assert request_detail_response.status_code == 200
    assert request_detail_response.json()["latest_version_id"] == snapshot["source_bundle"]["version_id"]

    version_response = client_b.get(
        f"/api/projects/{project['project_id']}/versions/{snapshot['source_bundle']['version_id']}"
    )
    assert version_response.status_code == 200
    assert version_response.json()["source_bundle"]["request_id"] == request["request_id"]


def test_frontend_index_is_served() -> None:
    client = make_client("api_frontend")
    response = client.get("/")
    assert response.status_code == 200
    assert "建筑自动建模系统 MVP" in response.text


def test_dev_server_defaults_to_port_3000(monkeypatch) -> None:
    monkeypatch.delenv("JIANMO_APP_HOST", raising=False)
    monkeypatch.delenv("JIANMO_APP_PORT", raising=False)

    host, port = main.get_dev_server_config()

    assert host == "0.0.0.0"
    assert port == 3000


def test_run_dev_server_uses_configured_host_and_port(monkeypatch) -> None:
    monkeypatch.setenv("JIANMO_APP_HOST", "127.0.0.1")
    monkeypatch.setenv("JIANMO_APP_PORT", "3000")

    calls: list[dict[str, object]] = []

    def fake_run(
        app_path: str,
        host: str,
        port: int,
        reload: bool,
        reload_dirs: list[str],
    ) -> None:
        calls.append(
            {
                "app_path": app_path,
                "host": host,
                "port": port,
                "reload": reload,
                "reload_dirs": reload_dirs,
            }
        )

    monkeypatch.setattr(main.uvicorn, "run", fake_run)

    main.run_dev_server()

    assert calls == [
        {
            "app_path": "jianmo.app.main:app",
            "host": "127.0.0.1",
            "port": 3000,
            "reload": True,
            "reload_dirs": [str(main.app_root)],
        }
    ]
