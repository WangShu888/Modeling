from pathlib import Path


WEB_ROOT = Path(__file__).resolve().parents[1] / "jianmo" / "web"


def test_vite_proxy_targets_backend_port_3000() -> None:
    vite_config = (WEB_ROOT / "vite.config.ts").read_text(encoding="utf-8")

    assert 'const backendUrl = "http://127.0.0.1:3000";' in vite_config
    assert '"/api": backendUrl' in vite_config
    assert '"/health": backendUrl' in vite_config


def test_vite_dev_server_listens_on_port_3001() -> None:
    vite_config = (WEB_ROOT / "vite.config.ts").read_text(encoding="utf-8")

    assert 'host: "0.0.0.0"' in vite_config
    assert "port: 3001" in vite_config


def test_vite_uses_polling_watchers_in_dev() -> None:
    vite_config = (WEB_ROOT / "vite.config.ts").read_text(encoding="utf-8")
    package_json = (WEB_ROOT / "package.json").read_text(encoding="utf-8")

    assert "usePolling: true" in vite_config
    assert "interval: 1000" in vite_config
    assert 'CHOKIDAR_USEPOLLING=1 CHOKIDAR_INTERVAL=1000 vite' in package_json
