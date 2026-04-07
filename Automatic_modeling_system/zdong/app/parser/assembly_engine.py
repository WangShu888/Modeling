from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..models import (
    BoundingBox2D,
    DrawingEntityRecord,
    DrawingFragmentRecord,
    GridAxis,
    Point2D,
)
from ..storey_inference import infer_storey_key, storey_sort_key


@dataclass
class StoreyManifest:
    storey_key: str
    component_count: int
    duplicates: int
    bounding_box: BoundingBox2D | None
    grid_signature: str


@dataclass
class CrossLayerChain:
    component_type: str
    storeys: list[str]
    anchor: Point2D | None
    signature: str


@dataclass
class AssemblyResult:
    storey_manifests: list[StoreyManifest] = field(default_factory=list)
    duplicate_signatures: list[str] = field(default_factory=list)
    cross_layer_chains: list[CrossLayerChain] = field(default_factory=list)


def _entity_anchor(entity: DrawingEntityRecord) -> Point2D | None:
    if entity.points:
        x = sum(point.x for point in entity.points) / len(entity.points)
        y = sum(point.y for point in entity.points) / len(entity.points)
        return Point2D(x=x, y=y)
    if entity.bbox is not None:
        return Point2D(
            x=(entity.bbox.min_x + entity.bbox.max_x) / 2.0,
            y=(entity.bbox.min_y + entity.bbox.max_y) / 2.0,
        )
    return None


def _entity_bbox(entity: DrawingEntityRecord) -> BoundingBox2D | None:
    if entity.bbox is not None:
        return entity.bbox
    if entity.points:
        min_x = min(point.x for point in entity.points)
        min_y = min(point.y for point in entity.points)
        max_x = max(point.x for point in entity.points)
        max_y = max(point.y for point in entity.points)
        return BoundingBox2D(min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y)
    return None


def _merge_bboxes(first: BoundingBox2D | None, second: BoundingBox2D | None) -> BoundingBox2D | None:
    if first is None:
        return second
    if second is None:
        return first
    return BoundingBox2D(
        min_x=min(first.min_x, second.min_x),
        min_y=min(first.min_y, second.min_y),
        max_x=max(first.max_x, second.max_x),
        max_y=max(first.max_y, second.max_y),
    )


def _grid_signature(axes: Sequence[GridAxis]) -> str:
    if not axes:
        return "none"
    labels: set[str] = set()
    for axis in axes:
        parts = [axis.label or axis.layer or axis.orientation]
        coordinate = axis.coordinate
        if coordinate is not None:
            parts.append(f"{coordinate:.3f}")
        labels.add("@".join(filter(None, parts)))
    return "|".join(sorted(labels))


def _storey_key_from_entity(entity: DrawingEntityRecord) -> str:
    for key in ("fragment_storey_key", "storey_key"):
        value = entity.metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return "1F"


def _fragment_storey_key(fragment: DrawingFragmentRecord) -> str:
    if fragment.storey_key:
        return fragment.storey_key
    if fragment.fragment_title:
        inferred = infer_storey_key(fragment.fragment_title)
        if inferred:
            return inferred
    if fragment.asset_name:
        inferred = infer_storey_key(fragment.asset_name)
        if inferred:
            return inferred
    return "1F"


def _entity_signature(entity: DrawingEntityRecord) -> tuple[str | None, str, float | None, float | None]:
    anchor = _entity_anchor(entity)
    component_type = entity.metadata.get("component_type") or entity.category
    x = round(anchor.x, 3) if anchor is not None else None
    y = round(anchor.y, 3) if anchor is not None else None
    return (entity.asset_name, component_type, x, y)


def _format_duplicate_signature(signature: tuple[str | None, str, float | None, float | None], storey_key: str) -> str:
    asset, comp_type, x, y = signature
    coords = f"{x},{y}" if x is not None and y is not None else "unknown"
    return f"{storey_key}:{asset or 'anon'}:{comp_type}:{coords}"


