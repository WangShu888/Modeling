from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import zdong.app.ifc_runtime as runtime


def _configure_runtime(monkeypatch, available_versions: dict[str, str]) -> None:
    versions = dict(available_versions)

    def fake_find_spec(name: str):
        return object() if name in versions else None

    def fake_version(name: str):
        if name in versions:
            return versions[name]
        raise runtime.metadata.PackageNotFoundError

    monkeypatch.setattr(runtime.importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(runtime.metadata, "version", fake_version)
    runtime.detect_ifc_runtime.cache_clear()


def test_detect_ifc_runtime_handles_missing_modules(monkeypatch) -> None:
    _configure_runtime(monkeypatch, {})
    info = runtime.detect_ifc_runtime()

    assert info.exporter == "text_ifc_fallback"
    assert info.validator == "semantic_pre_export_validator"
    assert info.schema == "IFC4"
    assert not info.formal_backend_ready
    assert all(not status.available and status.version is None for status in info.module_statuses)


def test_detect_ifc_runtime_reports_versions_for_available_tools(monkeypatch) -> None:
    _configure_runtime(monkeypatch, {"ifcopenshell": "0.8.0", "ifctester": "3.1.0"})
    info = runtime.detect_ifc_runtime()
    statuses = {status.name: status for status in info.module_statuses}

    assert info.exporter == "ifcopenshell_exporter"
    assert info.validator == "ifctester"
    assert info.formal_backend_ready
    assert statuses["ifcopenshell"].version == "0.8.0"
    assert statuses["ifctester"].version == "3.1.0"
    assert not statuses["ifcdiff"].available
