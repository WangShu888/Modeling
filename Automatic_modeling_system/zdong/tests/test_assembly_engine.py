from __future__ import annotations

from zdong.app.models import DrawingEntityRecord, Point2D
from zdong.app.parser.assembly_engine import AssemblyEngine


def _entity(
    category: str,
    storey: str | None = None,
    anchor: Point2D | None = None,
) -> DrawingEntityRecord:
    entity = DrawingEntityRecord(asset_name="project1", category=category)
    if storey:
        entity.metadata["storey_key"] = storey
    if anchor:
        entity.points.append(anchor)
    return entity


def _point(x: float, y: float) -> Point2D:
    return Point2D(x=x, y=y)


def test_assembly_merges_storeys_and_reports_duplicates() -> None:
    entities = [
        _entity("wall_line", "1F", _point(0.0, 0.0)),
        _entity("wall_line", "1F", _point(0.0, 0.0)),
        _entity("wall_line", "2F", _point(0.0, 0.0)),
    ]
    engine = AssemblyEngine()
    result = engine.assemble(entities)
    assert len(result.storey_manifests) == 2
    assert any(manifest.duplicates for manifest in result.storey_manifests)
    assert result.cross_layer_chains


def test_cross_layer_chains_require_multiple_storeys() -> None:
    entities = [
        _entity("wall_line", "1F", _point(0.0, 0.0)),
        _entity("wall_line", "2F", _point(0.0, 0.0)),
        _entity("wall_line", "3F", _point(0.0, 0.0)),
    ]
    engine = AssemblyEngine()
    result = engine.assemble(entities)
    assert len(result.cross_layer_chains) == 1
    assert result.cross_layer_chains[0].storeys == ["1F", "2F", "3F"]


def test_storey_manifest_records_grid_signature() -> None:
    engine = AssemblyEngine()
    result = engine.assemble([_entity("wall_line", "1F", _point(5.0, 5.0))])
    assert result.storey_manifests
    assert result.storey_manifests[0].grid_signature == "none"