class AssemblyEngine:
    def __init__(self, continuity_tolerance: float = 0.05):
        assert continuity_tolerance > 0
        self.continuity_tolerance = continuity_tolerance

    def assemble(
        self,
        components: Iterable[DrawingEntityRecord],
        fragments: Sequence[DrawingFragmentRecord] | None = None,
        grid_axes: Sequence[GridAxis] | None = None,
    ) -> AssemblyResult:
        fragments = fragments or []
        grid_axes = grid_axes or []
        storey_map: dict[str, list[DrawingEntityRecord]] = defaultdict(list)
        for component in components:
            key = _storey_key_from_entity(component)
            storey_map[key].append(component)
        self._ensure_storeys_from_fragments(storey_map, fragments)
        grid_sig = _grid_signature(grid_axes)
        manifests: list[StoreyManifest] = []
        duplicate_signatures: list[str] = []
        for storey_key, entries in storey_map.items():
            manifest, duplicates = self._summarize_storey(storey_key, entries, grid_sig)
            manifests.append(manifest)
            duplicate_signatures.extend(duplicates)
        manifests.sort(key=lambda manifest: storey_sort_key(manifest.storey_key))
        cross_layer_chains = self._build_cross_layer_chains(storey_map)
        return AssemblyResult(
            storey_manifests=manifests,
            duplicate_signatures=duplicate_signatures,
            cross_layer_chains=cross_layer_chains,
        )

    def _ensure_storeys_from_fragments(
        self,
        storey_map: dict[str, list[DrawingEntityRecord]],
        fragments: Sequence[DrawingFragmentRecord],
    ) -> None:
        for fragment in fragments:
            role = (fragment.fragment_role or "").lower()
            if role in {"section", "facade"}:
                continue
            key = _fragment_storey_key(fragment)
            storey_map.setdefault(key, [])

    def _summarize_storey(
        self,
        storey_key: str,
        components: list[DrawingEntityRecord],
        grid_signature: str,
    ) -> tuple[StoreyManifest, list[str]]:
        duplicates: list[str] = []
        signature_counts: dict[tuple[str | None, str, float | None, float | None], int] = defaultdict(int)
        bounding_box: BoundingBox2D | None = None
        for entity in components:
            signature = _entity_signature(entity)
            signature_counts[signature] += 1
            if signature_counts[signature] == 2:
                duplicates.append(_format_duplicate_signature(signature, storey_key))
            entity_bbox = _entity_bbox(entity)
            bounding_box = _merge_bboxes(bounding_box, entity_bbox)
        manifest = StoreyManifest(
            storey_key=storey_key,
            component_count=len(components),
            duplicates=len(duplicates),
            bounding_box=bounding_box,
            grid_signature=grid_signature,
        )
        return manifest, duplicates

    def _build_cross_layer_chains(self, storey_map: dict[str, list[DrawingEntityRecord]]) -> list[CrossLayerChain]:
        buckets: dict[tuple[str, int, int], dict[str, object]] = {}
        for storey_key, components in storey_map.items():
            for entity in components:
                anchor = _entity_anchor(entity)
                if anchor is None:
                    continue
                component_type = entity.metadata.get("component_type") or entity.category or "unknown"
                x_bucket = round(anchor.x / self.continuity_tolerance)
                y_bucket = round(anchor.y / self.continuity_tolerance)
                bucket_key = (component_type, x_bucket, y_bucket)
                bucket = buckets.setdefault(bucket_key, {"storeys": set(), "anchors": []})
                bucket["storeys"].add(storey_key)
                bucket["anchors"].append(anchor)
        chains: list[CrossLayerChain] = []
        for bucket_key, bucket in buckets.items():
            storeys = sorted(bucket["storeys"], key=storey_sort_key)
            if len(storeys) < 2:
                continue
            anchor = self._average_point(bucket["anchors"])
            chains.append(
                CrossLayerChain(
                    component_type=bucket_key[0],
                    storeys=storeys,
                    anchor=anchor,
                    signature=f"{bucket_key[0]}:{';'.join(storeys)}",
                )
            )
        return chains

    def _average_point(self, points: Iterable[Point2D]) -> Point2D | None:
        points_list = list(points)
        if not points_list:
            return None
        x = sum(point.x for point in points_list) / len(points_list)
        y = sum(point.y for point in points_list) / len(points_list)
        return Point2D(x=x, y=y)
