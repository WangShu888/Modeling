from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, Response


def _find_odafc_executable() -> Path:
    env_candidates = [
        os.getenv("JIANMO_ODAFC_PATH"),
        os.getenv("ODA_FILE_CONVERTER_PATH"),
    ]
    for candidate in env_candidates:
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file():
                return path

    roots = [
        Path("C:/Program Files/ODA"),
        Path("C:/Program Files (x86)/ODA"),
    ]
    for root in roots:
        if not root.exists():
            continue
        matches = sorted(root.rglob("ODAFileConverter.exe"), reverse=True)
        if matches:
            return matches[0]

    raise FileNotFoundError(
        "ODA File Converter is not installed on this Windows host. "
        "Set JIANMO_ODAFC_PATH or install ODA File Converter locally."
    )


def convert_dwg_bytes_to_dxf_bytes(
    source_bytes: bytes,
    filename: str,
    output_version: str = "ACAD2018",
) -> tuple[bytes, str]:
    if not source_bytes:
        raise RuntimeError("Uploaded DWG payload is empty.")

    converter_path = _find_odafc_executable()
    safe_name = Path(filename or "drawing.dwg").name
    if Path(safe_name).suffix.lower() != ".dwg":
        safe_name = f"{Path(safe_name).stem}.dwg"

    with tempfile.TemporaryDirectory(prefix="jianmo_dwg_in_") as input_dir, tempfile.TemporaryDirectory(
        prefix="jianmo_dwg_out_"
    ) as output_dir:
        input_path = Path(input_dir) / safe_name
        input_path.write_bytes(source_bytes)

        command = [
            str(converter_path),
            input_dir,
            output_dir,
            output_version,
            "DXF",
            "0",
            "1",
            input_path.name,
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise RuntimeError(f"Unable to launch ODA File Converter: {exc}") from exc

        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
            raise RuntimeError(f"ODA File Converter failed: {message}")

        output_name = input_path.with_suffix(".dxf").name
        output_path = Path(output_dir) / output_name
        if not output_path.is_file():
            raise RuntimeError("ODA File Converter completed without producing a DXF file.")
        return output_path.read_bytes(), output_name


def create_app() -> FastAPI:
    app = FastAPI(
        title="Jianmo Windows DWG Converter",
        version="0.1.0",
        description="A minimal Windows-side DWG to DXF conversion service backed by ODA File Converter.",
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/convert/dwg-to-dxf")
    async def convert_dwg_to_dxf(
        request: Request,
        x_source_filename: str | None = Header(default=None),
        x_output_version: str = Header(default="ACAD2018"),
        authorization: str | None = Header(default=None),
    ) -> Response:
        expected_token = os.getenv("JIANMO_DWG_CONVERTER_TOKEN") or os.getenv("JIANMO_WINDOWS_CONVERTER_TOKEN")
        if expected_token and authorization != f"Bearer {expected_token}":
            raise HTTPException(status_code=401, detail="Unauthorized converter request.")

        body = await request.body()
        filename = x_source_filename or "drawing.dwg"

        try:
            dxf_bytes, output_name = convert_dwg_bytes_to_dxf_bytes(
                source_bytes=body,
                filename=filename,
                output_version=x_output_version,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return Response(
            content=dxf_bytes,
            media_type="application/octet-stream",
            headers={"X-Output-Filename": output_name},
        )

    return app


app = create_app()


def run_dev_server() -> None:
    host = os.getenv("JIANMO_WINDOWS_CONVERTER_HOST", "0.0.0.0")
    port = int(os.getenv("JIANMO_WINDOWS_CONVERTER_PORT", "3010"))
    uvicorn.run(
        "zdong.app.windows_dwg_converter_service:app",
        host=host,
        port=port,
    )


if __name__ == "__main__":
    run_dev_server()
