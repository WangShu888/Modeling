from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zdong.app.assets import LocalAssetStorage
from zdong.app.models import ProjectCreateRequest
from zdong.app.store import InMemoryStore, SQLiteStore


def _prepare_asset(path: Path, content: bytes) -> tuple[int, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    size = path.stat().st_size
    digest = hashlib.sha256(content).hexdigest()
    return size, digest


def test_local_asset_storage_logs_ingest(tmp_path: Path) -> None:
    storage = LocalAssetStorage(tmp_path)
    content = b"intake-sample"
    stored = storage.save(
        "proj_0001",
        filename="plan.pdf",
        media_type="application/pdf",
        content=content,
    )

    assert (tmp_path / "proj_0001").is_dir()
    entries = storage.read_ingest_log()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["project_id"] == "proj_0001"
    assert entry["filename"] == "plan.pdf"
    assert entry["size_bytes"] == len(content)
    assert entry["content_hash"] == hashlib.sha256(content).hexdigest()
    assert entry["path"] == stored.path


def test_inmemory_store_validates_asset_integrity(tmp_path: Path) -> None:
    store = InMemoryStore()
    project = store.create_project(ProjectCreateRequest(name="in-memory"))
    asset_path = tmp_path / "sources" / "plan.pdf"
    size, digest = _prepare_asset(asset_path, b"plans")

    asset = store.create_asset(
        project.project_id,
        filename="plan.pdf",
        media_type="application/pdf",
        description="floor plan",
        path=str(asset_path),
        extension=".pdf",
        size_bytes=size,
        content_hash=digest,
    )
    assert asset.content_hash == digest

    with pytest.raises(ValueError, match="hash mismatch"):
        store.create_asset(
            project.project_id,
            filename="plan.pdf",
            media_type="application/pdf",
            description="floor plan",
            path=str(asset_path),
            extension=".pdf",
            size_bytes=size,
            content_hash="deadbeef",
        )

    with pytest.raises(ValueError, match="not found"):
        store.create_asset(
            project.project_id,
            filename="missing.pdf",
            media_type="application/pdf",
            description="missing",
            path=str(tmp_path / "missing.pdf"),
            extension=".pdf",
            size_bytes=1,
            content_hash="0" * 64,
        )


def test_sqlite_store_validates_asset_integrity(tmp_path: Path) -> None:
    db_path = tmp_path / "store.sqlite3"
    store = SQLiteStore(db_path)
    project = store.create_project(ProjectCreateRequest(name="sqlite"))
    asset_path = tmp_path / "sources" / "plan.pdf"
    size, digest = _prepare_asset(asset_path, b"sql plans")

    asset = store.create_asset(
        project.project_id,
        filename="plan.pdf",
        media_type="application/pdf",
        description="floor plan",
        path=str(asset_path),
        extension=".pdf",
        size_bytes=size,
        content_hash=digest,
    )
    assert asset.content_hash == digest

    with pytest.raises(ValueError, match="size mismatch"):
        store.create_asset(
            project.project_id,
            filename="plan.pdf",
            media_type="application/pdf",
            description="floor plan",
            path=str(asset_path),
            extension=".pdf",
            size_bytes=size + 1,
            content_hash=digest,
        )
