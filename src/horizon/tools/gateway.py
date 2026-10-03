from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path
from uuid import uuid4

from pydantic import Field, model_validator

from horizon.adapters.persistence.artifacts import reject_link
from horizon.adapters.workspace.snapshot import FileEntry, FileSnapshot, SnapshotManager
from horizon.domain.common import Contract, canonical_json, digest
from horizon.domain.errors import Conflict, HorizonError, PolicyDenied
from horizon.domain.model import ToolDefinition
from horizon.domain.plan import WorkItem
from horizon.domain.ports import AcceptanceExecutorPort, CodeRetrievalPort
from horizon.domain.task import TaskSpec, relative_pattern
from horizon.domain.tools import (
    AcceptanceResult,
    PatchRecoveryAssessment,
    ReplaceRecoveryAssessment,
    ToolOutcome,
)

MAX_FILE_BYTES = 256 * 1024
MAX_READ_OUTPUT_CHARS = 32 * 1024
MAX_READ_LINES = 400
MAX_SEARCH_BYTES = 2 * 1024 * 1024
MAX_SEARCH_RESULTS = 30


class SearchArgs(Contract):
    query: str = Field(min_length=1, max_length=200)


class ReadArgs(Contract):
    path: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def valid_range(self):
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError(
                "start_line and end_line must be supplied together; either omit both or use "
                '{"path":"<path>","start_line":<start_line>,"end_line":<end_line>}'
            )
        if self.start_line is None or self.end_line is None:
            return self
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        if self.end_line - self.start_line + 1 > MAX_READ_LINES:
            raise ValueError(f"read_file ranges are limited to {MAX_READ_LINES} lines")
        return self


class RetrieveArgs(Contract):
    query: str = Field(min_length=1, max_length=200)
    max_chunks: int = Field(default=1, ge=1, le=8)


class ReplaceArgs(Contract):
    path: str
    old: str = Field(min_length=1, max_length=64 * 1024)
    new: str = Field(max_length=64 * 1024)
    expected_occurrences: int = Field(default=1, ge=1, le=10)


class PatchEdit(Contract):
    path: str
    old: str = Field(min_length=1, max_length=64 * 1024)
    new: str = Field(max_length=64 * 1024)
    expected_occurrences: int = Field(default=1, ge=1, le=10)

    @model_validator(mode="after")
    def changes_content(self):
        if self.old == self.new:
            raise ValueError("Patch edit old and new text must differ")
        return self


