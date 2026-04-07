from __future__ import annotations

from zdong.app.models import DrawingFragmentRecord, GridAxis, Point2D
from zdong.app.parser.assembly_engine import AssemblyResult, CrossLayerChain, StoreyManifest
from zdong.app.parser.validation_engine import ValidationEngine


def _manifest(storey_key: str) -> StoreyManifest:
    return StoreyManifest(
        storey_key=storey_key,
        component_count=1,
        duplicates=0,
        bounding_box=None,
        grid_signature="none",
    )


def _chain() -> CrossLayerChain:
    return CrossLayerChain(
        component_type="wall",
        storeys=["1F", "2F"],
        anchor=Point2D(x=0.0, y=0.0),
        signature="wall:1F;2F",
    )


def _fragment(fragment_id: str, storey_key: str) -> DrawingFragmentRecord:
    return DrawingFragmentRecord(
        fragment_id=fragment_id,
        asset_name="project1",
        storey_key=storey_key,
        fragment_role="plan",
    )


def test_validation_blocks_missing_storey() -> None:
    fragments = [_fragment("frag-1", "1F"), _fragment("frag-2", "2F")]
    assembly_result = AssemblyResult(storey_manifests=[_manifest("1F")])
    outcome = ValidationEngine().validate(assembly_result, fragments=fragments)
    assert outcome.blocked_issue_list.issues
    assert outcome.blocked_issue_list.issues[0].category == "storey_missing"
    assert not outcome.model_ready_set.ready


def test_validation_reports_duplicate_and_grid_issues() -> None:
    assembly_result = AssemblyResult(
        storey_manifests=[_manifest("1F"), _manifest("2F")],
        duplicate_signatures=["1F:project1:wall:0.0,0.0"],
    )
    outcome = ValidationEngine().validate(assembly_result)
    categories = {issue.category for issue in outcome.need_review_list.issues}
    assert "duplicate_component" in categories
    assert "grid_missing" in categories
    assert outcome.model_ready_set.ready


def test_validation_skips_cross_layer_warning_when_chains_exist() -> None:
    assembly_result = AssemblyResult(
        storey_manifests=[_manifest("1F"), _manifest("2F")],
        cross_layer_chains=[_chain()],
    )
    axis = GridAxis(asset_name="project1", layer="GRID_A", coordinate=0.0, orientation="vertical")
    outcome = ValidationEngine().validate(assembly_result, grid_axes=[axis])
    categories = {issue.category for issue in outcome.need_review_list.issues}
    assert "cross_layer_alignment" not in categories
    assert outcome.model_ready_set.cross_layer_ready
