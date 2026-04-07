from __future__ import annotations

import importlib.metadata as metadata
import importlib.util
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class ModuleRuntimeStatus:
    name: str
    available: bool
    version: str | None


@dataclass(frozen=True)
class IfcRuntimeInfo:
    exporter: str
    validator: str
    schema: str
    ifcopenshell_available: bool
    ifctester_available: bool
    ifcdiff_available: bool
    formal_backend_ready: bool
    module_statuses: tuple[ModuleRuntimeStatus, ...]


MODULES_TO_INSPECT = ("ifcopenshell", "ifctester", "ifcdiff")


def _module_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _module_status(name: str) -> ModuleRuntimeStatus:
    spec = importlib.util.find_spec(name)
    if not spec:
        return ModuleRuntimeStatus(name=name, available=False, version=None)
    return ModuleRuntimeStatus(name=name, available=True, version=_module_version(name))


@lru_cache(maxsize=1)
def detect_ifc_runtime() -> IfcRuntimeInfo:
    module_statuses = tuple(_module_status(name) for name in MODULES_TO_INSPECT)
    status_by_name = {status.name: status for status in module_statuses}
    ifcopenshell_status = status_by_name["ifcopenshell"]
    ifctester_status = status_by_name["ifctester"]
    ifcdiff_status = status_by_name["ifcdiff"]
    formal_backend_ready = ifcopenshell_status.available and ifctester_status.available
    exporter = (
        "ifcopenshell_exporter" if ifcopenshell_status.available else "text_ifc_fallback"
    )
    validator = (
        "ifctester" if ifctester_status.available else "semantic_pre_export_validator"
    )
    return IfcRuntimeInfo(
        exporter=exporter,
        validator=validator,
        schema="IFC4",
        ifcopenshell_available=ifcopenshell_status.available,
        ifctester_available=ifctester_status.available,
        ifcdiff_available=ifcdiff_status.available,
        formal_backend_ready=formal_backend_ready,
        module_statuses=module_statuses,
    )
