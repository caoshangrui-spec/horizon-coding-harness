from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.domain.common import canonical_json, digest
from horizon.domain.run import Run, projection_hash
from horizon.domain.run_reliability import RunReliabilitySummary
from horizon.domain.states import RunStatus
from horizon.domain.terminal_evidence import (
    TERMINAL_EVIDENCE_BOUNDARIES,
    TERMINAL_EVIDENCE_BOUNDARIES_V1,
    TerminalEvidenceFile,
    TerminalEvidencePack,
    TerminalRunEvidence,
)


@dataclass(frozen=True)
class TerminalEvidenceResult:
    output_dir: Path
    evidence_pack_path: Path
    trace_path: Path
    final_state_path: Path
    summary_path: Path
    evidence_pack: TerminalEvidencePack


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _reliability_summary(run: Run) -> RunReliabilitySummary:
    required_ids = tuple(check.id for check in run.task.acceptance if check.required)
    required_acceptance_hash = digest(required_ids)
    report = run.validation
    current_validation = bool(
        report is not None
        and report["task_spec_hash"] == run.task.sha256
        and report["workspace_revision"] == run.workspace_revision
        and report["evidence_ref"]
    )
    required_checks_passed = bool(
        current_validation and set(required_ids) <= set(report["passed_check_ids"])
    )
    unknown_acceptance_evidence = not current_validation
    unknown_effects_present = bool(
        run.unknown_reservations or run.unknown_model_calls or run.unknown_tool_calls
    )
    open_effects_present = bool(
        run.reservations
        or run.model_reservations
        or run.tool_reservations
        or (run.promotion_intent is not None and run.promotion_receipt is None)
        or run.process_worker_launch is not None
    )
    quality_flags: list[str] = []
    limitations = [
        "Fallback and degradation history includes only records bound into the replayed Trace."
    ]
    if unknown_acceptance_evidence:
        quality_flags.append("acceptance_evidence_unknown")
        limitations.append(
            "Current-task acceptance evidence is unavailable; required-check outcome is unknown."
        )
    elif not required_checks_passed:
        quality_flags.append("required_checks_failed")
        limitations.append("Current validation did not pass every required acceptance check.")
    if unknown_effects_present:
        quality_flags.append("unknown_effects_present")
        limitations.append("One or more external effects have unknown outcomes.")
    if open_effects_present:
        quality_flags.append("open_effects_present")
        limitations.append("One or more reservations, promotions, or process effects remain open.")
    for degradation in run.reliability_degradations:
        if degradation.quality_flag not in quality_flags:
            quality_flags.append(degradation.quality_flag)
        limitations.append(
            f"{degradation.operation} used {degradation.strategy}; original reason(s): "
            f"{degradation.original_error_type}."
        )
    status = {
        RunStatus.SUCCEEDED: "passed",
        RunStatus.FAILED: "failed",
        RunStatus.CANCELLED: "blocked",
    }[run.status]
    return RunReliabilitySummary(
        status=status,
        task_spec_hash=run.task.sha256,
        effective_task_spec_hash=run.task.sha256,
        required_acceptance_hash=required_acceptance_hash,
        effective_required_acceptance_hash=required_acceptance_hash,
        required_checks_passed=required_checks_passed,
        unknown_acceptance_evidence=unknown_acceptance_evidence,
        quality_flags=tuple(quality_flags),
        fallback_history=(),
        degradations=tuple(run.reliability_degradations),
        limitations=tuple(limitations),
    )


