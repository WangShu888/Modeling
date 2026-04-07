from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_FILENAME_SANITIZER = re.compile(r"[^A-Za-z0-9._-]+")
_INGEST_LOG_FILENAME = "asset_ingest_log.jsonl"


def _safe_filename(filename: str) -> str:
    cleaned = _FILENAME_SANITIZER.sub("_", filename).strip("._")
    return cleaned or "asset.bin"


@dataclass
class StoredAssetFile:
    filename: str
    media_type: str
    path: str
    extension: str
    size_bytes: int
    content_hash: str


class LocalAssetStorage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.ingest_log_path = self.root / _INGEST_LOG_FILENAME

    def save(self, project_id: str, *, filename: str, media_type: str, content: bytes) -> StoredAssetFile:
        project_dir = self.root / project_id
        project_dir.mkdir(parents=True, exist_ok=True)

        safe_name = _safe_filename(filename)
        storage_name = f"{uuid.uuid4().hex}_{safe_name}"
        path = project_dir / storage_name
        path.write_bytes(content)

        size_bytes = len(content)
        content_hash = hashlib.sha256(content).hexdigest()
        self._append_ingest_log_entry(
            {
                "ingest_id": uuid.uuid4().hex,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "project_id": project_id,
                "filename": filename,
                "storage_name": storage_name,
                "media_type": media_type or "application/octet-stream",
                "path": str(path),
                "extension": (path.suffix.lower() or Path(filename).suffix.lower()),
                "size_bytes": size_bytes,
                "content_hash": content_hash,
            }
        )

        return StoredAssetFile(
            filename=filename,
            media_type=media_type or "application/octet-stream",
            path=str(path),
            extension=path.suffix.lower() or Path(filename).suffix.lower(),
            size_bytes=size_bytes,
            content_hash=content_hash,
        )

    def read_ingest_log(self) -> list[dict[str, object]]:
        if not self.ingest_log_path.exists():
            return []
        entries: list[dict[str, object]] = []
        with self.ingest_log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                entries.append(json.loads(line))
        return entries

    def _append_ingest_log_entry(self, entry: dict[str, object]) -> None:
        with self.ingest_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False))
            handle.write("\n")
