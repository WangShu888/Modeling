from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zdong.app import drawing_parser
from zdong.app.drawing_parser import DrawingParser
from zdong.app.models import AssetRecord, SourceBundle


def make_bundle(path: Path) -> SourceBundle:
    return SourceBundle(
        project_id="proj_0001",
        request_id="req_0001",
        version_id="ver_0001",
        prompt="build from drawing",
        source_mode_hint="cad_to_bim",
        assets=[
            AssetRecord(
                asset_id="asset_0001",
                filename=path.name,
                media_type="application/octet-stream",
                description="dwg source",
                path=str(path),
                extension=path.suffix.lower(),
            )
        ],
        form_fields={},
    )


def test_build_odafc_command_uses_wine_and_xvfb_for_windows_converter_on_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = DrawingParser(workspace_root=Path("/tmp/jianmo"))
    converter_path = Path("/tmp/ODAFileConverter.exe")

    monkeypatch.setattr(drawing_parser.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        drawing_parser.shutil,
        "which",
        lambda name: {
            "wine": "/usr/bin/wine",
            "xvfb-run": "/usr/bin/xvfb-run",
        }.get(name),
    )

    command = parser._build_odafc_command(converter_path)

    assert command == [
        "/usr/bin/xvfb-run",
        "-a",
        "/usr/bin/wine",
        str(converter_path),
    ]


def test_convert_dwg_to_dxf_runs_converter_and_returns_generated_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "sample.dwg"
    source_path.write_bytes(b"AC1032" + b"\0" * 64)
    parser = DrawingParser(workspace_root=tmp_path)

    monkeypatch.setattr(parser, "_find_odafc_executable", lambda: Path("/opt/ODAFileConverter"))
    monkeypatch.setattr(parser, "_build_odafc_command", lambda _path: ["converter"])

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        assert command[0] == "converter"
        assert command[1] == str(source_path.parent)
        assert command[3] == "ACAD2018"
        assert command[4] == "DXF"
        target_dir = Path(command[2])
        (target_dir / "sample.dxf").write_text("0\nEOF\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(drawing_parser.subprocess, "run", fake_run)

    converted = parser._convert_dwg_to_dxf(source_path, "AC1032 (R2018)")

    assert converted.name == "sample.dxf"
    assert converted.is_file()


def test_parse_reports_dwg_conversion_launch_failure_as_review_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "sample.dwg"
    source_path.write_bytes(b"AC1032" + b"\0" * 64)
    parser = DrawingParser(workspace_root=tmp_path)

    monkeypatch.setattr(parser, "_find_odafc_executable", lambda: Path("/opt/ODAFileConverter"))
    monkeypatch.setattr(parser, "_build_odafc_command", lambda _path: ["converter"])
    monkeypatch.setattr(
        drawing_parser.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("permission denied")),
    )

    parsed = parser.parse(make_bundle(source_path))

    assert parsed.asset_kinds == ["cad"]
    assert any(item.category == "dwg_conversion_failed" for item in parsed.pending_review)
    assert any("Unable to launch ODA File Converter" in issue for issue in parsed.unresolved_entities)
