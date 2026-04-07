from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zdong.app import windows_dwg_converter_service as converter_service


def test_convert_dwg_bytes_to_dxf_bytes_runs_odafc_and_returns_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        converter_service,
        "_find_odafc_executable",
        lambda: Path("C:/ODA/ODAFileConverter.exe"),
    )

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        assert command[0] == "C:/ODA/ODAFileConverter.exe"
        assert command[3] == "ACAD2018"
        assert command[4] == "DXF"
        input_dir = Path(command[1])
        output_dir = Path(command[2])
        input_name = command[7]
        assert (input_dir / input_name).is_file()
        (output_dir / Path(input_name).with_suffix(".dxf").name).write_text("0\nEOF\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(converter_service.subprocess, "run", fake_run)

    payload, filename = converter_service.convert_dwg_bytes_to_dxf_bytes(
        source_bytes=b"AC1032" + b"\0" * 64,
        filename="building-plan.dwg",
        output_version="ACAD2018",
    )

    assert filename == "building-plan.dxf"
    assert payload == b"0\nEOF\n"


def test_convert_dwg_bytes_to_dxf_bytes_reports_converter_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        converter_service,
        "_find_odafc_executable",
        lambda: Path("C:/ODA/ODAFileConverter.exe"),
    )
    monkeypatch.setattr(
        converter_service.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )

    with pytest.raises(RuntimeError, match="ODA File Converter failed: boom"):
        converter_service.convert_dwg_bytes_to_dxf_bytes(
            source_bytes=b"AC1032" + b"\0" * 64,
            filename="building-plan.dwg",
            output_version="ACAD2018",
        )
