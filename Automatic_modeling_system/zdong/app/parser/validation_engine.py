from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..models import DrawingFragmentRecord, GridAxis, IssueSeverity
from ..storey_inference import infer_storey_key, storey_sort_key
from .assembly_engine import AssemblyResult


@dataclass
class ModelReadySet:
    storey_keys: list[str]
    component_count: int
    grid_signature: str
    cross_layer_ready: bool
    ready: bool


@dataclass
class ValidationIssue:
    category: str
    reason: str
    severity: IssueSeverity
    target: str | None = None
    source_ref: str | None = None


@dataclass
class NeedReviewList:
    issues: list[ValidationIssue] = field(default_factory=list)


@dataclass
class BlockedIssueList:
    issues: list[ValidationIssue] = field(default_factory=list)


@dataclass
class ValidationOutcome:
    model_ready_set: ModelReadySet
    need_review_list: NeedReviewList
    blocked_issue_list: BlockedIssueList


def _grid_signature(axes: Sequence[GridAxis]) -> str:
    if not axes:
        return "none"
    signatures: set[str] = set()
    for axis in axes:
        label = axis.label or axis.layer or axis.orientation or "axis"
        coord = axis.coordinate
        coord_text = f"{coord:.3f}" if coord is not None else "unknown"
        signatures.add(f"{label}@{coord_text}")
    return "|".join(sorted(signatures))


def _infer_expected_storeys(fragments: Iterable[DrawingFragmentRecord] | None) -> set[str]:
    if not fragments:
        return set()
    keys: set[str] = set()
    for fragment in fragments:
        role = (fragment.fragment_role or "").lower()
        if role in {"section", "facade"}:
            continue
        candidate = fragment.storey_key
        if not candidate and fragment.fragment_title:
            candidate = infer_storey_key(fragment.fragment_title)
        if not candidate and fragment.asset_name:
            candidate = infer_storey_key(fragment.asset_name)
        if not candidate:
            candidate = "1F"
        keys.add(candidate)
    return keys


class ValidationEngine:
    def __init__(self, cross_layer_threshold: int = 2):
        self.cross_layer_threshold = max(2, cross_layer_threshold)

    def validate(
        self,
        assembly_result: AssemblyResult,
        fragments: Iterable[DrawingFragmentRecord] | None = None,
        grid_axes: Sequence[GridAxis] | None = None,
    ) -> ValidationOutcome:
        fragments = fragments or []
        grid_axes = grid_axes or []
        manifest_keys = sorted(
            {manifest.storey_key for manifest in assembly_result.storey_manifests},
            key=storey_sort_key,
        )
        component_count = sum(manifest.component_count for manifest in assembly_result.storey_manifests)
        blocked = BlockedIssueList()
        need_review = NeedReviewList()
        expected_storeys = _infer_expected_storeys(fragments)
        manifest_key_set = set(manifest_keys)
        missing_storeys = expected_storeys - manifest_key_set
        if missing_storeys:
            blocked.issues.append(
                ValidationIssue(
                    category="storey_missing",
                    reason=f"Missing expected storeys: {', '.join(sorted(missing_storeys, key=storey_sort_key))}",
                    severity="error",
                )
            )
        duplicates = assembly_result.duplicate_signatures
        if duplicates:
            need_review.issues.append(
                ValidationIssue(
                    category="duplicate_component",
                    reason=f"Detected {len(duplicates)} duplicate components: {', '.join(duplicates)}",
                    severity="warning",
                )
            )
        if len(manifest_keys) >= self.cross_layer_threshold and not assembly_result.cross_layer_chains:
            need_review.issues.append(
                ValidationIssue(
                    category="cross_layer_alignment",
                    reason="Assembly produced multiple storeys but no continuous cross-layer chains.",
                    severity="warning",
                )
            )
        if not grid_axes:
            need_review.issues.append(
                ValidationIssue(
                    category="grid_missing",
                    reason="No grid axes were available to align the storeys.",
                    severity="warning",
                )
            )
        grid_sig = _grid_signature(grid_axes)
        ready = not blocked.issues
        ready_set = ModelReadySet(
            storey_keys=manifest_keys,
            component_count=component_count,
            grid_signature=grid_sig,
            cross_layer_ready=bool(assembly_result.cross_layer_chains),
            ready=ready,
        )
        return ValidationOutcome(
            model_ready_set=ready_set,
            need_review_list=need_review,
            blocked_issue_list=blocked,
        )
