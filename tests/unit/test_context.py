import pytest

from horizon.application.context import ContextProjector
from horizon.domain.common import canonical_json
from horizon.domain.context import MandatoryFactLedger
from horizon.domain.errors import Conflict
from horizon.domain.memory import RunMemoryEntry, RunMemorySnapshot
from horizon.domain.model import FunctionCall, ModelMessage, ToolCall


def _prefix() -> tuple[ModelMessage, ModelMessage]:
    return (
        ModelMessage(role="system", content="bounded coding agent"),
        ModelMessage(role="user", content="complete the immutable task"),
    )


def _tool_unit(index: int, content: str) -> tuple[ModelMessage, ModelMessage]:
    call_id = f"call-{index}"
    return (
        ModelMessage(
            role="assistant",
            tool_calls=(
                ToolCall(
                    id=call_id,
                    function=FunctionCall(
                        name="read_file",
                        arguments={"path": f"src/file_{index}.py"},
                    ),
                ),
            ),
        ),
        ModelMessage(role="tool", content=content, tool_call_id=call_id),
    )


def _mandatory_facts(workspace_revision: str = "e" * 64) -> MandatoryFactLedger:
    return MandatoryFactLedger(
        run_id="run-context",
        task_spec_hash="a" * 64,
        objective_hash="b" * 64,
        plan_hash="c" * 64,
        plan_version=1,
        work_item_id="work-item",
        work_item_hash="d" * 64,
        allowed_paths_hash="f" * 64,
        denied_paths_hash="1" * 64,
        required_acceptance_ids=("unit",),
        required_acceptance_hash="2" * 64,
        budget_hash="3" * 64,
        execution_mode="workspace_write",
        authority_scope="workspace_write",
        model_policy_hash="4" * 64,
        workspace_revision=workspace_revision,
        tool_schema_hash="5" * 64,
    )


def _run_memory(*, status: str = "active") -> RunMemorySnapshot:
    revision = "e" * 64
    entry = RunMemoryEntry.create(
        scope_id="run-context",
        source_run_id="run-context",
        task_spec_hash="a" * 64,
        work_item_id="work-item",
        source_event_id="event-memory-1",
        source_event_seq=4,
        source_event_hash="6" * 64,
        source_call_id="call-memory-1",
        kind="observation",
        tool_name="read_file",
        outcome="success",
        status=status,
        dependency_revision=revision,
        evidence_ref="7" * 64,
        evidence_hash="7" * 64,
        statement="Controller observed tool read_file succeeded.",
        excerpt="def parse(value): return value",
    )
    return RunMemorySnapshot(
        run_id="run-context",
        task_spec_hash="a" * 64,
        plan_version=1,
        work_item_id="work-item",
        covered_event_seq=4,
        workspace_revision=revision,
        relevant_event_digest="8" * 64,
        max_entries=12,
        excerpt_chars=240,
        total_entry_count=1,
        included_entry_count=1,
        omitted_entry_count=0,
        active_count=int(status == "active"),
        stale_count=int(status == "stale"),
        unresolved_count=0,
        entries=(entry,),
    )


def test_projection_is_identity_when_source_is_within_budget():
    messages = (*_prefix(), *_tool_unit(1, "small result"))

    projected = ContextProjector(max_chars=2_000, preserve_recent_units=1).project(messages)

    assert projected.messages == messages
    assert projected.compacted is False
    assert projected.compacted_unit_count == 0
    assert projected.source_message_count == projected.projected_message_count == 4
    assert projected.max_chars == 2_000
    assert projected.preserve_recent_units == 1


