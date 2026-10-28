from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import yaml

from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.application.terminal_suite import (
    TerminalSuiteResult,
    export_terminal_suite,
    verify_terminal_suite_pack,
)
from horizon.domain.agent_batch import (
    AgentBatchCase,
    AgentBatchCaseRecord,
    AgentBatchManifest,
    AgentBatchState,
    AgentRunLaunchReceipt,
)
from horizon.domain.common import canonical_json, digest
from horizon.domain.plan import Plan
from horizon.domain.run import Run
from horizon.domain.task import TaskSpec
from horizon.domain.terminal_suite import TerminalSuiteCase, TerminalSuiteManifest


@dataclass(frozen=True)
class ResolvedAgentBatchCase:
    spec: AgentBatchCase
    task_path: Path
    plan_path: Path | None
    pilot_preflight_path: Path | None
    task: TaskSpec
    plan: Plan | None

    @property
    def template_task_id(self) -> str:
        return self.task.task_id


@dataclass(frozen=True)
class AgentBatchExecutionRequest:
    manifest: AgentBatchManifest
    case: ResolvedAgentBatchCase
    invocation_id: str
    expected_task_id: str
    launch_receipt_path: Path
    stdout_path: Path
    stderr_path: Path


AgentBatchExecutor = Callable[[AgentBatchExecutionRequest], None]


@dataclass(frozen=True)
class AgentBatchResult:
    output_dir: Path
    manifest_path: Path
    state_path: Path
    state: AgentBatchState
    terminal_suite: TerminalSuiteResult | None
    launched_case_count: int


def agent_batch_invocation_id(manifest_digest: str, case_id: str, attempt: int) -> str:
    return (
        f"batch_{digest({'manifest': manifest_digest, 'case': case_id, 'attempt': attempt})[:32]}"
    )


def agent_batch_task_id(template_task_id: str, invocation_id: str) -> str:
    suffix = digest({"invocation_id": invocation_id})[:12]
    prefix = template_task_id[: 120 - len(suffix) - 1]
    return f"{prefix}-{suffix}"


