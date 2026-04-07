from __future__ import annotations

from zdong.app.models import CoordinateReference, DrawingEntityRecord
from zdong.app.parser.compatibility_adapter import (
    ParserAssetSnapshot,
    ParserCompatibilityAdapter,
    ParserCompatibilityContext,
)


def _asset_snapshot(*, units: str, asset_name: str = "sample.dxf") -> ParserAssetSnapshot:
    entity = DrawingEntityRecord(asset_name=asset_name, category="wall_line")
    return ParserAssetSnapshot(
        asset_name=asset_name,
        kind="cad",
        units=units,
        origin=CoordinateReference(asset_name=asset_name, source="detected"),
        detected_entities=[entity],
        entity_summary={"lines": 1, "polylines": 0, "blocks": 0, "texts": 0, "dimensions": 0},
    )


def test_adapter_uses_first_asset_units_when_bundle_units_not_locked() -> None:
    adapter = ParserCompatibilityAdapter()
    context = ParserCompatibilityContext(asset_results=[_asset_snapshot(units="m")])

    parsed = adapter.adapt(context)

    assert parsed.units == "m"
    assert parsed.detected_entities_total == 1
    assert parsed.detected_entities[0].metadata["source_fragment_id"]
    assert parsed.detected_entities[0].metadata["source_storey_key"] == "1F"


def test_adapter_keeps_locked_bundle_units_and_reports_conflict() -> None:
    adapter = ParserCompatibilityAdapter()
    context = ParserCompatibilityContext(
        asset_results=[_asset_snapshot(units="m")],
        bundle_units="mm",
        bundle_units_locked=True,
    )

    parsed = adapter.adapt(context)

    assert parsed.units == "mm"
    assert any(item.category == "unit_conflict" for item in parsed.pending_review)