def test_projection_compacts_only_old_complete_units_deterministically():
    messages = (
        *_prefix(),
        *_tool_unit(1, "a" * 1_200),
        *_tool_unit(2, "b" * 1_200),
        *_tool_unit(3, "c" * 1_200),
    )
    projector = ContextProjector(max_chars=3_400, preserve_recent_units=1)

    first = projector.project(messages)
    second = projector.project(messages)

    assert first == second
    assert first.compacted is True
    assert first.compacted_unit_count == 2
    assert first.messages[:2] == messages[:2]
    assert first.messages[-2:] == messages[-2:]
    assert first.messages[2].role == "user"
    assert "Deterministic history projection" in (first.messages[2].content or "")
    assert "a" * 241 not in (first.messages[2].content or "")
    assert len(canonical_json([item.model_dump(mode="json") for item in first.messages])) <= 3_400


def test_projection_never_creates_an_orphan_tool_message():
    messages = (
        *_prefix(),
        *_tool_unit(1, "a" * 1_100),
        *_tool_unit(2, "b" * 1_100),
        *_tool_unit(3, "c" * 1_100),
    )

    projection = ContextProjector(max_chars=3_300, preserve_recent_units=1).project(messages)

    seen_calls: set[str] = set()
    for message in projection.messages:
        if message.role == "assistant":
            seen_calls.update(call.id for call in message.tool_calls)
        if message.role == "tool":
            assert message.tool_call_id in seen_calls


def test_projection_preserves_an_incomplete_tool_turn():
    incomplete = ModelMessage(
        role="assistant",
        tool_calls=(
            ToolCall(
                id="pending-call",
                function=FunctionCall(name="read_file", arguments={"path": "src/pending.py"}),
            ),
        ),
    )
    messages = (
        *_prefix(),
        *_tool_unit(1, "a" * 1_200),
        *_tool_unit(2, "b" * 1_200),
        incomplete,
    )

    projection = ContextProjector(max_chars=3_200, preserve_recent_units=1).project(messages)

    assert projection.compacted_unit_count == 2
    assert projection.messages[-1] == incomplete


def test_projection_compacts_recent_complete_unit_when_hard_budget_requires_it():
    messages = (*_prefix(), *_tool_unit(1, "x" * 4_000))

    projection = ContextProjector(max_chars=2_000, preserve_recent_units=1).project(messages)

    assert projection.compacted is True
    assert projection.compacted_unit_count == 1
    assert projection.messages[:2] == messages[:2]
    assert (
        len(canonical_json([item.model_dump(mode="json") for item in projection.messages])) <= 2_000
    )


def test_projection_compacts_a_pilot_sized_recent_tool_result():
    messages = (*_prefix(), *_tool_unit(1, "x" * 122_665))

    projection = ContextProjector(max_chars=60_000, preserve_recent_units=6).project(messages)

    assert projection.compacted_unit_count == 1
    assert projection.source_message_count == 4
    assert projection.projected_message_count == 3
    assert (
        len(canonical_json([item.model_dump(mode="json") for item in projection.messages]))
        <= 60_000
    )


def test_projection_rejects_when_incomplete_turn_alone_exceeds_budget():
    incomplete = ModelMessage(
        role="assistant",
        content="x" * 4_000,
        tool_calls=(
            ToolCall(
                id="pending-call",
                function=FunctionCall(name="read_file", arguments={"path": "src/pending.py"}),
            ),
        ),
    )
    messages = (*_prefix(), incomplete)

    with pytest.raises(Conflict, match="incomplete Agent turn"):
        ContextProjector(max_chars=2_000, preserve_recent_units=1).project(messages)


def test_projection_rejects_noncanonical_prefix():
    messages = (
        ModelMessage(role="user", content="missing system message"),
        ModelMessage(role="user", content="task"),
    )

    with pytest.raises(Conflict, match="start with system and user"):
        ContextProjector(max_chars=2_000, preserve_recent_units=1).project(messages)


def test_projection_rejects_an_orphan_tool_result():
    messages = (
        *_prefix(),
        ModelMessage(role="tool", content="orphan", tool_call_id="missing-call"),
    )

    with pytest.raises(Conflict, match="orphan tool result"):
        ContextProjector(max_chars=2_000, preserve_recent_units=1).project(messages)


