from __future__ import annotations

from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import ezdxf

from zdong.app.parser.dxf_reader import DxfDocumentReader, DxfDocumentResult


class DummyLayer:
    def __init__(self, name: str) -> None:
        self.dxf = SimpleNamespace(name=name)


class DummyEntity:
    def __init__(self, dxftype: str, **dxf_attrs: object) -> None:
        self._type = dxftype
        self.dxf = SimpleNamespace(**dxf_attrs)

    def dxftype(self) -> str:
        return self._type


class DummyLWPolylineEntity(DummyEntity):
    def __init__(
        self,
        *,
        layer: str,
        points: list[tuple[float, float]],
        closed: bool = False,
        handle: str = "",
    ) -> None:
        super().__init__("LWPOLYLINE", layer=layer, handle=handle)
        self._points = points
        self.closed = closed

    def get_points(self, mode: str = "xy") -> list[tuple[float, float]]:
        assert mode == "xy"
        return list(self._points)


class DummyProxyEntity(DummyEntity):
    def __init__(
        self,
        *,
        layer: str,
        handle: str,
        virtual_entities: list[DummyEntity] | None = None,
        proxy_graphic: bytes = b"\x08\x00\x00\x00\x00\x00\x00\x00",
    ) -> None:
        super().__init__("ACAD_PROXY_ENTITY", layer=layer, handle=handle)
        self._virtual_entities = virtual_entities or []
        self.proxy_graphic = proxy_graphic
        self.acdb_proxy_entity = []

    def virtual_entities(self) -> list[DummyEntity]:
        return list(self._virtual_entities)


class DummyDoc:
    def __init__(self, layers: list[DummyLayer], entities: list[DummyEntity], header: dict[str, object]) -> None:
        self.layers = layers
        self._entities = entities
        self.header = header

    def modelspace(self) -> list[DummyEntity]:
        return list(self._entities)


REAL_TCH_DXF_PATHS = [
    Path(__file__).resolve().parents[2] / "图纸文件" / "一层平面图.dxf",
    Path(__file__).resolve().parents[2] / "图纸文件" / "二层平面图.dxf",
]


def test_parse_document_detects_wall_line() -> None:
    reader = DxfDocumentReader()
    layer = DummyLayer("A-WALL")
    entity = DummyEntity(
        "LINE",
        layer="A-WALL",
        handle="H1",
        start=(0.0, 0.0),
        end=(0.0, 5.0),
    )
    doc = DummyDoc(
        layers=[layer],
        entities=[entity],
        header={"$INSBASE": (0.0, 0.0, 0.0), "$INSUNITS": "0"},
    )

    result = reader.parse_document(doc, asset_name="floor.dxf", descriptor="Floor plan")
    assert isinstance(result, DxfDocumentResult)
    assert any(entity.category == "wall_line" for entity in result.detected_entities)
    assert "A-WALL" in result.recognized_layers


def test_parse_document_keeps_full_detected_entities_and_decodes_units() -> None:
    reader = DxfDocumentReader()
    layer = DummyLayer("A-WALL")
    entities = [
        DummyEntity(
            "LINE",
            layer="A-WALL",
            handle=f"H{index}",
            start=(0.0, float(index)),
            end=(5.0, float(index)),
        )
        for index in range(220)
    ]
    doc = DummyDoc(
        layers=[layer],
        entities=entities,
        header={"$INSBASE": (0.0, 0.0, 0.0), "$INSUNITS": 4},
    )

    result = reader.parse_document(doc, asset_name="dense-floor.dxf", descriptor="Dense floor plan")

    assert len(result.detected_entities) == 220
    assert result.units == "mm"


