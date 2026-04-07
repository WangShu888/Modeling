from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Protocol

from .models import (
    AssetRecord,
    ModelingRequestCreate,
    ModelingRequestRecord,
    ProjectCreateRequest,
    ProjectSummary,
    VersionSnapshot,
)


class Store(Protocol):
    def next_id(self, prefix: str) -> str: ...

    def create_project(self, payload: ProjectCreateRequest) -> ProjectSummary: ...

    def list_projects(self) -> list[ProjectSummary]: ...

    def get_project(self, project_id: str) -> ProjectSummary | None: ...

    def create_asset(
        self,
        project_id: str,
        *,
        filename: str,
        media_type: str,
        description: str | None,
        path: str,
        extension: str,
        size_bytes: int,
        content_hash: str,
    ) -> AssetRecord: ...

    def list_assets(self, project_id: str) -> list[AssetRecord]: ...

    def get_asset(self, project_id: str, asset_id: str) -> AssetRecord | None: ...

    def create_request(
        self,
        project_id: str,
        payload: ModelingRequestCreate,
    ) -> ModelingRequestRecord: ...

    def list_requests(self, project_id: str) -> list[ModelingRequestRecord]: ...

    def get_request(self, project_id: str, request_id: str) -> ModelingRequestRecord | None: ...

    def save_version(self, snapshot: VersionSnapshot) -> VersionSnapshot: ...

    def list_versions(self, project_id: str) -> list[VersionSnapshot]: ...

    def get_version(self, project_id: str, version_id: str) -> VersionSnapshot | None: ...


def _ensure_asset_integrity(path: str, size_bytes: int, content_hash: str) -> None:
    asset_path = Path(path)
    if not asset_path.is_file():
        raise ValueError(f"Asset file not found at {asset_path}")
    actual_size = asset_path.stat().st_size
    if actual_size != size_bytes:
        raise ValueError(
            f"Asset size mismatch for {asset_path}: expected {size_bytes}, actual {actual_size}"
        )
    actual_hash = hashlib.sha256(asset_path.read_bytes()).hexdigest()
    if actual_hash != content_hash:
        raise ValueError(
            f"Asset hash mismatch for {asset_path}: expected {content_hash}, actual {actual_hash}"
        )


class InMemoryStore:
    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)
        self._projects: dict[str, ProjectSummary] = {}
        self._assets: dict[str, dict[str, AssetRecord]] = defaultdict(dict)
        self._requests: dict[str, dict[str, ModelingRequestRecord]] = defaultdict(dict)
        self._versions: dict[str, dict[str, VersionSnapshot]] = defaultdict(dict)

    def next_id(self, prefix: str) -> str:
        self._counters[prefix] += 1
        return f"{prefix}_{self._counters[prefix]:04d}"

    def create_project(self, payload: ProjectCreateRequest) -> ProjectSummary:
        project = ProjectSummary(
            project_id=self.next_id("proj"),
            name=payload.name,
            building_type=payload.building_type,
            region=payload.region,
        )
        self._projects[project.project_id] = project
        return project

    def list_projects(self) -> list[ProjectSummary]:
        return list(self._projects.values())

    def get_project(self, project_id: str) -> ProjectSummary | None:
        return self._projects.get(project_id)

    def create_asset(
        self,
        project_id: str,
        *,
        filename: str,
        media_type: str,
        description: str | None,
        path: str,
        extension: str,
        size_bytes: int,
        content_hash: str,
    ) -> AssetRecord:
        if self.get_project(project_id) is None:
            raise KeyError(project_id)

        _ensure_asset_integrity(path, size_bytes, content_hash)

        asset = AssetRecord(
            asset_id=self.next_id("asset"),
            project_id=project_id,
            filename=filename,
            media_type=media_type,
            description=description,
            path=path,
            extension=extension,
            size_bytes=size_bytes,
            content_hash=content_hash,
        )
        self._assets[project_id][asset.asset_id] = asset
        return asset

    def list_assets(self, project_id: str) -> list[AssetRecord]:
        return list(self._assets.get(project_id, {}).values())

    def get_asset(self, project_id: str, asset_id: str) -> AssetRecord | None:
        return self._assets.get(project_id, {}).get(asset_id)

    def create_request(
        self,
        project_id: str,
        payload: ModelingRequestCreate,
    ) -> ModelingRequestRecord:
        if self.get_project(project_id) is None:
            raise KeyError(project_id)
        for asset_id in payload.asset_ids:
            if self.get_asset(project_id, asset_id) is None:
                raise KeyError(asset_id)

        request = ModelingRequestRecord(
            project_id=project_id,
            request_id=self.next_id("req"),
            **payload.model_dump(mode="json"),
        )
        self._requests[project_id][request.request_id] = request
        return request

    def list_requests(self, project_id: str) -> list[ModelingRequestRecord]:
        return list(self._requests.get(project_id, {}).values())

    def get_request(self, project_id: str, request_id: str) -> ModelingRequestRecord | None:
        return self._requests.get(project_id, {}).get(request_id)

    def save_version(self, snapshot: VersionSnapshot) -> VersionSnapshot:
        project_id = snapshot.project.project_id
        request_id = snapshot.source_bundle.request_id
        version_id = snapshot.source_bundle.version_id
        self._versions[project_id][version_id] = snapshot

        project = snapshot.project.model_copy(deep=True)
        project.latest_version_id = version_id
        self._projects[project_id] = project
        snapshot.project.latest_version_id = version_id

        request = self.get_request(project_id, request_id)
        if request is not None:
            request.latest_version_id = version_id

        return snapshot

    def list_versions(self, project_id: str) -> list[VersionSnapshot]:
        return list(self._versions.get(project_id, {}).values())

    def get_version(self, project_id: str, version_id: str) -> VersionSnapshot | None:
        return self._versions.get(project_id, {}).get(version_id)


class SQLiteStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS counters (
                    prefix TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    building_type TEXT,
                    region TEXT,
                    created_at TEXT NOT NULL,
                    latest_version_id TEXT
                );

                CREATE TABLE IF NOT EXISTS assets (
                    asset_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    description TEXT,
                    path TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );

                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    latest_version_id TEXT,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );

                CREATE TABLE IF NOT EXISTS versions (
                    version_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id),
                    FOREIGN KEY(request_id) REFERENCES requests(request_id)
                );
                """
            )

    def next_id(self, prefix: str) -> str:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM counters WHERE prefix = ?",
                (prefix,),
            ).fetchone()
            next_value = (int(row["value"]) if row is not None else 0) + 1
            if row is None:
                connection.execute(
                    "INSERT INTO counters(prefix, value) VALUES (?, ?)",
                    (prefix, next_value),
                )
            else:
                connection.execute(
                    "UPDATE counters SET value = ? WHERE prefix = ?",
                    (next_value, prefix),
                )
            connection.commit()
        return f"{prefix}_{next_value:04d}"

    def create_project(self, payload: ProjectCreateRequest) -> ProjectSummary:
        project = ProjectSummary(
            project_id=self.next_id("proj"),
            name=payload.name,
            building_type=payload.building_type,
            region=payload.region,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO projects(project_id, name, building_type, region, created_at, latest_version_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    project.project_id,
                    project.name,
                    project.building_type,
                    project.region,
                    project.created_at,
                    project.latest_version_id,
                ),
            )
            connection.commit()
        return project

    def list_projects(self) -> list[ProjectSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT project_id, name, building_type, region, created_at, latest_version_id
                FROM projects
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [self._project_from_row(row) for row in rows]

    def get_project(self, project_id: str) -> ProjectSummary | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT project_id, name, building_type, region, created_at, latest_version_id
                FROM projects
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
        return self._project_from_row(row) if row is not None else None

    def create_asset(
        self,
        project_id: str,
        *,
        filename: str,
        media_type: str,
        description: str | None,
        path: str,
        extension: str,
        size_bytes: int,
        content_hash: str,
    ) -> AssetRecord:
        if self.get_project(project_id) is None:
            raise KeyError(project_id)

        _ensure_asset_integrity(path, size_bytes, content_hash)

        asset = AssetRecord(
            asset_id=self.next_id("asset"),
            project_id=project_id,
            filename=filename,
            media_type=media_type,
            description=description,
            path=path,
            extension=extension,
            size_bytes=size_bytes,
            content_hash=content_hash,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO assets(
                    asset_id, project_id, filename, media_type, description, path,
                    extension, size_bytes, content_hash, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset.asset_id,
                    asset.project_id,
                    asset.filename,
                    asset.media_type,
                    asset.description,
                    asset.path,
                    asset.extension,
                    asset.size_bytes,
                    asset.content_hash,
                    asset.created_at,
                ),
            )
            connection.commit()
        return asset

    def list_assets(self, project_id: str) -> list[AssetRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT asset_id, project_id, filename, media_type, description, path,
                       extension, size_bytes, content_hash, created_at
                FROM assets
                WHERE project_id = ?
                ORDER BY created_at DESC
                """,
                (project_id,),
            ).fetchall()
        return [self._asset_from_row(row) for row in rows]

    def get_asset(self, project_id: str, asset_id: str) -> AssetRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT asset_id, project_id, filename, media_type, description, path,
                       extension, size_bytes, content_hash, created_at
                FROM assets
                WHERE project_id = ? AND asset_id = ?
                """,
                (project_id, asset_id),
            ).fetchone()
        return self._asset_from_row(row) if row is not None else None

    def create_request(
        self,
        project_id: str,
        payload: ModelingRequestCreate,
    ) -> ModelingRequestRecord:
        if self.get_project(project_id) is None:
            raise KeyError(project_id)
        for asset_id in payload.asset_ids:
            if self.get_asset(project_id, asset_id) is None:
                raise KeyError(asset_id)

        request = ModelingRequestRecord(
            project_id=project_id,
            request_id=self.next_id("req"),
            **payload.model_dump(mode="json"),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO requests(request_id, project_id, payload_json, created_at, latest_version_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.project_id,
                    json.dumps(payload.model_dump(mode="json"), ensure_ascii=False),
                    request.created_at,
                    request.latest_version_id,
                ),
            )
            connection.commit()
        return request

    def list_requests(self, project_id: str) -> list[ModelingRequestRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT request_id, project_id, payload_json, created_at, latest_version_id
                FROM requests
                WHERE project_id = ?
                ORDER BY created_at DESC
                """,
                (project_id,),
            ).fetchall()
        return [self._request_from_row(row) for row in rows]

    def get_request(self, project_id: str, request_id: str) -> ModelingRequestRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT request_id, project_id, payload_json, created_at, latest_version_id
                FROM requests
                WHERE project_id = ? AND request_id = ?
                """,
                (project_id, request_id),
            ).fetchone()
        return self._request_from_row(row) if row is not None else None

    def save_version(self, snapshot: VersionSnapshot) -> VersionSnapshot:
        project_id = snapshot.project.project_id
        request_id = snapshot.source_bundle.request_id
        version_id = snapshot.source_bundle.version_id
        created_at = snapshot.created_at

        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO versions(version_id, project_id, request_id, created_at, snapshot_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    project_id,
                    request_id,
                    created_at,
                    snapshot.model_dump_json(),
                ),
            )
            connection.execute(
                "UPDATE projects SET latest_version_id = ? WHERE project_id = ?",
                (version_id, project_id),
            )
            connection.execute(
                "UPDATE requests SET latest_version_id = ? WHERE project_id = ? AND request_id = ?",
                (version_id, project_id, request_id),
            )
            connection.commit()

        snapshot.project.latest_version_id = version_id
        return snapshot

    def list_versions(self, project_id: str) -> list[VersionSnapshot]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT snapshot_json
                FROM versions
                WHERE project_id = ?
                ORDER BY created_at DESC
                """,
                (project_id,),
            ).fetchall()
        return [VersionSnapshot.model_validate_json(row["snapshot_json"]) for row in rows]

    def get_version(self, project_id: str, version_id: str) -> VersionSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT snapshot_json
                FROM versions
                WHERE project_id = ? AND version_id = ?
                """,
                (project_id, version_id),
            ).fetchone()
        return VersionSnapshot.model_validate_json(row["snapshot_json"]) if row is not None else None

    def _project_from_row(self, row: sqlite3.Row) -> ProjectSummary:
        return ProjectSummary(
            project_id=row["project_id"],
            name=row["name"],
            building_type=row["building_type"],
            region=row["region"],
            created_at=row["created_at"],
            latest_version_id=row["latest_version_id"],
        )

    def _asset_from_row(self, row: sqlite3.Row) -> AssetRecord:
        return AssetRecord(
            asset_id=row["asset_id"],
            project_id=row["project_id"],
            filename=row["filename"],
            media_type=row["media_type"],
            description=row["description"],
            path=row["path"],
            extension=row["extension"],
            size_bytes=int(row["size_bytes"]),
            content_hash=row["content_hash"],
            created_at=row["created_at"],
        )

    def _request_from_row(self, row: sqlite3.Row) -> ModelingRequestRecord:
        payload = json.loads(row["payload_json"])
        return ModelingRequestRecord(
            project_id=row["project_id"],
            request_id=row["request_id"],
            created_at=row["created_at"],
            latest_version_id=row["latest_version_id"],
            **payload,
        )
