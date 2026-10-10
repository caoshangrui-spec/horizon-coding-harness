from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.application.terminal_evidence import (
    export_terminal_evidence_pack,
    verify_terminal_evidence_pack,
)
from horizon.domain.common import canonical_json
from horizon.domain.run import Run
from horizon.domain.states import RunStatus
from horizon.domain.terminal_evidence import TerminalEvidencePack
from horizon.domain.terminal_suite import (
    TERMINAL_SUITE_BOUNDARIES,
    TerminalSuiteCase,
    TerminalSuiteCaseResult,
    TerminalSuiteFile,
    TerminalSuiteManifest,
    TerminalSuitePack,
    TerminalSuiteReasonCount,
    TerminalSuiteReport,
)


@dataclass(frozen=True)
class TerminalSuiteResult:
    output_dir: Path
    suite_pack_path: Path
    manifest_path: Path
    report_path: Path
    summary_path: Path
    suite_pack: TerminalSuitePack


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)


def _resolve_member(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve(strict=True)
    if not candidate.is_relative_to(root):
        raise ValueError("Terminal-suite file escapes its evidence directory")
    return candidate


def _file_record(root: Path, role: str, path: Path) -> TerminalSuiteFile:
    content = path.read_bytes()
    return TerminalSuiteFile(
        role=role,
        path=path.relative_to(root).as_posix(),
        sha256=_sha256(content),
        bytes=len(content),
    )


def _case_pack_path(case: TerminalSuiteCase, sequence: int) -> str:
    return f"cases/{sequence:03d}-{case.case_id}/evidence-pack.json"


def _replay_child(pack_path: Path, pack: TerminalEvidencePack) -> Run:
    trace = next(item for item in pack.files if item.role == "trace")
    trace_path = _resolve_member(pack_path.parent, trace.path)
    return SQLiteEventStore.replay_jsonl(trace_path.read_text(encoding="utf-8"))


def _case_result(
    case: TerminalSuiteCase,
    sequence: int,
    root: Path,
    pack_path: Path,
    pack: TerminalEvidencePack,
) -> TerminalSuiteCaseResult:
    run = _replay_child(pack_path, pack)
    evidence = pack.evidence
    if evidence.run_id != case.run_id or run.run_id != case.run_id:
        raise ValueError(f"Terminal-suite case {case.case_id} resolved the wrong Run")
    relative_pack = pack_path.relative_to(root).as_posix()
    expected_pack = _case_pack_path(case, sequence)
    if relative_pack != expected_pack:
        raise ValueError(f"Terminal-suite case {case.case_id} has a noncanonical pack path")
    pack_content = pack_path.read_bytes()
    return TerminalSuiteCaseResult(
        sequence=sequence,
        case_id=case.case_id,
        run_id=evidence.run_id,
        task_id=evidence.task_id,
        status=evidence.status,
        task_succeeded=evidence.task_succeeded,
        failure_reason=evidence.failure_reason,
        budget_stop_reason=(run.budget_stop.reason_code if run.budget_stop is not None else None),
        event_count=evidence.event_count,
        event_hash=evidence.event_hash,
        projection_hash=evidence.projection_hash,
        unknown_effects_present=evidence.unknown_effects_present,
        open_effects_present=evidence.open_effects_present,
        evidence_pack_path=relative_pack,
        evidence_pack_sha256=_sha256(pack_content),
        evidence_pack_bytes=len(pack_content),
    )


def _reason_counts(values: list[str]) -> tuple[TerminalSuiteReasonCount, ...]:
    return tuple(
        TerminalSuiteReasonCount(reason=reason, run_count=values.count(reason))
        for reason in sorted(set(values))
    )


def _build_report(
    manifest: TerminalSuiteManifest,
    cases: tuple[TerminalSuiteCaseResult, ...],
) -> TerminalSuiteReport:
    succeeded = sum(case.status == RunStatus.SUCCEEDED for case in cases)
    failure_reasons = [case.failure_reason for case in cases if case.failure_reason is not None]
    budget_stop_reasons = [
        case.budget_stop_reason.value for case in cases if case.budget_stop_reason is not None
    ]
    return TerminalSuiteReport(
        suite_id=manifest.suite_id,
        manifest_digest=manifest.sha256,
        case_count=len(cases),
        succeeded_run_count=succeeded,
        failed_run_count=sum(case.status == RunStatus.FAILED for case in cases),
        cancelled_run_count=sum(case.status == RunStatus.CANCELLED for case in cases),
        task_success_rate=round(succeeded / len(cases), 6),
        terminal_capture_verified_count=sum(case.terminal_capture_verified for case in cases),
        unknown_effect_run_count=sum(case.unknown_effects_present for case in cases),
        open_effect_run_count=sum(case.open_effects_present for case in cases),
        failure_reason_counts=_reason_counts(failure_reasons),
        budget_stop_reason_counts=_reason_counts(budget_stop_reasons),
        cases=cases,
    )


def _markdown_code(value: object) -> str:
    escaped = str(value).replace("\r", " ").replace("\n", " ")
    escaped = escaped.replace("|", "\\|").replace("`", "\\`")
    return f"`{escaped}`"


def _counts_summary(counts: tuple[TerminalSuiteReasonCount, ...]) -> str:
    if not counts:
        return "- none\n"
    return "".join(f"- {_markdown_code(item.reason)}: `{item.run_count}`\n" for item in counts)


def _summary(report: TerminalSuiteReport) -> str:
    rows = []
    for case in report.cases:
        budget_stop = case.budget_stop_reason.value if case.budget_stop_reason else "none"
        rows.append(
            "| "
            f"{case.sequence} | {_markdown_code(case.case_id)} | "
            f"{_markdown_code(case.run_id)} | `{case.status.value}` | "
            f"`{str(case.task_succeeded).lower()}` | "
            f"{_markdown_code(case.failure_reason or 'none')} | "
            f"{_markdown_code(budget_stop)} | "
            f"`{str(case.unknown_effects_present).lower()}` | "
            f"`{str(case.open_effects_present).lower()}` |\n"
        )
    boundaries = "\n".join(f"- `{item}`" for item in TERMINAL_SUITE_BOUNDARIES)
    return (
        "# Horizon Terminal Run Suite\n\n"
        f"- Suite: `{report.suite_id}`\n"
        f"- Manifest digest: `{report.manifest_digest}`\n"
        f"- Existing terminal Runs aggregated: `{report.case_count}`\n"
        f"- Succeeded / failed / cancelled: `{report.succeeded_run_count}` / "
        f"`{report.failed_run_count}` / `{report.cancelled_run_count}`\n"
        f"- Task success rate: `{report.task_success_rate:.6f}`\n"
        f"- Replay-verified terminal captures: `{report.terminal_capture_verified_count}`\n"
        f"- Runs with unknown effects: `{report.unknown_effect_run_count}`\n"
        f"- Runs with open effects: `{report.open_effect_run_count}`\n\n"
        "## Cases\n\n"
        "| # | Case | Run | Status | Task succeeded | Failure reason | Budget stop | "
        "Unknown | Open |\n"
        "|---:|---|---|---|---:|---|---|---:|---:|\n"
        f"{''.join(rows)}\n"
        "## Failure reasons\n\n"
        f"{_counts_summary(report.failure_reason_counts)}\n"
        "## Budget-stop reasons\n\n"
        f"{_counts_summary(report.budget_stop_reason_counts)}\n"
        "The suite verifier checks the top-level hashes, verifies every child EvidencePack, "
        "replays every child Trace, and recomputes this report and summary without a control "
        "database. Generation and verification do not execute the Runs.\n\n"
        "## Claim boundaries\n\n"
        f"{boundaries}\n"
    )


def _preflight(store: SQLiteEventStore, manifest: TerminalSuiteManifest) -> None:
    for case in manifest.cases:
        run = store.get(case.run_id)
        if not run.terminal:
            raise ValueError(f"Terminal-suite case {case.case_id} references a nonterminal Run")
        replayed = SQLiteEventStore.replay_jsonl(store.export_jsonl(case.run_id))
        if replayed.as_dict() != run.as_dict():
            raise ValueError(
                f"Terminal-suite case {case.case_id} live and replayed projections differ"
            )


def export_terminal_suite(
    store: SQLiteEventStore,
    manifest: TerminalSuiteManifest,
    output_dir: Path,
) -> TerminalSuiteResult:
    """Aggregate existing terminal Runs; never execute tasks, models, tools, or repository code."""

    destination = output_dir.resolve()
    if destination.exists():
        raise FileExistsError(f"Terminal-suite destination already exists: {destination}")
    _preflight(store, manifest)

    destination.mkdir(parents=True, exist_ok=False)
    manifest_path = destination / "suite-manifest.json"
    report_path = destination / "suite-report.json"
    summary_path = destination / "SUMMARY.md"
    suite_pack_path = destination / "suite-pack.json"
    _write_new(manifest_path, (canonical_json(manifest) + "\n").encode("utf-8"))

    cases = []
    for sequence, case in enumerate(manifest.cases, start=1):
        pack_path = destination / _case_pack_path(case, sequence)
        exported = export_terminal_evidence_pack(store, case.run_id, pack_path.parent)
        cases.append(
            _case_result(
                case,
                sequence,
                destination,
                exported.evidence_pack_path,
                exported.evidence_pack,
            )
        )

    report = _build_report(manifest, tuple(cases))
    _write_new(report_path, (canonical_json(report) + "\n").encode("utf-8"))
    _write_new(summary_path, _summary(report).encode("utf-8"))
    pack = TerminalSuitePack(
        report=report,
        files=(
            _file_record(destination, "manifest", manifest_path),
            _file_record(destination, "report", report_path),
            _file_record(destination, "summary", summary_path),
        ),
    )
    _write_new(suite_pack_path, (canonical_json(pack) + "\n").encode("utf-8"))
    verified = verify_terminal_suite_pack(suite_pack_path)
    return TerminalSuiteResult(
        output_dir=destination,
        suite_pack_path=suite_pack_path,
        manifest_path=manifest_path,
        report_path=report_path,
        summary_path=summary_path,
        suite_pack=verified,
    )


def verify_terminal_suite_pack(path: Path) -> TerminalSuitePack:
    """Verify the aggregate and every child terminal EvidencePack without a control database."""

    suite_pack_path = path.resolve(strict=True)
    root = suite_pack_path.parent
    pack = TerminalSuitePack.model_validate_json(suite_pack_path.read_text(encoding="utf-8"))
    contents: dict[str, bytes] = {}
    for item in pack.files:
        candidate = _resolve_member(root, item.path)
        content = candidate.read_bytes()
        if len(content) != item.bytes or _sha256(content) != item.sha256:
            raise ValueError(f"Terminal-suite file failed integrity verification: {item.path}")
        contents[item.role] = content

    manifest = TerminalSuiteManifest.model_validate_json(contents["manifest"])
    report = TerminalSuiteReport.model_validate_json(contents["report"])
    if report != pack.report:
        raise ValueError("Terminal-suite pack metadata does not match its report")
    if report.suite_id != manifest.suite_id or report.manifest_digest != manifest.sha256:
        raise ValueError("Terminal-suite report does not match its manifest")

    cases = []
    for sequence, case in enumerate(manifest.cases, start=1):
        relative_pack = _case_pack_path(case, sequence)
        child_path = _resolve_member(root, relative_pack)
        child_pack = verify_terminal_evidence_pack(child_path)
        cases.append(_case_result(case, sequence, root, child_path, child_pack))
    expected_report = _build_report(manifest, tuple(cases))
    if report != expected_report:
        raise ValueError("Terminal-suite report does not match its replayed child Runs")
    if contents["summary"] != _summary(expected_report).encode("utf-8"):
        raise ValueError("Terminal-suite summary does not match its replayed child Runs")
    return pack