def write_agent_run_launch_receipt(path: Path, receipt: AgentRunLaunchReceipt) -> None:
    """Publish a Run mapping once so a batch can recover after its controller exits."""

    content = (canonical_json(receipt) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = AgentRunLaunchReceipt.model_validate_json(path.read_text(encoding="utf-8"))
        if existing != receipt:
            raise ValueError("Agent Run launch receipt already exists with different content")
        return
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = AgentRunLaunchReceipt.model_validate_json(path.read_text(encoding="utf-8"))
            if existing != receipt:
                raise ValueError("Agent Run launch receipt raced with different content") from None
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _resolve_member(root: Path, relative: str, *, label: str) -> Path:
    candidate = (root / relative).resolve(strict=True)
    if not candidate.is_relative_to(root):
        raise ValueError(f"Agent-batch {label} escapes the manifest directory")
    if not candidate.is_file():
        raise ValueError(f"Agent-batch {label} must be a file")
    return candidate


def resolve_agent_batch_cases(
    manifest_path: Path,
    manifest: AgentBatchManifest,
) -> tuple[ResolvedAgentBatchCase, ...]:
    root = manifest_path.resolve(strict=True).parent
    resolved = []
    for case in manifest.cases:
        task_path = _resolve_member(root, case.task_path, label="TaskSpec")
        task = TaskSpec.model_validate(yaml.safe_load(task_path.read_text(encoding="utf-8")))
        plan_path = (
            _resolve_member(root, case.plan_path, label="Plan")
            if case.plan_path is not None
            else None
        )
        pilot_path = (
            _resolve_member(root, case.pilot_preflight_path, label="pilot preflight")
            if case.pilot_preflight_path is not None
            else None
        )
        plan = (
            Plan.model_validate(yaml.safe_load(plan_path.read_text(encoding="utf-8")))
            if plan_path is not None
            else None
        )
        if plan is not None:
            plan.check_task(task)
        resolved.append(
            ResolvedAgentBatchCase(
                spec=case,
                task_path=task_path,
                plan_path=plan_path,
                pilot_preflight_path=pilot_path,
                task=task,
                plan=plan,
            )
        )
    return tuple(resolved)


class SequentialAgentBatchRunner:
    """Run independent Agent cases in manifest order and checkpoint between cases."""

    def __init__(self, store: SQLiteEventStore, executor: AgentBatchExecutor):
        self.store = store
        self.executor = executor

    @staticmethod
    def _state_content(state: AgentBatchState) -> bytes:
        return (canonical_json(state) + "\n").encode("utf-8")

    @staticmethod
    def _state_path(output: Path) -> Path:
        return output / "batch-state.json"

    @staticmethod
    def _manifest_path(output: Path) -> Path:
        return output / "batch-manifest.json"

    @staticmethod
    def _case_root(output: Path, record: AgentBatchCaseRecord) -> Path:
        return output / "cases" / f"{record.sequence:03d}-{record.case_id}"

    def _save(self, output: Path, state: AgentBatchState) -> None:
        _atomic_write(self._state_path(output), self._state_content(state))

    @staticmethod
    def _replace_case(
        state: AgentBatchState,
        sequence: int,
        replacement: AgentBatchCaseRecord,
    ) -> AgentBatchState:
        cases = list(state.cases)
        cases[sequence - 1] = replacement
        return AgentBatchState(
            batch_id=state.batch_id,
            manifest_digest=state.manifest_digest,
            revision=state.revision + 1,
            cases=tuple(cases),
        )

    @staticmethod
    def _error(exc: Exception) -> tuple[str, str]:
        error_type = type(exc).__name__ or "Exception"
        message = str(exc).strip() or error_type
        return error_type[:120], message

    def _find_run_by_task_id(self, task_id: str) -> Run | None:
        matches = [
            payload["run_id"]
            for payload in self.store.list_runs()
            if payload["task"]["task_id"] == task_id
        ]
        if len(matches) > 1:
            raise ValueError("Agent-batch invocation resolved multiple Runs")
        return self.store.get(matches[0]) if matches else None

    def _resolve_run(
        self,
        output: Path,
        resolved: ResolvedAgentBatchCase,
        record: AgentBatchCaseRecord,
    ) -> Run | None:
        if record.invocation_id is None:
            return None
        expected_task_id = agent_batch_task_id(
            resolved.template_task_id,
            record.invocation_id,
        )
        receipt_path = self._case_root(output, record) / "run-receipt.json"
        if receipt_path.is_file():
            receipt = AgentRunLaunchReceipt.model_validate_json(
                receipt_path.read_text(encoding="utf-8")
            )
            if receipt.invocation_id != record.invocation_id:
                raise ValueError("Agent-batch launch receipt has the wrong invocation")
            if receipt.task_id != expected_task_id:
                raise ValueError("Agent-batch launch receipt has the wrong Task identity")
            run = self.store.get(receipt.run_id)
            if run.task.task_id != receipt.task_id or run.task.sha256 != receipt.task_spec_hash:
                raise ValueError("Agent-batch launch receipt does not match its Run")
            return run
        return self._find_run_by_task_id(expected_task_id)

    @staticmethod
    def _from_run(
        record: AgentBatchCaseRecord,
        run: Run,
        *,
        error: tuple[str, str] | None = None,
    ) -> AgentBatchCaseRecord:
        if run.terminal:
            return AgentBatchCaseRecord(
                sequence=record.sequence,
                case_id=record.case_id,
                state="terminal",
                attempt_count=record.attempt_count,
                invocation_id=record.invocation_id,
                run_id=run.run_id,
                run_status=run.status,
                failure_reason=run.failure_reason,
                last_error_type=error[0] if error else None,
                last_error_message=error[1] if error else None,
            )
        return AgentBatchCaseRecord(
            sequence=record.sequence,
            case_id=record.case_id,
            state="attention",
            attempt_count=record.attempt_count,
            invocation_id=record.invocation_id,
            run_id=run.run_id,
            run_status=run.status,
            last_error_type=(error[0] if error else "NonTerminalRun"),
            last_error_message=(
                error[1]
                if error
                else "The case stopped before a terminal state and blocks later cases"
            ),
        )

    def _reconcile_record(
        self,
        output: Path,
        resolved: ResolvedAgentBatchCase,
        record: AgentBatchCaseRecord,
    ) -> AgentBatchCaseRecord:
        if record.state == "pending":
            return record
        run = self._resolve_run(output, resolved, record)
        if run is None:
            return AgentBatchCaseRecord(
                sequence=record.sequence,
                case_id=record.case_id,
                state="pending",
                attempt_count=record.attempt_count,
                last_error_type=record.last_error_type,
                last_error_message=record.last_error_message,
            )
        return self._from_run(record, run)

    def _load_or_create(
        self,
        manifest: AgentBatchManifest,
        output: Path,
        *,
        resume: bool,
    ) -> AgentBatchState:
        manifest_path = self._manifest_path(output)
        state_path = self._state_path(output)
        if resume:
            if not output.is_dir() or not manifest_path.is_file() or not state_path.is_file():
                raise ValueError("Agent-batch resume requires an existing complete checkpoint")
            stored_manifest = AgentBatchManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
            if stored_manifest != manifest:
                raise ValueError("Agent-batch manifest changed after the first invocation")
            state = AgentBatchState.model_validate_json(state_path.read_text(encoding="utf-8"))
            if state.batch_id != manifest.batch_id or state.manifest_digest != manifest.sha256:
                raise ValueError("Agent-batch state does not match its manifest")
            return state
        if output.exists():
            raise FileExistsError(f"Agent-batch output already exists: {output}")
        output.mkdir(parents=True, exist_ok=False)
        (output / "cases").mkdir()
        _write_new(manifest_path, (canonical_json(manifest) + "\n").encode("utf-8"))
        state = AgentBatchState(
            batch_id=manifest.batch_id,
            manifest_digest=manifest.sha256,
            revision=0,
            cases=tuple(
                AgentBatchCaseRecord(sequence=sequence, case_id=case.case_id)
                for sequence, case in enumerate(manifest.cases, start=1)
            ),
        )
        _write_new(state_path, self._state_content(state))
        return state

    def _verify_complete(
        self,
        output: Path,
        manifest: AgentBatchManifest,
        state: AgentBatchState,
    ) -> None:
        assert state.terminal_suite_path is not None
        pack = verify_terminal_suite_pack(output / state.terminal_suite_path)
        expected = [(case.case_id, case.run_id) for case in state.cases]
        actual = [(case.case_id, case.run_id) for case in pack.report.cases]
        if pack.report.suite_id != manifest.batch_id or actual != expected:
            raise ValueError("Completed Agent-batch suite does not match its checkpoint")

    def _finalize(
        self,
        output: Path,
        manifest: AgentBatchManifest,
        state: AgentBatchState,
    ) -> tuple[AgentBatchState, TerminalSuiteResult | None]:
        suite_dir = output / "terminal-suite"
        pack_path = suite_dir / "suite-pack.json"
        terminal_manifest = TerminalSuiteManifest(
            suite_id=manifest.batch_id,
            cases=tuple(
                TerminalSuiteCase(case_id=case.case_id, run_id=case.run_id)
                for case in state.cases
                if case.run_id is not None
            ),
        )
        suite_result = None
        if pack_path.is_file():
            pack = verify_terminal_suite_pack(pack_path)
            expected = [(case.case_id, case.run_id) for case in state.cases]
            actual = [(case.case_id, case.run_id) for case in pack.report.cases]
            if pack.report.suite_id != manifest.batch_id or actual != expected:
                raise ValueError("Existing Agent-batch terminal suite has different cases")
        elif suite_dir.exists():
            raise ValueError("Agent-batch terminal suite directory is incomplete")
        else:
            suite_result = export_terminal_suite(self.store, terminal_manifest, suite_dir)
        completed = AgentBatchState(
            batch_id=state.batch_id,
            manifest_digest=state.manifest_digest,
            revision=state.revision + 1,
            complete=True,
            terminal_suite_path="terminal-suite/suite-pack.json",
            cases=state.cases,
        )
        self._save(output, completed)
        self._verify_complete(output, manifest, completed)
        return completed, suite_result

    def run(
        self,
        manifest_path: Path,
        manifest: AgentBatchManifest,
        output_dir: Path,
        *,
        resume: bool = False,
        max_cases_this_invocation: int | None = None,
    ) -> AgentBatchResult:
        if max_cases_this_invocation is not None and not 1 <= max_cases_this_invocation <= 100:
            raise ValueError("Agent-batch invocation case limit must be in [1, 100]")
        resolved_cases = resolve_agent_batch_cases(manifest_path, manifest)
        output = output_dir.resolve()
        state = self._load_or_create(manifest, output, resume=resume)
        if state.complete:
            self._verify_complete(output, manifest, state)
            return AgentBatchResult(
                output_dir=output,
                manifest_path=self._manifest_path(output),
                state_path=self._state_path(output),
                state=state,
                terminal_suite=None,
                launched_case_count=0,
            )

        launched = 0
        for resolved, current in zip(resolved_cases, state.cases, strict=True):
            reconciled = self._reconcile_record(output, resolved, current)
            if reconciled != current:
                state = self._replace_case(state, current.sequence, reconciled)
                self._save(output, state)
            if reconciled.state == "terminal":
                continue
            if reconciled.state == "attention":
                break
            if max_cases_this_invocation is not None and launched >= max_cases_this_invocation:
                break

            attempt = reconciled.attempt_count + 1
            invocation_id = agent_batch_invocation_id(
                manifest.sha256,
                reconciled.case_id,
                attempt,
            )
            running = AgentBatchCaseRecord(
                sequence=reconciled.sequence,
                case_id=reconciled.case_id,
                state="running",
                attempt_count=attempt,
                invocation_id=invocation_id,
            )
            state = self._replace_case(state, reconciled.sequence, running)
            self._save(output, state)
            case_root = self._case_root(output, running)
            case_root.mkdir(parents=True, exist_ok=True)
            request = AgentBatchExecutionRequest(
                manifest=manifest,
                case=resolved,
                invocation_id=invocation_id,
                expected_task_id=agent_batch_task_id(
                    resolved.template_task_id,
                    invocation_id,
                ),
                launch_receipt_path=case_root / "run-receipt.json",
                stdout_path=case_root / "stdout.log",
                stderr_path=case_root / "stderr.log",
            )
            error = None
            try:
                self.executor(request)
            except Exception as exc:  # The Run state, not the Python exception, decides progress.
                error = self._error(exc)
            launched += 1
            run = self._resolve_run(output, resolved, running)
            if run is None:
                updated = AgentBatchCaseRecord(
                    sequence=running.sequence,
                    case_id=running.case_id,
                    state="pending",
                    attempt_count=running.attempt_count,
                    last_error_type=(error[0] if error else "MissingRunReceipt"),
                    last_error_message=(
                        error[1]
                        if error
                        else "Agent case returned without creating a recoverable Run"
                    ),
                )
            else:
                updated = self._from_run(running, run, error=error)
            state = self._replace_case(state, running.sequence, updated)
            self._save(output, state)
            if updated.state != "terminal":
                break

        suite_result = None
        if all(case.state == "terminal" for case in state.cases):
            state, suite_result = self._finalize(output, manifest, state)
        return AgentBatchResult(
            output_dir=output,
            manifest_path=self._manifest_path(output),
            state_path=self._state_path(output),
            state=state,
            terminal_suite=suite_result,
            launched_case_count=launched,
        )
