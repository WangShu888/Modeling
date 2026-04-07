from __future__ import annotations

import sys
from pathlib import Path

import ezdxf

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zdong.app.drawing_parser import DrawingParser
from zdong.app.models import AssetRecord, SourceBundle


def _bundle(path: Path) -> SourceBundle:
    return SourceBundle(
        project_id="proj_0001",
        request_id="req_0001",
        version_id="ver_0001",
        prompt="parse drawing",
        source_mode_hint="cad_to_bim",
        assets=[
            AssetRecord(
                asset_id="asset_0001",
                filename=path.name,
                media_type="image/vnd.dxf",
                path=str(path),
                extension=path.suffix.lower(),
            )
        ],
        form_fields={},
    )


def test_parser_emits_chinese_view_marker_candidates_from_dxf_text(tmp_path: Path) -> None:
    path = tmp_path / "综合图.dxf"
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_text("二层平面图", dxfattribs={"layer": "TEXT"}).set_placement((0, 0))
    msp.add_text("1-1剖面图", dxfattribs={"layer": "TEXT"}).set_placement((1000, 0))
    msp.add_text("南立面图", dxfattribs={"layer": "TEXT"}).set_placement((2000, 0))
    doc.saveas(path)

    parsed = DrawingParser(workspace_root=tmp_path).parse(_bundle(path))

    candidate_names = [item.name for item in parsed.storey_candidate_details]
    assert "standard_floor" in candidate_names
    assert "section_reference" in candidate_names
    assert "facade_reference" in candidate_names
    assert "二层平面图" in candidate_names


def test_parser_flags_multi_storey_asset_collapse_risk(tmp_path: Path) -> None:
    path = tmp_path / "综合平面图.dxf"
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_line((0, 0), (4000, 0), dxfattribs={"layer": "WALL"})
    msp.add_text("一层平面图", dxfattribs={"layer": "TEXT"}).set_placement((0, 0))
    msp.add_text("二层平面图", dxfattribs={"layer": "TEXT"}).set_placement((1000, 0))
    doc.saveas(path)

    parsed = DrawingParser(workspace_root=tmp_path).parse(_bundle(path))

    categories = [item.category for item in parsed.pending_review]
    assert "multi_storey_asset_collapsed" in categories
    plan_fragments = [fragment for fragment in parsed.fragments if fragment.fragment_role == "plan"]
    assert len(plan_fragments) >= 2
    assert {fragment.storey_key for fragment in plan_fragments} >= {"1F", "2F"}
    assert all(fragment.fragment_id for fragment in plan_fragments)
    assert all(fragment.fragment_title for fragment in plan_fragments)
    assert all(fragment.bbox is not None for fragment in plan_fragments)
    wall_entities = [entity for entity in parsed.detected_entities if entity.category in {"wall_line", "wall_path"}]
    assert wall_entities
    assert "fragment_id" in wall_entities[0].metadata
    assert "fragment_storey_key" in wall_entities[0].metadata


def test_parser_assigns_entities_to_nearest_floor_fragment(tmp_path: Path) -> None:
    path = tmp_path / "综合分层图.dxf"
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_line((-1000, 0), (1000, 0), dxfattribs={"layer": "WALL"})
    msp.add_line((1000, 0), (3000, 0), dxfattribs={"layer": "WALL"})
    msp.add_text("一层平面图", dxfattribs={"layer": "TEXT"}).set_placement((0, 0))
    msp.add_text("二层平面图", dxfattribs={"layer": "TEXT"}).set_placement((2000, 0))
    doc.saveas(path)

    parsed = DrawingParser(workspace_root=tmp_path).parse(_bundle(path))

    categories = [item.category for item in parsed.pending_review]
    assert "multi_storey_asset_collapsed" not in categories
    wall_entities = [entity for entity in parsed.detected_entities if entity.category in {"wall_line", "wall_path"}]
    assert {entity.metadata["fragment_storey_key"] for entity in wall_entities} == {"1F", "2F"}


def test_parser_keeps_full_entity_output_and_emits_entity_stats(tmp_path: Path) -> None:
    path = tmp_path / "构件密集图.dxf"
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    for index in range(220):
        y = index * 100
        msp.add_line((0, y), (4000, y), dxfattribs={"layer": "WALL"})
    doc.saveas(path)

    parsed = DrawingParser(workspace_root=tmp_path).parse(_bundle(path))

    categories = [item.category for item in parsed.pending_review]
    assert "entity_detection_truncated" not in categories
    assert len(parsed.detected_entities) == 220
    assert parsed.detected_entities_total == 220
    assert parsed.detected_entities_emitted == 220
    assert parsed.detected_entities_dropped == 0
    assert parsed.detected_entities_source_summary[path.name] == 220


def test_parser_uses_geometry_clusters_when_titles_are_far_from_view_geometry(tmp_path: Path) -> None:
    path = tmp_path / "楼梯分层详图.dxf"
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()

    for y in range(0, 5000, 1000):
        msp.add_line((0, y), (1200, y), dxfattribs={"layer": "WALL"})
    for y in range(0, 5000, 1000):
        msp.add_line((6000, y), (7200, y), dxfattribs={"layer": "WALL"})

    msp.add_text("楼梯首层平面详图", dxfattribs={"layer": "TEXT"}).set_placement((10000, 0))
    msp.add_text("楼梯二层平面详图", dxfattribs={"layer": "TEXT"}).set_placement((12000, 0))
    doc.saveas(path)

    parsed = DrawingParser(workspace_root=tmp_path).parse(_bundle(path))

    wall_entities = [entity for entity in parsed.detected_entities if entity.category in {"wall_line", "wall_path"}]
    assert wall_entities
    assert {entity.metadata["fragment_storey_key"] for entity in wall_entities} == {"1F", "2F"}
    assert "multi_storey_asset_collapsed" not in [item.category for item in parsed.pending_review]
