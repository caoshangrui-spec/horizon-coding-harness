import hashlib

from horizon.domain.human import matches_no_progress_evidence
from horizon.domain.tools import ToolCallRecord


def _record(index: int, name: str, arguments_hash: str, status: str, detail_ref: str):
    revision = "a" * 64
    return ToolCallRecord(
        call_id=f"call-{index}",
        name=name,
        arguments_hash=arguments_hash,
        status=status,
        output_hash=detail_ref if status == "error" else "b" * 64,
        workspace_revision_before=revision,
        workspace_revision_after=revision,
        artifact_ref=detail_ref if status == "error" else "c" * 64,
        workspace_manifest_ref="d" * 64,
    )


def test_alternating_no_progress_evidence_is_exact_and_content_bound():
    detail = (
        "NoProgressPolicy: an exact alternating two-action cycle reached 6 consecutive "
        "unchanged-revision receipts."
    )
    detail_ref = hashlib.sha256(detail.encode("utf-8")).hexdigest()
    records = [
        _record(index, name, arguments_hash, "success" if index < 4 else "error", detail_ref)
        for index, (name, arguments_hash) in enumerate(
            [("read_file", "1" * 64), ("search_repo", "2" * 64)] * 3
        )
    ]

    assert matches_no_progress_evidence(
        records,
        0,
        "alternating_two_action_cycle",
        "call-5",
        detail_ref,
        "a" * 64,
        detail,
    )
    assert not matches_no_progress_evidence(
        records,
        0,
        "identical_action",
        "call-5",
        detail_ref,
        "a" * 64,
        detail,
    )
    assert not matches_no_progress_evidence(
        records,
        0,
        "alternating_two_action_cycle",
        "call-5",
        detail_ref,
        "a" * 64,
        detail + " tampered",
    )
    drifted = [*records[:3], records[3].model_copy(update={"workspace_revision_after": "e" * 64})]
    drifted.extend(records[4:])
    assert not matches_no_progress_evidence(
        drifted,
        0,
        "alternating_two_action_cycle",
        "call-5",
        detail_ref,
        "a" * 64,
        detail,
    )
