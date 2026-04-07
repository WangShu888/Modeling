from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zdong.app import drawing_parser
from zdong.app.drawing_parser import DrawingParser


class _FakeResponse:
    def __init__(self, payload: bytes, headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def test_remote_converter_uploads_dwg_and_writes_returned_dxf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "sample.dwg"
    source_path.write_bytes(b"AC1032" + b"\0" * 64)
    parser = DrawingParser(workspace_root=tmp_path)

    monkeypatch.setenv("JIANMO_DWG_CONVERTER_URL", "http://windows-host:3010/convert/dwg-to-dxf")
    monkeypatch.setenv("JIANMO_DWG_CONVERTER_TOKEN", "secret-token")

    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        assert timeout == 180.0
        assert request.full_url == "http://windows-host:3010/convert/dwg-to-dxf"
        assert request.get_method() == "POST"
        headers = {key.lower(): value for key, value in request.header_items()}
        assert headers["x-source-filename"] == "sample.dwg"
        assert headers["x-output-version"] == "ACAD2018"
        assert headers["authorization"] == "Bearer secret-token"
        assert request.data == source_path.read_bytes()
        return _FakeResponse(
            b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n",
            headers={"Content-Type": "application/octet-stream", "X-Output-Filename": "remote-sample.dxf"},
        )

    monkeypatch.setattr(drawing_parser.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        parser,
        "_find_odafc_executable",
        lambda: (_ for _ in ()).throw(AssertionError("local converter should not be used")),
    )

    converted = parser._convert_dwg_to_dxf(source_path, "AC1032 (R2018)")

    assert converted.name == "remote-sample.dxf"
    assert converted.read_text(encoding="utf-8").startswith("0\nSECTION")


def test_remote_converter_http_error_becomes_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "sample.dwg"
    source_path.write_bytes(b"AC1032" + b"\0" * 64)
    parser = DrawingParser(workspace_root=tmp_path)

    monkeypatch.setenv("JIANMO_DWG_CONVERTER_URL", "http://windows-host:3010/convert/dwg-to-dxf")

    class _FakeHttpError(drawing_parser.urllib_error.HTTPError):
        def __init__(self) -> None:
            super().__init__(
                url="http://windows-host:3010/convert/dwg-to-dxf",
                code=502,
                msg="Bad Gateway",
                hdrs=None,
                fp=None,
            )

        def read(self) -> bytes:
            return b"converter failed"

    monkeypatch.setattr(
        drawing_parser.urllib_request,
        "urlopen",
        lambda request, timeout: (_ for _ in ()).throw(_FakeHttpError()),
    )

    with pytest.raises(RuntimeError, match="HTTP 502: converter failed"):
        parser._convert_dwg_to_dxf(source_path, "AC1032 (R2018)")
