from __future__ import annotations

import difflib
import fnmatch
import hashlib
import os
from pathlib import Path
from uuid import uuid4

from horizon.adapters.persistence.artifacts import reject_link
from horizon.adapters.vcs.git import read_git_head
from horizon.adapters.workspace.snapshot import FileSnapshot, SnapshotManager
from horizon.domain.common import digest
from horizon.domain.errors import Conflict, PolicyDenied
from horizon.domain.promotion import PromotionPlan, WorkspaceChange, WorkspaceOrigin
from horizon.domain.task import Constraints

MAX_PROMOTION_CREATED_FILE_BYTES = 64 * 1024


def workspace_path_hash(path: Path) -> str:
    resolved = path.resolve(strict=True)
    return digest({"resolved_path": os.path.normcase(str(resolved))})


class WorkspacePromoter:
    """Promote up to eight verified changes, including at most one created file."""

    def __init__(self, snapshots: SnapshotManager):
        self.snapshots = snapshots

    @staticmethod
    def _permitted(path: str, constraints: Constraints) -> bool:
        return any(
            fnmatch.fnmatchcase(path, pattern) for pattern in constraints.allowed_paths
        ) and not any(fnmatch.fnmatchcase(path, pattern) for pattern in constraints.denied_paths)

    @staticmethod
    def _maps(snapshot: FileSnapshot):
        return {entry.path: entry for entry in snapshot.files}

    def bind_origin(self, source: Path, constraints: Constraints) -> WorkspaceOrigin:
        snapshot, manifest = self.snapshots.capture(
            source,
            denied_paths=constraints.denied_paths,
        )
        return WorkspaceOrigin(
            source_path_hash=workspace_path_hash(source),
            source_revision=snapshot.workspace_revision,
            source_manifest_ref=manifest,
            git_head=read_git_head(source),
        )

    def build_plan(
        self,
        source: Path,
        candidate: Path,
        origin: WorkspaceOrigin,
        constraints: Constraints,
    ) -> PromotionPlan:
        if workspace_path_hash(source) != origin.source_path_hash:
            raise Conflict("Promotion source path does not match the bound workspace origin")
        source_snapshot, source_manifest = self.snapshots.capture(
            source,
            denied_paths=constraints.denied_paths,
        )
        if (
            source_snapshot.workspace_revision != origin.source_revision
            or source_manifest != origin.source_manifest_ref
        ):
            raise Conflict("Source workspace changed after the Agent staging snapshot")
        if read_git_head(source) != origin.git_head:
            raise Conflict("Git HEAD changed after the Agent staging snapshot")
        candidate_snapshot, candidate_manifest = self.snapshots.capture(
            candidate,
            denied_paths=constraints.denied_paths,
        )
        before = self._maps(source_snapshot)
        after = self._maps(candidate_snapshot)
        deleted_paths = set(before) - set(after)
        if deleted_paths:
            raise Conflict("Promotion does not support deleted files")
        created_paths = set(after) - set(before)
        if len(created_paths) > 1:
            raise Conflict("Promotion supports at most one created file")
        changes = tuple(
            WorkspaceChange(
                path=path,
                kind="modified" if path in before else "created",
                before_sha256=before[path].sha256 if path in before else None,
                after_sha256=after[path].sha256,
            )
            for path in sorted(after)
            if path not in before or before[path].sha256 != after[path].sha256
        )
        if not 1 <= len(changes) <= 8:
            raise Conflict("Promotion requires between one and eight changed files")
        patches: list[str] = []
        for change in changes:
            if not self._permitted(change.path, constraints):
                raise PolicyDenied("Candidate diff is outside the TaskSpec path authority")
            after_bytes = self.snapshots.artifacts.read(change.after_sha256)
            if change.kind == "created":
                if len(after_bytes) > MAX_PROMOTION_CREATED_FILE_BYTES:
                    raise PolicyDenied("Created promotion file exceeds the 64 KiB byte limit")
                before_bytes = b""
            else:
                assert change.before_sha256 is not None
                before_bytes = self.snapshots.artifacts.read(change.before_sha256)
            try:
                before_text = before_bytes.decode("utf-8")
                after_text = after_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PolicyDenied("Promotion supports UTF-8 text changes only") from exc
            file_patch = "".join(
                difflib.unified_diff(
                    before_text.splitlines(keepends=True),
                    after_text.splitlines(keepends=True),
                    fromfile=(f"a/{change.path}" if change.kind == "modified" else "/dev/null"),
                    tofile=f"b/{change.path}",
                )
            )
            if change.kind == "created" and not file_patch:
                file_patch = f"--- /dev/null\n+++ b/{change.path}\n"
            patches.append(file_patch)
        patch = "".join(patches)
        if not patch or len(patch.encode("utf-8")) > 512 * 1024:
            raise PolicyDenied("Promotion diff is empty or exceeds the audit artifact limit")
        diff_artifact_ref = self.snapshots.artifacts.put(patch.encode("utf-8"))
        return PromotionPlan(
            source_path_hash=origin.source_path_hash,
            source_revision_before=origin.source_revision,
            source_manifest_ref_before=origin.source_manifest_ref,
            candidate_revision=candidate_snapshot.workspace_revision,
            candidate_manifest_ref=candidate_manifest,
            diff_artifact_ref=diff_artifact_ref,
            git_head_before=origin.git_head,
            changes=changes,
        )

    @staticmethod
    def _target(root: Path, relative: str) -> Path:
        target = root.joinpath(*relative.split("/"))
        for part in (target.absolute(), *target.absolute().parents):
            reject_link(part)
            if part == root:
                break
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise PolicyDenied("Promotion target must be an existing regular workspace file")
        return resolved

    @staticmethod
    def _new_target(root: Path, relative: str) -> Path:
        target = root.joinpath(*relative.split("/"))
        reject_link(target)
        if target.exists():
            raise Conflict("Promotion create target appeared after source snapshot verification")
        for part in (target.parent.absolute(), *target.parent.absolute().parents):
            reject_link(part)
            if part == root:
                break
        try:
            parent = target.parent.resolve(strict=True)
        except OSError as exc:
            raise Conflict("Promotion create parent is no longer an existing directory") from exc
        if not parent.is_relative_to(root) or not parent.is_dir():
            raise PolicyDenied("Promotion create parent must remain inside the source workspace")
        return parent / target.name

    @staticmethod
    def _atomic_write(target: Path, content: bytes, suffix: str) -> None:
        temporary = target.parent / f".{target.name}.{uuid4().hex}.{suffix}"
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _exclusive_create(target: Path, content: bytes) -> tuple[int, int]:
        created_identity: tuple[int, int] | None = None
        try:
            try:
                with target.open("xb") as stream:
                    stat = os.fstat(stream.fileno())
                    created_identity = (stat.st_dev, stat.st_ino)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError as exc:
                raise Conflict("Promotion create target appeared before publication") from exc
        except BaseException:
            if created_identity is not None:
                try:
                    stat = target.stat(follow_symlinks=False)
                    if (stat.st_dev, stat.st_ino) == created_identity:
                        target.unlink()
                except FileNotFoundError:
                    pass
            raise
        assert created_identity is not None
        return created_identity

    @classmethod
    def _rollback_written(
        cls,
        written: list[str],
        targets: dict[str, Path],
        changes: dict[str, WorkspaceChange],
        originals: dict[str, bytes],
        created_identities: dict[str, tuple[int, int]],
    ) -> None:
        for path in reversed(written):
            target = targets[path]
            change = changes[path]
            if target.is_symlink() or target.is_junction() or not target.is_file():
                continue
            if hashlib.sha256(target.read_bytes()).hexdigest() != change.after_sha256:
                continue
            if change.kind == "created":
                identity = created_identities.get(path)
                if identity is None:
                    continue
                stat = target.stat(follow_symlinks=False)
                if (stat.st_dev, stat.st_ino) == identity:
                    target.unlink()
            else:
                cls._atomic_write(
                    target,
                    originals[path],
                    "horizon-promotion-rollback",
                )

    def apply_or_recover(
        self,
        plan: PromotionPlan,
        source: Path,
        candidate: Path,
        constraints: Constraints,
    ) -> tuple[str, str, bool]:
        if workspace_path_hash(source) != plan.source_path_hash:
            raise Conflict("Promotion source path does not match the reserved intent")
        if read_git_head(source) != plan.git_head_before:
            raise Conflict("Git HEAD changed after promotion was reserved")
        candidate_snapshot, candidate_manifest = self.snapshots.capture(
            candidate,
            denied_paths=constraints.denied_paths,
        )
        if (
            candidate_snapshot.workspace_revision != plan.candidate_revision
            or candidate_manifest != plan.candidate_manifest_ref
        ):
            raise Conflict("Staging candidate changed after promotion was reserved")
        base_snapshot = self.snapshots.verify(plan.source_manifest_ref_before)
        if base_snapshot.workspace_revision != plan.source_revision_before:
            raise Conflict("Promotion base manifest does not match the reserved revision")
        current, _ = self.snapshots.capture(
            source,
            denied_paths=constraints.denied_paths,
        )
        if current.excluded_paths != base_snapshot.excluded_paths:
            raise Conflict("Source excluded-path set changed after promotion was reserved")
        source_entries = self._maps(current)
        base_entries = self._maps(base_snapshot)
        candidate_entries = self._maps(candidate_snapshot)
        changes = {change.path: change for change in plan.changes}
        created_paths = {path for path, change in changes.items() if change.kind == "created"}
        if len(created_paths) > 1:
            raise Conflict("Promotion intent contains more than one created file")
        if set(candidate_entries) != set(base_entries) | created_paths:
            raise Conflict("Promotion candidate contains unsupported file-set changes")
        if not set(base_entries) <= set(source_entries) or not set(source_entries) <= set(
            candidate_entries
        ):
            raise Conflict("Source workspace file set changed after promotion was reserved")
        expected_changes = {
            path
            for path, entry in candidate_entries.items()
            if path not in base_entries or base_entries[path].sha256 != entry.sha256
        }
        if set(changes) != expected_changes:
            raise Conflict("Promotion plan changes do not match its manifests")

        already_applied = False
        pending_paths: list[str] = []
        targets: dict[str, Path] = {}
        originals: dict[str, bytes] = {}
        replacements: dict[str, bytes] = {}
        source_root = source.resolve(strict=True)
        for path, candidate_entry in candidate_entries.items():
            base_entry = base_entries.get(path)
            current_entry = source_entries.get(path)
            candidate_hash = candidate_entry.sha256
            change = changes.get(path)
            if change is None:
                if (
                    base_entry is None
                    or current_entry is None
                    or current_entry.sha256 != base_entry.sha256
                    or candidate_hash != base_entry.sha256
                ):
                    raise Conflict("Unrelated source file changed during promotion")
                continue
            if change.after_sha256 != candidate_hash:
                raise Conflict("Promotion plan file hashes no longer match its manifests")
            if not self._permitted(path, constraints):
                raise PolicyDenied("Reserved promotion path is outside TaskSpec authority")
            replacements[path] = self.snapshots.artifacts.read(change.after_sha256)
            if change.kind == "created":
                if base_entry is not None or change.before_sha256 is not None:
                    raise Conflict("Created promotion change does not match its base manifest")
                if len(replacements[path]) > MAX_PROMOTION_CREATED_FILE_BYTES:
                    raise PolicyDenied("Created promotion file exceeds the 64 KiB byte limit")
                if current_entry is None:
                    pending_paths.append(path)
                    targets[path] = self._new_target(source_root, path)
                elif current_entry.sha256 == change.after_sha256:
                    already_applied = True
                    target = self._target(source_root, path)
                    if hashlib.sha256(target.read_bytes()).hexdigest() != current_entry.sha256:
                        raise Conflict(
                            "Promotion target changed after source snapshot verification"
                        )
                    targets[path] = target
                else:
                    raise Conflict("Source contains a divergent created promotion effect")
            else:
                if (
                    base_entry is None
                    or current_entry is None
                    or change.before_sha256 != base_entry.sha256
                ):
                    raise Conflict("Modified promotion change does not match its base manifest")
                if current_entry.sha256 == change.after_sha256:
                    already_applied = True
                elif current_entry.sha256 == change.before_sha256:
                    pending_paths.append(path)
                else:
                    raise Conflict("Source contains a divergent partial promotion effect")
                target = self._target(source_root, path)
                originals[path] = self.snapshots.artifacts.read(change.before_sha256)
                if hashlib.sha256(target.read_bytes()).hexdigest() != current_entry.sha256:
                    raise Conflict("Promotion target changed after source snapshot verification")
                targets[path] = target

        written: list[str] = []
        created_identities: dict[str, tuple[int, int]] = {}
        try:
            for path in pending_paths:
                written.append(path)
                if changes[path].kind == "created":
                    created_identities[path] = self._exclusive_create(
                        targets[path], replacements[path]
                    )
                else:
                    self._atomic_write(targets[path], replacements[path], "horizon-promote")
            promoted, manifest = self.snapshots.capture(
                source,
                denied_paths=constraints.denied_paths,
            )
            if promoted.workspace_revision != plan.candidate_revision:
                raise Conflict("Promoted source does not match the validated candidate revision")
        except BaseException:
            self._rollback_written(
                written,
                targets,
                changes,
                originals,
                created_identities,
            )
            raise
        if read_git_head(source) != plan.git_head_before:
            self._rollback_written(
                written,
                targets,
                changes,
                originals,
                created_identities,
            )
            raise Conflict("Git HEAD changed during promotion")
        return promoted.workspace_revision, manifest, already_applied
