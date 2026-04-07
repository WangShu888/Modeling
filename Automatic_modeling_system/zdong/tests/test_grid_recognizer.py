from __future__ import annotations

import ezdxf

from zdong.app.parser.grid_recognizer import GridRecognizer


def _new_document() -> ezdxf.document.Drawing:
    return ezdxf.new("R2010")


def test_grid_recognizer_extracts_axis_from_lines() -> None:
    doc = _new_document()
    msp = doc.modelspace()
    msp.add_line(
        (0, 0),
        (0, 10),
        dxfattribs={"layer": "A-AXIS"},
    )
    msp.add_line(
        (2, 0),
        (2, 10),
        dxfattribs={"layer": "A-AXIS"},
    )

    recognizer = GridRecognizer(axis_limit=6)
    axes = recognizer.extract(doc, "grid-lines")

    assert len(axes) == 2
    assert all(axis.layer == "A-AXIS" for axis in axes)
    assert all(axis.orientation == "vertical" for axis in axes if axis.coordinate is not None)
    assert {axis.start.x for axis in axes} == {0.0, 2.0}


def test_grid_recognizer_collects_grid_labels_from_text() -> None:
    doc = _new_document()
    msp = doc.modelspace()
    text = msp.add_text(
        "Axis A1",
        dxfattribs={"layer": "A-AXIS"},
        height=1.5,
    )
    text.dxf.insert = (5, 5)

    recognizer = GridRecognizer(axis_limit=5)
    axes = recognizer.extract(doc, "grid-text")

    assert len(axes) == 1
    assert axes[0].label == "A1"
    assert axes[0].layer == "A-AXIS"
