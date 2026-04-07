from pathlib import Path

import ezdxf
import fitz

from jianmo.app.drawing_parser import DrawingParser
from jianmo.app.models import AssetRecord, SourceBundle


def make_runtime_dir(name: str) -> Path:
    root = Path.cwd() / "test_runtime" / name
    root.mkdir(parents=True, exist_ok=True)
    return root


def make_bundle(path: Path, description: str) -> SourceBundle:
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
                description=description,
                path=str(path),
                extension=path.suffix.lower(),
            )
        ],
        form_fields={},
    )


def test_parser_reads_real_dxf_geometry() -> None:
    runtime_dir = make_runtime_dir("drawing_parser_dxf")
    path = runtime_dir / "standard-floor.dxf"
    if path.exists():
        path.unlink()
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    for layer_name in ("A-AXIS", "A-WALL", "A-WIND", "A-DIMS", "A-NOTE"):
        doc.layers.add(layer_name)

    msp = doc.modelspace()
    msp.add_line((0, 0), (0, 6000), dxfattribs={"layer": "A-AXIS"})
    msp.add_lwpolyline(
        [(0, 0), (6000, 0), (6000, 4000), (0, 4000), (0, 0)],
        dxfattribs={"layer": "A-WALL"},
    )
    block = doc.blocks.new(name="WINDOW_800x1200")
    block.add_line((0, 0), (800, 0))
    msp.add_blockref("WINDOW_800x1200", (1200, 800), dxfattribs={"layer": "A-WIND"})
    msp.add_text("STANDARD FLOOR PLAN", dxfattribs={"layer": "A-NOTE", "insert": (300, 300)})
    dim = msp.add_linear_dim(
        base=(0, -500),
        p1=(0, 0),
        p2=(6000, 0),
        angle=0,
        dxfattribs={"layer": "A-DIMS"},
    )
    dim.render()
    doc.saveas(path)

    parsed = DrawingParser(workspace_root=runtime_dir).parse(make_bundle(path, "standard floor plan"))

    assert parsed.asset_kinds == ["cad"]
    assert parsed.units == "mm"
    assert {"A-AXIS", "A-WALL", "A-WIND", "A-DIMS", "A-NOTE"}.issubset(parsed.recognized_layers)
    assert parsed.entity_summary["lines"] >= 1
    assert parsed.entity_summary["polylines"] >= 1
    assert parsed.entity_summary["blocks"] >= 1
    assert parsed.dimension_entities >= 1
    assert parsed.grid_lines_detected >= 1
    assert any("STANDARD FLOOR PLAN" in text for text in parsed.text_annotations)
    assert "standard_floor" in parsed.storey_candidates
    assert parsed.origin.source == "dxf_insbase"
    assert any(layer.semantic_role == "grid" for layer in parsed.layer_map)
    assert any(axis.layer == "A-AXIS" for axis in parsed.grid_map)
    assert any(detail.kind in {"cad_dimension", "text_dimension"} for detail in parsed.dimension_details)
    assert any(entity.category in {"wall_path", "window_block"} for entity in parsed.detected_entities)
    assert parsed.pending_review == []
    assert parsed.unresolved_entities == []


def test_parser_reads_real_pdf_geometry_and_text() -> None:
    runtime_dir = make_runtime_dir("drawing_parser_pdf")
    path = runtime_dir / "facade.pdf"
    if path.exists():
        path.unlink()
    doc = fitz.open()
    page = doc.new_page()
    shape = page.new_shape()
    shape.draw_line((20, 20), (200, 20))
    shape.draw_rect(fitz.Rect(40, 40, 180, 120))
    shape.finish(color=(0, 0, 0))
    shape.commit()
    page.insert_text((30, 170), "Axis A-1")
    page.insert_text((30, 200), "3600 mm")
    page.insert_text((30, 230), "Section")
    doc.save(path)
    doc.close()

    parsed = DrawingParser(workspace_root=runtime_dir).parse(make_bundle(path, "standard floor PDF"))

    assert parsed.asset_kinds == ["pdf"]
    assert parsed.units == "mm"
    assert parsed.entity_summary["lines"] >= 1
    assert parsed.entity_summary["polylines"] >= 1
    assert parsed.entity_summary["texts"] >= 3
    assert parsed.grid_lines_detected >= 1
    assert parsed.dimension_entities >= 1
    assert any("Axis A-1" in text for text in parsed.text_annotations)
    assert any("3600 mm" in text for text in parsed.text_annotations)
    assert "standard_floor" in parsed.storey_candidates
    assert "section_reference" in parsed.storey_candidates
    assert parsed.pdf_assets[0].pdf_type == "vector"
    assert "vector" in parsed.pdf_modes_detected
    assert any(item.semantic_tag == "dimension" for item in parsed.text_annotation_items)
    assert parsed.unresolved_entities == []


def test_parser_reports_clear_dwg_blocker_when_converter_is_missing(
    monkeypatch
) -> None:
    runtime_dir = make_runtime_dir("drawing_parser_dwg")
    path = runtime_dir / "sample.dwg"
    path.write_bytes(b"AC1032" + b"\0" * 64)

    parser = DrawingParser(workspace_root=runtime_dir)

    def fake_convert(*_args, **_kwargs) -> Path:
        raise FileNotFoundError("ODA File Converter is not installed.")

    monkeypatch.setattr(parser, "_convert_dwg_to_dxf", fake_convert)

    parsed = parser.parse(make_bundle(path, "residential standard floor"))

    assert parsed.asset_kinds == ["cad"]
    assert parsed.entity_summary == {
        "lines": 0,
        "polylines": 0,
        "blocks": 0,
        "texts": 0,
        "dimensions": 0,
    }
    assert any("DWG version AC1032 (R2018)" in text for text in parsed.text_annotations)
    assert any(item.category == "dwg_converter_missing" for item in parsed.pending_review)
    assert any("ODA File Converter is not installed" in issue for issue in parsed.unresolved_entities)


def test_parser_distinguishes_scanned_pdf_for_review() -> None:
    runtime_dir = make_runtime_dir("drawing_parser_scanned_pdf")
    path = runtime_dir / "scan.pdf"
    if path.exists():
        path.unlink()

    doc = fitz.open()
    page = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 80), False)
    pix.clear_with(255)
    page.insert_image(fitz.Rect(0, 0, 120, 80), pixmap=pix)
    doc.save(path)
    doc.close()

    parsed = DrawingParser(workspace_root=runtime_dir).parse(make_bundle(path, "scan reference"))

    assert parsed.asset_kinds == ["pdf"]
    assert parsed.pdf_assets[0].pdf_type == "scanned"
    assert "scanned" in parsed.pdf_modes_detected
    assert any(item.category == "scanned_pdf_requires_ocr" for item in parsed.pending_review)