class ApplyPatchArgs(Contract):
    edits: tuple[PatchEdit, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_paths(self):
        paths = [edit.path.casefold() for edit in self.edits]
        if len(paths) != len(set(paths)):
            raise ValueError("Patch edits must target distinct files")
        return self


class RunCheckArgs(Contract):
    check_id: str


class SubmitArgs(Contract):
    summary: str = Field(min_length=1, max_length=2000)


def tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            name="search_repo",
            description=(
                "Search allowed text files for an exact substring. Results are path-ordered and "
                "bounded; use retrieve_code for symbol or concept localization."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="read_file",
            description=(
                "Read one allowed UTF-8 text file. Large files require an inclusive "
                "start_line/end_line range (maximum 400 lines). Never send only one bound: "
                "omit both bounds for a small file, or copy both start_line and end_line from "
                "retrieve_code evidence."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Inclusive 1-based start. Omit with end_line or provide both."
                        ),
                    },
                    "end_line": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Inclusive 1-based end. Omit with start_line or provide both."
                        ),
                    },
                },
                "required": ["path"],
                "dependentRequired": {
                    "start_line": ["end_line"],
                    "end_line": ["start_line"],
                },
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="retrieve_code",
            description=(
                "Preferred tool for locating symbols or concepts: retrieve ranked, "
                "revision-bound lexical code evidence with paths, line ranges, and content "
                "hashes. Returns the top chunk by default; request more only when rank 1 is "
                "ambiguous or insufficient."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_chunks": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="replace_text",
            description="Atomically replace an exact string in one allowed existing file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "expected_occurrences": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["path", "old", "new"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="apply_patch",
            description=(
                "Apply one bounded batch of exact replacements to distinct allowed existing "
                "UTF-8 files. Every edit is validated before the first write."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "old": {"type": "string"},
                                "new": {"type": "string"},
                                "expected_occurrences": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 10,
                                },
                            },
                            "required": ["path", "old", "new"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["edits"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="run_check",
            description="Run one controller-defined acceptance check by ID.",
            parameters={
                "type": "object",
                "properties": {"check_id": {"type": "string"}},
                "required": ["check_id"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="submit",
            description="Request protected final validation after completing the task.",
            parameters={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        ),
    )


class WorkspaceToolGateway:
    def __init__(
        self,
        workspace: Path,
        task: TaskSpec,
        work_item: WorkItem,
        snapshots: SnapshotManager,
        checks: AcceptanceExecutorPort | None,
        retriever: CodeRetrievalPort | None = None,
    ):
        for part in (workspace.absolute(), *workspace.absolute().parents):
            reject_link(part)
        self.workspace = workspace.resolve(strict=True)
        if not self.workspace.is_dir():
            raise PolicyDenied("Tool workspace must be a directory")
        self.task = task
        self.work_item = work_item
        self.snapshots = snapshots
        self.checks = checks
        self.retriever = retriever
        self.last_check_results: dict[str, AcceptanceResult] = {}

    def activate_work_item(self, work_item: WorkItem) -> None:
        known_checks = {check.id for check in self.task.acceptance}
        known_tools = {tool.name for tool in tool_definitions()}
        if (
            not set(work_item.acceptance_ids) <= known_checks
            or not set(work_item.allowed_tools) <= known_tools
        ):
            raise PolicyDenied("WorkItem capabilities are outside the TaskSpec tool gateway")
        self.work_item = work_item
        self.last_check_results = {}

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        allowed = set(self.work_item.allowed_tools) | {"submit"}
        if self.retriever is None:
            allowed.discard("retrieve_code")
        return tuple(tool for tool in tool_definitions() if tool.name in allowed)

    def _permitted(self, path: str) -> bool:
        allowed = any(
            fnmatch.fnmatchcase(path, pattern) for pattern in self.task.constraints.allowed_paths
        )
        denied = any(
            fnmatch.fnmatchcase(path, pattern) for pattern in self.task.constraints.denied_paths
        )
        return allowed and not denied

    def _path(self, value: str) -> Path:
        relative_pattern(value)
        if not self._permitted(value):
            raise PolicyDenied("Path is outside the TaskSpec authority scope")
        target = self.workspace.joinpath(*value.split("/"))
        for part in (target.absolute(), *target.absolute().parents):
            reject_link(part)
            if part == self.workspace:
                break
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(self.workspace) or not resolved.is_file():
            raise PolicyDenied("Tool path must resolve to an existing workspace file")
        return resolved

    def _snapshot(self) -> tuple[str, str]:
        snapshot, manifest = self.snapshots.capture(
            self.workspace,
            denied_paths=self.task.constraints.denied_paths,
        )
        return snapshot.workspace_revision, manifest

    def current_revision(self) -> str:
        revision, _ = self._snapshot()
        return revision

    def checkpoint(self) -> tuple[str, str]:
        return self._snapshot()

    def verify_manifest(self, manifest_hash: str):
        return self.snapshots.verify(manifest_hash)

    def validation_evidence(self, results: tuple[AcceptanceResult, ...]) -> str:
        payload = [result.model_dump(mode="json") for result in results]
        return self.snapshots.artifacts.put(canonical_json(payload).encode("utf-8"))

    def _outcome(
        self,
        status: str,
        content: str,
        before: str | None,
        after: str | None,
        workspace_manifest_ref: str | None = None,
    ) -> ToolOutcome:
        if not content:
            content = "Tool completed without textual output."
        output_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        output_ref = self.snapshots.artifacts.put(content.encode("utf-8"))
        return ToolOutcome(
            status=status,
            content=content,
            output_hash=output_hash,
            workspace_revision_before=before,
            workspace_revision_after=after,
            artifact_ref=output_ref,
            workspace_manifest_ref=workspace_manifest_ref,
        )

    def _search(self, arguments: dict) -> ToolOutcome:
        args = SearchArgs.model_validate(arguments)
        before, manifest = self._snapshot()
        matches: list[str] = []
        scanned = 0
        limit_reason: str | None = None
        for path in sorted(self.workspace.rglob("*")):
            if len(matches) >= MAX_SEARCH_RESULTS:
                limit_reason = "result_limit"
                break
            if not path.is_file() or path.is_symlink() or path.is_junction():
                continue
            rel = path.relative_to(self.workspace).as_posix()
            if not self._permitted(rel):
                continue
            content = path.read_bytes()
            scanned += len(content)
            if scanned > MAX_SEARCH_BYTES:
                limit_reason = "byte_limit"
                break
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if args.query in line:
                    matches.append(f"{rel}:{line_number}:{line[:300]}")
                    if len(matches) >= MAX_SEARCH_RESULTS:
                        limit_reason = "result_limit"
                        break
        result = "\n".join(matches) if matches else "NO_MATCHES"
        if limit_reason is not None:
            result += (
                f"\nSEARCH_TRUNCATED reason={limit_reason}; results are path-ordered. "
                "Use retrieve_code for ranked symbol or concept localization."
            )
        return self._outcome("success", result, before, before, manifest)

    def _read(self, arguments: dict) -> ToolOutcome:
        args = ReadArgs.model_validate(arguments)
        before, manifest = self._snapshot()
        content = self._path(args.path).read_bytes()
        if len(content) > MAX_FILE_BYTES:
            raise PolicyDenied("File exceeds the bounded read limit")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PolicyDenied("Only UTF-8 text files are supported") from exc
        lines = text.splitlines(keepends=True)
        if args.start_line is None:
            if len(text) > MAX_READ_OUTPUT_CHARS:
                raise PolicyDenied(
                    f"File has {len(lines)} lines and {len(text)} characters; unbounded "
                    f"read_file output is limited to {MAX_READ_OUTPUT_CHARS} characters. "
                    f"Use start_line and end_line (maximum {MAX_READ_LINES} lines), preferably "
                    "from retrieve_code evidence."
                )
            return self._outcome("success", text or "EMPTY_FILE", before, before, manifest)

        assert args.end_line is not None
        if args.start_line > len(lines):
            raise PolicyDenied(
                f"start_line {args.start_line} exceeds the file's {len(lines)} lines"
            )
        actual_end = min(args.end_line, len(lines))
        selected = "".join(lines[args.start_line - 1 : actual_end])
        header = (
            f"READ_RANGE path={args.path} lines={args.start_line}-{actual_end} "
            f"total_lines={len(lines)}\n"
        )
        result = header + selected
        if len(result) > MAX_READ_OUTPUT_CHARS:
            raise PolicyDenied(
                f"Selected range produces {len(result)} characters; read_file output is "
                f"limited to {MAX_READ_OUTPUT_CHARS}. Use a smaller line range."
            )
        return self._outcome("success", result, before, before, manifest)

    def _retrieve(self, arguments: dict) -> ToolOutcome:
        if self.retriever is None:
            raise PolicyDenied("Code retrieval is unavailable")
        args = RetrieveArgs.model_validate(arguments)
        before, manifest = self._snapshot()
        evidence = self.retriever.retrieve(
            source_manifest_ref=manifest,
            workspace_revision=before,
            allowed_paths=self.task.constraints.allowed_paths,
            denied_paths=self.task.constraints.denied_paths,
            query=args.query,
            max_chunks=args.max_chunks,
        )
        after, after_manifest = self._snapshot()
        if after != before:
            raise Conflict("Workspace changed while code evidence was being retrieved")
        if after_manifest != manifest:
            raise Conflict("Stable workspace produced a different retrieval manifest")
        return self._outcome(
            "success",
            canonical_json(evidence),
            before,
            after,
            manifest,
        )

    def _replace(self, arguments: dict) -> ToolOutcome:
        if self.task.execution_mode != "workspace_write":
            raise PolicyDenied("Task execution mode does not permit writes")
        args = ReplaceArgs.model_validate(arguments)
        target = self._path(args.path)
        before, _ = self._snapshot()
        original = target.read_bytes()
        encoded = self._replacement_bytes(original, args)
        try:
            self._atomic_write(target, encoded, "tmp")
            after, manifest = self._snapshot()
        except BaseException:
            if target.read_bytes() != original:
                self._atomic_write(target, original, "rollback")
            raise
        return self._outcome(
            "success",
            f"Replaced {args.expected_occurrences} occurrence(s) in {args.path}.",
            before,
            after,
            manifest,
        )

    def _apply_patch(self, arguments: dict) -> ToolOutcome:
        if self.task.execution_mode != "workspace_write":
            raise PolicyDenied("Task execution mode does not permit writes")
        args = ApplyPatchArgs.model_validate(arguments)
        before, pre_manifest = self._snapshot()
        _, pre, expected, originals, updates = self._expected_patch_snapshot(
            arguments,
            pre_manifest,
        )
        if pre.workspace_revision != before:
            raise Conflict("Patch pre-dispatch manifest does not match the live workspace")
        targets = {edit.path: self._path(edit.path) for edit in args.edits}
        if any(targets[path].read_bytes() != original for path, original in originals.items()):
            raise Conflict("Workspace changed while the patch was being prepared")

        written: list[str] = []
        try:
            for edit in args.edits:
                self._atomic_write(targets[edit.path], updates[edit.path], "patch")
                written.append(edit.path)
            after, manifest = self._snapshot()
            if after != expected.workspace_revision:
                raise Conflict("Workspace changed concurrently while applying the patch")
        except BaseException:
            self._restore_patch_files(
                targets,
                written,
                expected_contents=updates,
                restore_contents=originals,
                suffix="patch-rollback",
            )
            raise
        return self._outcome(
            "success",
            f"Applied {len(args.edits)} exact edit(s) across {len(targets)} file(s).",
            before,
            after,
            manifest,
        )

    @staticmethod
    def _replacement_bytes(original: bytes, args: ReplaceArgs) -> bytes:
        if len(original) > MAX_FILE_BYTES:
            raise PolicyDenied("File exceeds the bounded write limit")
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PolicyDenied("Only UTF-8 text files are supported") from exc
        if text.count(args.old) != args.expected_occurrences:
            raise ValueError("Exact replacement occurrence count did not match")
        updated = text.replace(args.old, args.new)
        encoded = updated.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise PolicyDenied("Updated file exceeds the bounded write limit")
        return encoded

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

    def _expected_replace_snapshot(
        self,
        arguments: dict,
        pre_manifest_ref: str,
    ) -> tuple[ReplaceArgs, FileSnapshot, FileSnapshot, bytes, bytes]:
        if self.task.execution_mode != "workspace_write":
            raise PolicyDenied("Task execution mode does not permit writes")
        args = ReplaceArgs.model_validate(arguments)
        self._path(args.path)
        pre = self.snapshots.verify(pre_manifest_ref)
        entries = list(pre.files)
        index = next((i for i, entry in enumerate(entries) if entry.path == args.path), None)
        if index is None:
            raise Conflict("Replace target is absent from the pre-dispatch manifest")
        original = self.snapshots.artifacts.read(entries[index].sha256)
        updated = self._replacement_bytes(original, args)
        entries[index] = FileEntry(
            path=entries[index].path,
            sha256=hashlib.sha256(updated).hexdigest(),
            size_bytes=len(updated),
            executable=entries[index].executable,
        )
        expected = FileSnapshot(
            files=tuple(entries),
            excluded_paths=pre.excluded_paths,
            workspace_revision=digest([entry.model_dump(mode="json") for entry in entries]),
        )
        return args, pre, expected, original, updated

    def _expected_patch_snapshot(
        self,
        arguments: dict,
        pre_manifest_ref: str,
    ) -> tuple[
        ApplyPatchArgs,
        FileSnapshot,
        FileSnapshot,
        dict[str, bytes],
        dict[str, bytes],
    ]:
        if self.task.execution_mode != "workspace_write":
            raise PolicyDenied("Task execution mode does not permit writes")
        args = ApplyPatchArgs.model_validate(arguments)
        for edit in args.edits:
            self._path(edit.path)
        pre = self.snapshots.verify(pre_manifest_ref)
        entries = list(pre.files)
        indexes = {entry.path: index for index, entry in enumerate(entries)}
        originals: dict[str, bytes] = {}
        updates: dict[str, bytes] = {}
        for edit in args.edits:
            index = indexes.get(edit.path)
            if index is None:
                raise Conflict("Patch target is absent from the pre-dispatch manifest")
            original = self.snapshots.artifacts.read(entries[index].sha256)
            updated = self._replacement_bytes(
                original,
                ReplaceArgs(
                    path=edit.path,
                    old=edit.old,
                    new=edit.new,
                    expected_occurrences=edit.expected_occurrences,
                ),
            )
            originals[edit.path] = original
            updates[edit.path] = updated
            entries[index] = FileEntry(
                path=entries[index].path,
                sha256=hashlib.sha256(updated).hexdigest(),
                size_bytes=len(updated),
                executable=entries[index].executable,
            )
        expected = FileSnapshot(
            files=tuple(entries),
            excluded_paths=pre.excluded_paths,
            workspace_revision=digest([entry.model_dump(mode="json") for entry in entries]),
        )
        return args, pre, expected, originals, updates

    @classmethod
    def _restore_patch_files(
        cls,
        targets: dict[str, Path],
        paths: list[str],
        *,
        expected_contents: dict[str, bytes],
        restore_contents: dict[str, bytes],
        suffix: str,
    ) -> None:
        for path in reversed(paths):
            target = targets[path]
            if target.read_bytes() == expected_contents[path]:
                cls._atomic_write(target, restore_contents[path], suffix)

    def assess_replace_recovery(
        self,
        arguments: dict,
        pre_manifest_ref: str,
    ) -> ReplaceRecoveryAssessment:
        args, pre, expected, _, _ = self._expected_replace_snapshot(
            arguments,
            pre_manifest_ref,
        )
        current, current_manifest = self.snapshots.capture(
            self.workspace,
            denied_paths=self.task.constraints.denied_paths,
        )
        if current.workspace_revision == pre.workspace_revision:
            state = "pre_effect"
        elif current.workspace_revision == expected.workspace_revision:
            state = "expected_effect"
        else:
            state = "diverged"
        return ReplaceRecoveryAssessment(
            state=state,
            path=args.path,
            pre_revision=pre.workspace_revision,
            expected_revision=expected.workspace_revision,
            current_revision=current.workspace_revision,
            current_manifest_ref=current_manifest,
        )

    def rollback_replace_recovery(
        self,
        arguments: dict,
        pre_manifest_ref: str,
        expected_current_revision: str,
    ) -> tuple[str, str]:
        args, pre, expected, original, updated = self._expected_replace_snapshot(
            arguments,
            pre_manifest_ref,
        )
        assessment = self.assess_replace_recovery(arguments, pre_manifest_ref)
        if assessment.current_revision != expected_current_revision:
            raise Conflict("Workspace changed after replace recovery assessment")
        if assessment.state == "diverged":
            raise Conflict("Diverged replace effect cannot be rolled back automatically")
        if assessment.state == "pre_effect":
            return assessment.current_revision, assessment.current_manifest_ref

        target = self._path(args.path)
        if target.read_bytes() != updated:
            raise Conflict("Replace target changed after recovery assessment")
        self._atomic_write(target, original, "recovery-rollback")
        restored, manifest = self.snapshots.capture(
            self.workspace,
            denied_paths=self.task.constraints.denied_paths,
        )
        if restored.workspace_revision != pre.workspace_revision:
            if target.read_bytes() == original:
                self._atomic_write(target, updated, "recovery-revert")
            raise Conflict("Workspace changed concurrently during replace rollback")
        if expected.workspace_revision != expected_current_revision:
            raise Conflict("Replace recovery expectation changed during rollback")
        return restored.workspace_revision, manifest

    def assess_patch_recovery(
        self,
        arguments: dict,
        pre_manifest_ref: str,
    ) -> PatchRecoveryAssessment:
        args, pre, expected, _, _ = self._expected_patch_snapshot(
            arguments,
            pre_manifest_ref,
        )
        current, current_manifest = self.snapshots.capture(
            self.workspace,
            denied_paths=self.task.constraints.denied_paths,
        )
        if current.workspace_revision == pre.workspace_revision:
            state = "pre_effect"
        elif current.workspace_revision == expected.workspace_revision:
            state = "expected_effect"
        else:
            state = "diverged"
        return PatchRecoveryAssessment(
            state=state,
            paths=tuple(edit.path for edit in args.edits),
            pre_revision=pre.workspace_revision,
            expected_revision=expected.workspace_revision,
            current_revision=current.workspace_revision,
            current_manifest_ref=current_manifest,
        )

    def rollback_patch_recovery(
        self,
        arguments: dict,
        pre_manifest_ref: str,
        expected_current_revision: str,
    ) -> tuple[str, str]:
        args, pre, expected, originals, updates = self._expected_patch_snapshot(
            arguments,
            pre_manifest_ref,
        )
        assessment = self.assess_patch_recovery(arguments, pre_manifest_ref)
        if assessment.current_revision != expected_current_revision:
            raise Conflict("Workspace changed after patch recovery assessment")
        if assessment.state == "diverged":
            raise Conflict("Diverged patch effect cannot be rolled back automatically")
        if assessment.state == "pre_effect":
            return assessment.current_revision, assessment.current_manifest_ref

        targets = {edit.path: self._path(edit.path) for edit in args.edits}
        if any(targets[path].read_bytes() != updated for path, updated in updates.items()):
            raise Conflict("Patch target changed after recovery assessment")
        restored_paths: list[str] = []
        try:
            for edit in args.edits:
                self._atomic_write(
                    targets[edit.path],
                    originals[edit.path],
                    "patch-recovery-rollback",
                )
                restored_paths.append(edit.path)
            restored, manifest = self.snapshots.capture(
                self.workspace,
                denied_paths=self.task.constraints.denied_paths,
            )
            if restored.workspace_revision != pre.workspace_revision:
                raise Conflict("Workspace changed concurrently during patch rollback")
        except BaseException:
            self._restore_patch_files(
                targets,
                restored_paths,
                expected_contents=originals,
                restore_contents=updates,
                suffix="patch-recovery-revert",
            )
            raise
        if expected.workspace_revision != expected_current_revision:
            raise Conflict("Patch recovery expectation changed during rollback")
        return restored.workspace_revision, manifest

    def _execute_check(
        self,
        check_id: str,
        *,
        require_work_item_scope: bool,
        attempt_id: str | None = None,
    ) -> ToolOutcome:
        checks = {check.id: check for check in self.task.acceptance}
        check = checks.get(check_id)
        if check is None or (
            require_work_item_scope and check_id not in self.work_item.acceptance_ids
        ):
            raise PolicyDenied("Unknown or unauthorized acceptance check ID")
        if self.checks is None:
            raise PolicyDenied("Acceptance execution is unavailable in recovery-only mode")
        before, _ = self._snapshot()
        durable_execute = getattr(self.checks, "execute_attempt", None)
        if attempt_id is not None and durable_execute is not None:
            result = durable_execute(self.workspace, check, attempt_id)
        else:
            result = self.checks.execute(self.workspace, check)
        after, manifest = self._snapshot()
        self.last_check_results[check.id] = result
        content = (
            f"check_id={check.id}\npassed={str(result.passed).lower()}\n"
            f"exit_code={result.exit_code}\ntimed_out={str(result.timed_out).lower()}\n"
            f"output_truncated={str(result.output_truncated).lower()}\n{result.output}"
        )
        return self._outcome(
            "success" if result.passed else "error",
            content,
            before,
            after,
            manifest,
        )

    def _run_check(self, arguments: dict, attempt_id: str | None = None) -> ToolOutcome:
        args = RunCheckArgs.model_validate(arguments)
        return self._execute_check(
            args.check_id,
            require_work_item_scope=True,
            attempt_id=attempt_id,
        )

    def dispatch_protected_check(
        self,
        check_id: str,
        attempt_id: str | None = None,
    ) -> ToolOutcome:
        return self._execute_check(
            check_id,
            require_work_item_scope=False,
            attempt_id=attempt_id,
        )

    def _submit(self, arguments: dict) -> ToolOutcome:
        args = SubmitArgs.model_validate(arguments)
        revision, manifest = self._snapshot()
        return self._outcome(
            "success",
            f"Validation requested: {args.summary}",
            revision,
            revision,
            manifest,
        )

    def dispatch(
        self,
        name: str,
        arguments: dict,
        attempt_id: str | None = None,
    ) -> ToolOutcome:
        allowed = {tool.name for tool in self.definitions}
        if name not in allowed:
            raise PolicyDenied("Tool is not allowed by the current WorkItem")
        handlers = {
            "search_repo": self._search,
            "read_file": self._read,
            "retrieve_code": self._retrieve,
            "replace_text": self._replace,
            "apply_patch": self._apply_patch,
            "submit": self._submit,
        }
        if name == "run_check":
            return self._run_check(arguments, attempt_id)
        return handlers[name](arguments)

    def dispatch_safe(
        self,
        name: str,
        arguments: dict,
        attempt_id: str | None = None,
    ) -> ToolOutcome:
        try:
            return self.dispatch(name, arguments, attempt_id)
        except (HorizonError, ValueError) as exc:
            revision = self.current_revision()
            return self._outcome(
                "error",
                f"{type(exc).__name__}: {exc}",
                revision,
                revision,
            )