def _evidence(run: Run, *, schema_version: Literal[1, 2] = 2) -> TerminalRunEvidence:
    unknown_reservations = tuple(sorted(run.unknown_reservations))
    unknown_models = tuple(sorted(run.unknown_model_calls))
    unknown_tools = tuple(sorted(run.unknown_tool_calls))
    open_reservations = tuple(sorted(run.reservations))
    open_models = tuple(sorted(run.model_reservations))
    open_tools = tuple(sorted(run.tool_reservations))
    promotion_pending = run.promotion_intent is not None and run.promotion_receipt is None
    process_launch = run.process_worker_launch
    reliability_summary = _reliability_summary(run) if schema_version == 2 else None
    boundaries = (
        TERMINAL_EVIDENCE_BOUNDARIES if schema_version == 2 else TERMINAL_EVIDENCE_BOUNDARIES_V1
    )
    return TerminalRunEvidence(
        schema_version=schema_version,
        run_id=run.run_id,
        task_id=run.task.task_id,
        task_spec_hash=run.task.sha256,
        status=run.status,
        task_succeeded=run.status == RunStatus.SUCCEEDED,
        failure_reason=run.failure_reason,
        event_count=run.seq,
        event_hash=run.event_hash,
        projection_hash=projection_hash(run),
        unknown_reservation_ids=unknown_reservations,
        unknown_model_call_ids=unknown_models,
        unknown_tool_call_ids=unknown_tools,
        open_reservation_ids=open_reservations,
        open_model_reservation_ids=open_models,
        open_tool_reservation_ids=open_tools,
        promotion_pending=promotion_pending,
        active_process_launch_id=(
            process_launch["launch_id"] if process_launch is not None else None
        ),
        active_process_id=(process_launch["process_id"] if process_launch is not None else None),
        unknown_effects_present=bool(unknown_reservations or unknown_models or unknown_tools),
        open_effects_present=bool(
            open_reservations
            or open_models
            or open_tools
            or promotion_pending
            or process_launch is not None
        ),
        reliability_summary=reliability_summary,
        boundaries=boundaries,
    )


def _id_list(values: tuple[str, ...]) -> str:
    return canonical_json(values)


def _summary(evidence: TerminalRunEvidence) -> str:
    boundaries = "\n".join(f"- `{item}`" for item in evidence.boundaries)
    reliability = evidence.reliability_summary
    reliability_section = ""
    if reliability is not None:
        fallback_history = [item.model_dump(mode="json") for item in reliability.fallback_history]
        degradations = [item.model_dump(mode="json") for item in reliability.degradations]
        reliability_section = (
            "\n## Reliability\n\n"
            f"- Outcome: `{reliability.status}`\n"
            f"- Required checks passed: "
            f"`{str(reliability.required_checks_passed).lower()}`\n"
            f"- Acceptance evidence unknown: "
            f"`{str(reliability.unknown_acceptance_evidence).lower()}`\n"
            f"- Required acceptance hash: `{reliability.required_acceptance_hash}`\n"
            f"- Quality flags: {canonical_json(reliability.quality_flags)}\n"
            f"- Fallback history: {canonical_json(fallback_history)}\n"
            f"- Degradations: {canonical_json(degradations)}\n"
            f"- Limitations: {canonical_json(reliability.limitations)}\n"
        )
    return (
        "# Horizon Terminal Run Evidence\n\n"
        f"- Run: `{evidence.run_id}`\n"
        f"- Task: `{evidence.task_id}`\n"
        f"- Terminal status: `{evidence.status.value}`\n"
        f"- Task succeeded: `{str(evidence.task_succeeded).lower()}`\n"
        f"- Failure reason: `{evidence.failure_reason or 'none'}`\n"
        f"- Event count: `{evidence.event_count}`\n"
        f"- Event hash: `{evidence.event_hash}`\n"
        f"- Projection hash: `{evidence.projection_hash}`\n"
        f"- Unknown reservations: {_id_list(evidence.unknown_reservation_ids)}\n"
        f"- Unknown model calls: {_id_list(evidence.unknown_model_call_ids)}\n"
        f"- Unknown tool calls: {_id_list(evidence.unknown_tool_call_ids)}\n"
        f"- Open reservations: {_id_list(evidence.open_reservation_ids)}\n"
        f"- Open model reservations: {_id_list(evidence.open_model_reservation_ids)}\n"
        f"- Open tool reservations: {_id_list(evidence.open_tool_reservation_ids)}\n"
        f"- Promotion pending: `{str(evidence.promotion_pending).lower()}`\n\n"
        f"- Active Process Worker: `{evidence.active_process_launch_id or 'none'}` / PID "
        f"`{evidence.active_process_id or 'none'}`\n\n"
        "The verifier checks every listed file hash, replays the append-only Trace offline, and "
        "requires the replayed projection to equal the exported final state.\n\n"
        f"{reliability_section}"
        "## Claim boundaries\n\n"
        f"{boundaries}\n"
    )


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)


