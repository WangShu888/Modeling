from __future__ import annotations

import json
from pathlib import Path


def test_site_boundary_warning_has_human_readable_chinese_copy() -> None:
    copy_path = Path("/workspace/自动建模系统/zdong/web/src/issueCopy.json")
    payload = json.loads(copy_path.read_text(encoding="utf-8"))

    warning = "A large closed polyline was inferred as a possible site boundary and should be confirmed."
    translated = payload["issue_messages"][warning]

    assert "场地边界" in translated
    assert "人工确认" in translated


def test_common_pipeline_statuses_have_chinese_labels() -> None:
    copy_path = Path("/workspace/自动建模系统/zdong/web/src/issueCopy.json")
    payload = json.loads(copy_path.read_text(encoding="utf-8"))

    assert payload["statuses"]["warning"] == "警告"
    assert payload["statuses"]["passed"] == "通过"
    assert payload["severities"]["error"] == "错误"