def test_projection_injects_content_addressed_mandatory_facts_without_mutating_source():
    messages = (*_prefix(), *_tool_unit(1, "small result"))
    facts = _mandatory_facts()

    projection = ContextProjector(max_chars=4_000, preserve_recent_units=1).project(
        messages,
        mandatory_facts=facts,
        mandatory_facts_ref=facts.sha256,
    )

    assert messages[0].content == "bounded coding agent"
    assert projection.mandatory_facts_ref == facts.sha256
    assert projection.mandatory_facts_hash == facts.sha256
    assert facts.task_spec_hash in (projection.messages[0].content or "")
    assert facts.workspace_revision in (projection.messages[0].content or "")
    assert projection.source_message_count == projection.projected_message_count == len(messages)


def test_compaction_cannot_remove_or_replace_mandatory_facts():
    messages = (
        *_prefix(),
        *_tool_unit(1, "a" * 1_200),
        *_tool_unit(2, "b" * 1_200),
        *_tool_unit(3, "c" * 1_200),
    )
    facts = _mandatory_facts()

    projection = ContextProjector(max_chars=4_500, preserve_recent_units=1).project(
        messages,
        mandatory_facts=facts,
        mandatory_facts_ref=facts.sha256,
    )

    assert projection.compacted is True
    assert facts.sha256 == projection.mandatory_facts_hash
    assert facts.task_spec_hash in (projection.messages[0].content or "")
    assert facts.required_acceptance_ids[0] in (projection.messages[0].content or "")


def test_projection_rejects_mandatory_fact_artifact_mismatch():
    facts = _mandatory_facts()

    with pytest.raises(Conflict, match="content-addressed artifact"):
        ContextProjector(max_chars=4_000, preserve_recent_units=1).project(
            _prefix(),
            mandatory_facts=facts,
            mandatory_facts_ref="0" * 64,
        )


def test_projection_injects_only_active_run_memory_evidence():
    active = _run_memory(status="active")
    active_projection = ContextProjector(max_chars=4_000, preserve_recent_units=1).project(
        _prefix(),
        run_memory=active,
        run_memory_ref=active.sha256,
    )

    assert active_projection.run_memory_ref == active.sha256
    assert active_projection.run_memory_hash == active.sha256
    assert active_projection.run_memory_entry_count == 1
    assert "def parse(value)" in (active_projection.messages[0].content or "")

    stale = _run_memory(status="stale")
    stale_projection = ContextProjector(max_chars=4_000, preserve_recent_units=1).project(
        _prefix(),
        run_memory=stale,
        run_memory_ref=stale.sha256,
    )

    system = stale_projection.messages[0].content or ""
    assert '"stale_count":1' in system
    assert '"status":"stale"' in system
    assert "def parse(value)" not in system


def test_compaction_preserves_run_memory_binding():
    messages = (
        *_prefix(),
        *_tool_unit(1, "a" * 1_200),
        *_tool_unit(2, "b" * 1_200),
        *_tool_unit(3, "c" * 1_200),
    )
    memory = _run_memory()

    projection = ContextProjector(max_chars=4_500, preserve_recent_units=1).project(
        messages,
        run_memory=memory,
        run_memory_ref=memory.sha256,
    )

    assert projection.compacted is True
    assert projection.run_memory_ref == memory.sha256
    assert projection.run_memory_hash == memory.sha256
    assert projection.run_memory_entry_count == 1
    assert "def parse(value)" in (projection.messages[0].content or "")


def test_projection_rejects_run_memory_artifact_mismatch():
    memory = _run_memory()

    with pytest.raises(Conflict, match="Run memory does not match"):
        ContextProjector(max_chars=4_000, preserve_recent_units=1).project(
            _prefix(),
            run_memory=memory,
            run_memory_ref="0" * 64,
        )