def _file_record(root: Path, role: str, path: Path) -> TerminalEvidenceFile:
    content = path.read_bytes()
    return TerminalEvidenceFile(
        role=role,
        path=path.relative_to(root).as_posix(),
        sha256=_sha256(content),
        bytes=len(content),
    )


def export_terminal_evidence_pack(
    store: SQLiteEventStore,
    run_id: str,
    output_dir: Path,
) -> TerminalEvidenceResult:
    """Export one terminal Run without re-executing any model, tool, or external effect."""

    run = store.get(run_id)
    if not run.terminal:
        raise ValueError(
            "Terminal evidence can be exported only after the Run reaches a terminal state"
        )
    trace_content = store.export_jsonl(run_id).encode("utf-8")
    replayed = SQLiteEventStore.replay_jsonl(trace_content.decode("utf-8"))
    if replayed.as_dict() != run.as_dict():
        raise ValueError("Live and replayed Run projections do not match")
    evidence = _evidence(replayed)
    final_state_content = (canonical_json(replayed.as_dict()) + "\n").encode("utf-8")
    summary_content = _summary(evidence).encode("utf-8")

    destination = output_dir.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    trace_path = destination / "trace.jsonl"
    final_state_path = destination / "final-run.json"
    summary_path = destination / "SUMMARY.md"
    evidence_pack_path = destination / "evidence-pack.json"
    _write_new(trace_path, trace_content)
    _write_new(final_state_path, final_state_content)
    _write_new(summary_path, summary_content)
    pack = TerminalEvidencePack(
        schema_version=evidence.schema_version,
        evidence=evidence,
        files=(
            _file_record(destination, "trace", trace_path),
            _file_record(destination, "final_state", final_state_path),
            _file_record(destination, "summary", summary_path),
        ),
    )
    _write_new(evidence_pack_path, (canonical_json(pack) + "\n").encode("utf-8"))
    verified = verify_terminal_evidence_pack(evidence_pack_path)
    return TerminalEvidenceResult(
        output_dir=destination,
        evidence_pack_path=evidence_pack_path,
        trace_path=trace_path,
        final_state_path=final_state_path,
        summary_path=summary_path,
        evidence_pack=verified,
    )


def verify_terminal_evidence_pack(path: Path) -> TerminalEvidencePack:
    """Verify file integrity, replay the Trace, and compare the exact terminal projection."""

    pack_path = path.resolve(strict=True)
    root = pack_path.parent
    pack = TerminalEvidencePack.model_validate_json(pack_path.read_text(encoding="utf-8"))
    contents: dict[str, bytes] = {}
    for item in pack.files:
        candidate = (root / item.path).resolve(strict=True)
        if not candidate.is_relative_to(root):
            raise ValueError("Terminal evidence file escapes its pack directory")
        content = candidate.read_bytes()
        if len(content) != item.bytes or _sha256(content) != item.sha256:
            raise ValueError(f"Terminal evidence file failed integrity verification: {item.path}")
        contents[item.role] = content

    trace_text = contents["trace"].decode("utf-8")
    replayed = SQLiteEventStore.replay_jsonl(trace_text)
    if not replayed.terminal:
        raise ValueError("Terminal EvidencePack Trace does not end in a terminal state")
    expected_evidence = _evidence(replayed, schema_version=pack.schema_version)
    if pack.evidence != expected_evidence:
        raise ValueError("Terminal EvidencePack metadata does not match its Trace")
    expected_state = (canonical_json(replayed.as_dict()) + "\n").encode("utf-8")
    if contents["final_state"] != expected_state:
        raise ValueError("Terminal final state does not match its replayed Trace")
    if contents["summary"] != _summary(expected_evidence).encode("utf-8"):
        raise ValueError("Terminal summary does not match its replayed Trace")
    return pack