def test_parse_document_uses_proxy_virtual_entities_when_available() -> None:
    reader = DxfDocumentReader()
    proxy_virtual = DummyEntity(
        "LINE",
        layer="WALL",
        handle="V1",
        start=(0.0, 0.0),
        end=(8.0, 0.0),
    )
    proxy = DummyProxyEntity(layer="WALL", handle="P1", virtual_entities=[proxy_virtual])
    doc = DummyDoc(
        layers=[DummyLayer("WALL")],
        entities=[proxy],
        header={"$INSBASE": (0.0, 0.0, 0.0), "$INSUNITS": 4},
    )

    result = reader.parse_document(doc, asset_name="proxy-virtual.dxf", descriptor="Proxy virtual")

    assert any(entity.category == "wall_line" for entity in result.detected_entities)
    assert not any(item.category == "proxy_virtual_entities_empty" for item in result.pending_review)


def test_parse_document_promotes_proxy_entities_from_available_line_segments() -> None:
    reader = DxfDocumentReader()
    donor_line = DummyEntity(
        "LINE",
        layer="A-Strs",
        handle="L1",
        start=(0.0, 0.0),
        end=(12.0, 0.0),
    )
    proxy_wall_1 = DummyProxyEntity(layer="WALL", handle="P1")
    proxy_wall_2 = DummyProxyEntity(layer="WALL", handle="P2")
    proxy_window = DummyProxyEntity(layer="WINDOW", handle="P3")
    doc = DummyDoc(
        layers=[DummyLayer("A-Strs"), DummyLayer("WALL"), DummyLayer("WINDOW")],
        entities=[donor_line, proxy_wall_1, proxy_wall_2, proxy_window],
        header={"$INSBASE": (0.0, 0.0, 0.0), "$INSUNITS": 4},
    )

    result = reader.parse_document(doc, asset_name="proxy-fallback-lines.dxf", descriptor="Proxy fallback lines")

    wall_entities = [entity for entity in result.detected_entities if entity.category == "wall_line"]
    window_entities = [entity for entity in result.detected_entities if entity.category == "window_block"]
    assert len(wall_entities) >= 2
    assert len(window_entities) >= 1
    assert any(entity.metadata.get("proxy_fallback") for entity in wall_entities)
    assert any(item.category == "proxy_geometry_approximated" for item in result.pending_review)


def test_parse_document_reports_unresolved_proxy_geometry_without_donors() -> None:
    reader = DxfDocumentReader()
    proxy_wall = DummyProxyEntity(layer="WALL", handle="P1")
    proxy_window = DummyProxyEntity(layer="WINDOW", handle="P2")
    doc = DummyDoc(
        layers=[DummyLayer("WALL"), DummyLayer("WINDOW")],
        entities=[proxy_wall, proxy_window],
        header={"$INSBASE": (0.0, 0.0, 0.0), "$INSUNITS": 4},
    )

    result = reader.parse_document(doc, asset_name="proxy-unresolved.dxf", descriptor="Proxy unresolved")

    assert not any(entity.category in {"wall_line", "wall_path"} for entity in result.detected_entities)
    assert any(item.category == "proxy_geometry_unresolved" for item in result.pending_review)
    assert any(item.category == "proxy_virtual_entities_empty" for item in result.pending_review)


def test_parse_document_extracts_tch_entities_from_real_floor_plan_samples() -> None:
    reader = DxfDocumentReader()

    for path in REAL_TCH_DXF_PATHS:
        assert path.is_file(), f"missing regression sample: {path}"

        doc = ezdxf.readfile(path)
        result = reader.parse_document(doc, asset_name=path.name, descriptor=path.stem)
        categories = Counter(entity.category for entity in result.detected_entities)

        assert categories["wall_line"] == 15
        assert categories["window_block"] >= 3
        assert categories["door_block"] >= 3
        assert categories["column_path"] == 9
        assert categories["room_label"] == 4
        assert "WALL" in result.recognized_layers
        assert "COLUMN" in result.recognized_layers
        assert "A-Wind" in result.recognized_layers
        assert "WINDOW" in result.recognized_layers
