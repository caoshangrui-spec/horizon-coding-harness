from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from uuid import uuid4

from horizon.domain.errors import IntegrityError, PolicyDenied

_MAX_VERIFIED_ARTIFACTS = 16_384
_ArtifactSignature = tuple[int, int, int, int, int]


def reject_link(path: Path) -> None:
    if path.is_symlink() or path.is_junction():
        raise PolicyDenied(f"Links and junctions are not accepted: {path.name}")


class ArtifactStore:
    """Immutable content-addressed bytes, published before SQLite references them."""

    def __init__(self, root: Path):
        for part in (root.absolute(), *root.absolute().parents):
            reject_link(part)
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # A process may snapshot the same large workspace many times. Remember only artifacts
        # this instance has written or fully verified; changed filesystem metadata invalidates
        # the fast path and forces a fresh digest check.
        self._verified: dict[str, _ArtifactSignature] = {}

    @staticmethod
    def _signature(path: Path) -> _ArtifactSignature:
        details = path.stat()
        return (
            details.st_dev,
            details.st_ino,
            details.st_size,
            details.st_mtime_ns,
            details.st_ctime_ns,
        )

    def _remember_verified(self, sha256: str, signature: _ArtifactSignature) -> None:
        if sha256 not in self._verified and len(self._verified) >= _MAX_VERIFIED_ARTIFACTS:
            self._verified.pop(next(iter(self._verified)))
        self._verified[sha256] = signature

    def _path(self, sha256: str) -> Path:
        if re.fullmatch(r"[a-f0-9]{64}", sha256) is None:
            raise IntegrityError("Invalid artifact digest")
        path = self.root / sha256[:2] / sha256
        reject_link(path.parent)
        reject_link(path)
        return path

    def put(self, content: bytes) -> str:
        sha256 = hashlib.sha256(content).hexdigest()
        path = self._path(sha256)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            signature = self._signature(path)
            if self._verified.get(sha256) != signature:
                self.read(sha256)
            return sha256
        temporary = path.parent / f".{uuid4().hex}.tmp"
        published = False
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Link publication is atomic and cannot overwrite another writer's artifact.
                os.link(temporary, path)
                published = True
            except FileExistsError:
                self.read(sha256)
            if os.name != "nt":
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
        if published:
            # Removing the temporary hard link changes the inode ctime on POSIX. Cache the
            # stable post-unlink signature so an unchanged artifact keeps the verified fast path.
            self._remember_verified(sha256, self._signature(path))
        return sha256

    def read(self, sha256: str, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        path = self._path(sha256)
        try:
            before = self._signature(path)
            with path.open("rb") as stream:
                content = stream.read(max_bytes + 1)
            after = self._signature(path)
        except FileNotFoundError as exc:
            raise IntegrityError(f"Missing artifact: {sha256}") from exc
        if (
            before != after
            or len(content) > max_bytes
            or hashlib.sha256(content).hexdigest() != sha256
        ):
            raise IntegrityError(f"Artifact size or digest mismatch: {sha256}")
        self._remember_verified(sha256, after)
        return content
