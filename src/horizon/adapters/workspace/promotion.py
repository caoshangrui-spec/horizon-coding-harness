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


def workspace_path_hash(path: Path) -> str:
    resolved = path.resolve(strict=True)
    return digest({"resolved_path": os.path.normcase(str(resolved))})


class WorkspacePromoter:
    """Promote up to eight verified modified files with crash-recoverable intent."""

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
        if set(before) != set(after):
            raise Conflict("Initial promotion supports modified existing files only")
        changes = tuple(
            WorkspaceChange(
                path=path,
                before_sha256=before[path].sha256,
                after_sha256=after[path].sha256,
            )
            for path in sorted(before)
            if before[path].sha256 != after[path].sha256
        )
        if not 1 <= len(changes) <= 8:
            raise Conflict("Promotion requires between one and eight modified files")
        patches: list[str] = []
        for change in changes:
            if not self._permitted(change.path, constraints):
                raise PolicyDenied("Candidate diff is outside the TaskSpec path authority")
            before_bytes = self.snapshots.artifacts.read(change.before_sha256)
            after_bytes = self.snapshots.artifacts.read(change.after_sha256)
            try:
                before_text = before_bytes.decode("utf-8")
                after_text = after_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PolicyDenied("Promotion supports UTF-8 text changes only") from exc
            patches.append(
                "".join(
                    difflib.unified_diff(
                        before_text.splitlines(keepends=True),
                        after_text.splitlines(keepends=True),
                        fromfile=f"a/{change.path}",
                        tofile=f"b/{change.path}",
                    )
                )
            )
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
        if set(source_entries) != set(base_entries) or set(base_entries) != set(candidate_entries):
            raise Conflict("Source workspace file set changed after promotion was reserved")
        changes = {change.path: change for change in plan.changes}
        if set(changes) != {
            path
            for path in base_entries
            if base_entries[path].sha256 != candidate_entries[path].sha256
        }:
            raise Conflict("Promotion plan changes do not match its manifests")

        already_applied = False
        pending_paths: list[str] = []
        targets: dict[str, Path] = {}
        originals: dict[str, bytes] = {}
        replacements: dict[str, bytes] = {}
        for path, base_entry in base_entries.items():
            current_hash = source_entries[path].sha256
            candidate_hash = candidate_entries[path].sha256
            change = changes.get(path)
            if change is None:
                if current_hash != base_entry.sha256 or candidate_hash != base_entry.sha256:
                    raise Conflict("Unrelated source file changed during promotion")
                continue
            if change.before_sha256 != base_entry.sha256 or change.after_sha256 != candidate_hash:
                raise Conflict("Promotion plan file hashes no longer match its manifests")
            if not self._permitted(path, constraints):
                raise PolicyDenied("Reserved promotion path is outside TaskSpec authority")
            if current_hash == change.after_sha256:
                already_applied = True
            elif current_hash == change.before_sha256:
                pending_paths.append(path)
            else:
                raise Conflict("Source contains a divergent partial promotion effect")
            target = self._target(source.resolve(strict=True), path)
            originals[path] = self.snapshots.artifacts.read(change.before_sha256)
            replacements[path] = self.snapshots.artifacts.read(change.after_sha256)
            if hashlib.sha256(target.read_bytes()).hexdigest() != current_hash:
                raise Conflict("Promotion target changed after source snapshot verification")
            targets[path] = target

        written: list[str] = []
        try:
            for path in pending_paths:
                self._atomic_write(targets[path], replacements[path], "horizon-promote")
                written.append(path)
            promoted, manifest = self.snapshots.capture(
                source,
                denied_paths=constraints.denied_paths,
            )
            if promoted.workspace_revision != plan.candidate_revision:
                raise Conflict("Promoted source does not match the validated candidate revision")
        except BaseException:
            for path in reversed(written):
                if (
                    hashlib.sha256(targets[path].read_bytes()).hexdigest()
                    == changes[path].after_sha256
                ):
                    self._atomic_write(
                        targets[path],
                        originals[path],
                        "horizon-promotion-rollback",
                    )
            raise
        if read_git_head(source) != plan.git_head_before:
            for path in reversed(written):
                if (
                    hashlib.sha256(targets[path].read_bytes()).hexdigest()
                    == changes[path].after_sha256
                ):
                    self._atomic_write(
                        targets[path],
                        originals[path],
                        "horizon-promotion-head-rollback",
                    )
            raise Conflict("Git HEAD changed during promotion")
        return promoted.workspace_revision, manifest, already_applied
