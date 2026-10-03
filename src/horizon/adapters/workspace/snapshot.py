from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from horizon.adapters.persistence.artifacts import ArtifactStore, reject_link
from horizon.domain.common import Contract, canonical_json, digest
from horizon.domain.errors import IntegrityError, PolicyDenied
from horizon.domain.task import relative_pattern


class FileEntry(Contract):
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    executable: bool = False

    @model_validator(mode="after")
    def safe_path(self):
        relative_pattern(self.path)
        if any(part in {".git", ".horizon"} for part in self.path.split("/")):
            raise ValueError("Control paths cannot be restored from workspace artifacts")
        if any(char in self.path for char in "*?[]"):
            raise ValueError("Snapshot entries are literal paths, not globs")
        # Prevent NTFS ADS, reserved devices and case/trailing-dot aliasing on Windows.
        devices = {"CON", "PRN", "AUX", "NUL"}
        devices.update(f"{prefix}{index}" for prefix in ("COM", "LPT") for index in range(1, 10))
        for part in self.path.split("/"):
            if part.endswith((".", " ")) or part.split(".")[0].upper() in devices:
                raise ValueError("Path is not portable to Windows")
        return self


class FileSnapshot(Contract):
    schema_version: Literal[1] = 1
    files: tuple[FileEntry, ...]
    excluded_paths: tuple[str, ...]
    workspace_revision: str

    @model_validator(mode="after")
    def validate_manifest(self):
        names = [entry.path.casefold() for entry in self.files]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate or case-colliding snapshot paths")
        if self.workspace_revision != digest(
            [entry.model_dump(mode="json") for entry in self.files]
        ):
            raise ValueError("Snapshot tree hash mismatch")
        return self


class SnapshotManager:
    """Captures files only. Git objects, running processes and approvals are not snapshots."""

    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    def capture(
        self,
        root: Path,
        *,
        allowed_paths: tuple[str, ...] | None = None,
        denied_paths: tuple[str, ...] = (".env", ".env.*", "secrets/**"),
        max_file_bytes: int = 8 * 1024 * 1024,
        max_total_bytes: int = 64 * 1024 * 1024,
        max_files: int = 10_000,
    ) -> tuple[FileSnapshot, str]:
        for part in (root.absolute(), *root.absolute().parents):
            reject_link(part)
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise PolicyDenied("Workspace must be a directory")
        if root == self.artifacts.root or root.is_relative_to(self.artifacts.root):
            raise PolicyDenied("Artifact storage is not a workspace")
        if allowed_paths is not None:
            for pattern in allowed_paths:
                relative_pattern(pattern)
            if not allowed_paths:
                raise PolicyDenied("A scoped snapshot requires at least one allowed path")
        for pattern in denied_paths:
            relative_pattern(pattern)
        excluded: list[str] = []
        entries: list[FileEntry] = []
        total = 0
        for current, dirs, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in sorted(dirs[:]):
                path = current_path / name
                rel = path.relative_to(root).as_posix()
                reject_link(path)
                if path.resolve() == self.artifacts.root:
                    dirs.remove(name)
                    excluded.append(rel + "/")
                    continue
                if name in {".git", ".horizon", ".venv", "__pycache__", ".pytest_cache"} or any(
                    fnmatch.fnmatchcase(rel + "/", pattern) for pattern in denied_paths
                ):
                    dirs.remove(name)
                    excluded.append(rel + "/")
            dirs.sort()
            for name in sorted(files):
                path = current_path / name
                rel = path.relative_to(root).as_posix()
                reject_link(path)
                if (
                    allowed_paths is not None
                    and not any(fnmatch.fnmatchcase(rel, pattern) for pattern in allowed_paths)
                ) or any(fnmatch.fnmatchcase(rel, pattern) for pattern in denied_paths):
                    excluded.append(rel)
                    continue
                if not stat.S_ISREG(path.stat().st_mode):
                    raise PolicyDenied("Snapshot only supports regular files")
                with path.open("rb") as stream:
                    content = stream.read(max_file_bytes + 1)
                total += len(content)
                if (
                    len(content) > max_file_bytes
                    or total > max_total_bytes
                    or len(entries) >= max_files
                ):
                    raise PolicyDenied("Snapshot size limit exceeded; nothing has been committed")
                sha256 = self.artifacts.put(content)
                entries.append(
                    FileEntry(
                        path=rel,
                        sha256=sha256,
                        size_bytes=len(content),
                        executable=bool(path.stat().st_mode & stat.S_IXUSR),
                    )
                )
        entries.sort(key=lambda entry: entry.path)
        revision = digest([entry.model_dump(mode="json") for entry in entries])
        snapshot = FileSnapshot(
            files=entries, excluded_paths=sorted(excluded), workspace_revision=revision
        )
        manifest_hash = self.artifacts.put(canonical_json(snapshot).encode("utf-8"))
        return snapshot, manifest_hash

    def load(self, manifest_hash: str) -> FileSnapshot:
        try:
            return FileSnapshot.model_validate_json(self.artifacts.read(manifest_hash))
        except ValueError as exc:
            raise IntegrityError("Invalid workspace manifest") from exc

    def verify(self, manifest_hash: str) -> FileSnapshot:
        snapshot = self.load(manifest_hash)
        for entry in snapshot.files:
            content = self.artifacts.read(entry.sha256)
            if len(content) != entry.size_bytes:
                raise IntegrityError("Snapshot file size mismatch")
        return snapshot

    def restore(self, manifest_hash: str, destination: Path) -> FileSnapshot:
        snapshot = self.verify(manifest_hash)
        destination = destination.absolute()
        for path in (destination, *destination.parents):
            reject_link(path)
        if destination.exists():
            raise PolicyDenied("Restore requires a NEW directory; existing files are preserved")
        if destination == self.artifacts.root or destination.is_relative_to(self.artifacts.root):
            raise PolicyDenied("Cannot restore over the control artifact store")
        destination.mkdir(parents=True, exist_ok=False)
        for entry in snapshot.files:
            target = destination.joinpath(*entry.path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            content = self.artifacts.read(entry.sha256)
            with target.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                target.chmod(0o755 if entry.executable else 0o644)
            if hashlib.sha256(target.read_bytes()).hexdigest() != entry.sha256:
                raise IntegrityError("Restored file failed hash verification")
        return snapshot
