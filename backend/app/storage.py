from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredEvidence:
    path: str
    sha256: str
    size: int


class LocalEvidenceStorage:
    """Local implementation of the evidence storage boundary.

    The service only persists opaque generated names. A MinIO/S3 implementation
    can replace this class without changing inspection or API contracts.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes, filename: str) -> StoredEvidence:
        suffix = Path(filename).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            suffix = ".img"
        digest = hashlib.sha256(content).hexdigest()
        destination = self.root / f"{uuid.uuid4()}-{digest[:16]}{suffix}"
        destination.write_bytes(content)
        return StoredEvidence(path=str(destination), sha256=digest, size=len(content))

    def resolve(self, stored_path: str) -> Path:
        candidate = Path(stored_path).resolve(strict=True)
        if candidate.parent != self.root:
            raise ValueError("证据文件路径越界")
        return candidate

    def delete(self, stored_path: str) -> None:
        try:
            self.resolve(stored_path).unlink(missing_ok=True)
        except FileNotFoundError:
            return
