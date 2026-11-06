"""Frozen v5 requests: counterfactual input retention, never a model-success test."""

import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from horizon.application.context import ContextProjector, _read_preserving_unit, _units
from horizon.application.model_probe import conservative_input_estimate
from horizon.application.retrieval_control import read_evidence_missing
from horizon.application.retrieval_lineage import _deliveries_for_model_call, _RetrievalEvidence
from horizon.domain.common import digest
from horizon.domain.context import ContextProjection, MandatoryFactLedger
from horizon.domain.memory import RunMemorySnapshot
from horizon.domain.model import ModelMessage, ModelRequest
from horizon.domain.retrieval import EvidencePack

FIXTURE = Path(__file__).parents[1] / "fixtures/cookiecutter3/failure-inputs-v5.json"


@pytest.mark.parametrize("index", range(9), ids=lambda i: f"execution-{i + 1}")
def test_frozen_v5_full_read_survives_with_real_request_budget(index):
    frozen = json.loads(FIXTURE.read_text(encoding="utf-8"))
    turn = frozen["turns"][index]
    artifacts = frozen["artifacts"]
    for ref, payload in artifacts.items():
        assert digest(payload) == ref
    before = ContextProjection.model_validate(artifacts[turn["context_projection_ref"]])
    template = frozen["request_templates"][turn["request_template_ref"]]
    assert digest(template) == turn["request_template_ref"]
    old_request = ModelRequest.model_validate({**template, "messages": before.messages})
    assert old_request.sha256 == turn["request_hash"]
    assert not any(m.role == "tool" for m in before.messages)
    messages = tuple(
        ModelMessage.model_validate(m) for m in frozen["messages"][: turn["source_message_count"]]
    )
    assert digest([m.model_dump(mode="json") for m in messages]) == before.source_digest
    facts = MandatoryFactLedger.model_validate(artifacts[turn["mandatory_facts_ref"]])
    memory = RunMemorySnapshot.model_validate(artifacts[turn["run_memory_ref"]])

    def estimate(projected):
        return conservative_input_estimate(
            ModelRequest.model_validate({**template, "messages": projected})
        )

    projector = ContextProjector(
        max_chars=before.max_chars,
        preserve_recent_units=before.preserve_recent_units,
        max_input_tokens=before.input_token_budget.max_input_tokens,
        estimate_input_tokens=estimate,
    )
    kwargs = dict(
        mandatory_facts=facts,
        mandatory_facts_ref=facts.sha256,
        run_memory=memory,
        run_memory_ref=memory.sha256,
    )
    after = projector.project(messages, **kwargs)
    assert after == projector.project(messages, **kwargs)
    assert after.projected_chars <= 8000
    assert after.input_token_budget.estimate == estimate(after.messages)
    assert after.source_digest == before.source_digest
    assert after.mandatory_facts_ref == before.mandatory_facts_ref
    assert after.run_memory_ref == before.run_memory_ref
    assert after.messages[1] == messages[1]
    assert all(unit.complete for unit in _units(after.messages[2:]))
    if index < 2:
        return  # No successful read_file receipt exists yet.
    latest_unit = _units(messages[2:])[-1]
    read_call = next(
        call for call in latest_unit.messages[0].tool_calls if call.function.name == "read_file"
    )
    receipt = next(m for m in latest_unit.messages if m.tool_call_id == read_call.id)
    assert receipt in after.messages  # Exact full bytes and provider ID, not a code keyword.
    assert latest_unit.messages[0] in after.messages  # Original multi-call assistant unchanged.
    assert not read_evidence_missing(messages, after.messages, read_call.function.arguments)
    assert read_evidence_missing(messages, before.messages, read_call.function.arguments)


def _frozen_mixed_unit():
    frozen = json.loads(FIXTURE.read_text(encoding="utf-8"))
    turn = frozen["turns"][2]
    messages = tuple(
        ModelMessage.model_validate(m) for m in frozen["messages"][2 : turn["source_message_count"]]
    )
    return _units(messages)[-1]


def test_mixed_read_only_projection_binds_omission_and_keeps_original_protocol():
    original = _frozen_mixed_unit()
    original_hash = digest([m.model_dump(mode="json") for m in original.messages])
    projected = _read_preserving_unit(original)
    assert projected != original
    assert projected.messages[:2] == original.messages[:2]
    assert projected.messages[2].tool_call_id == original.messages[2].tool_call_id
    content = projected.messages[2].content
    assert "not source evidence" in content
    assert sha256(original.messages[2].content.encode()).hexdigest() in content
    assert _units(projected.messages) == (projected,)
    assert digest([m.model_dump(mode="json") for m in original.messages]) == original_hash
    # Even without older turns, partial compaction must be traceable and must not add
    # more messages than the source transcript.
    messages = (
        ModelMessage(role="system", content="Keep task constraints."),
        ModelMessage(role="user", content="Fix the task."),
        *original.messages,
    )
    p = ContextProjector(
        max_chars=4500,
        preserve_recent_units=2,
        max_input_tokens=120000,
        estimate_input_tokens=lambda ms: conservative_input_estimate(
            ModelRequest(model="offline", messages=ms, max_output_tokens=2048)
        ),
    ).project(messages)
    assert p.messages[2:] == projected.messages
    assert p.compacted and p.compacted_unit_count == 1
    assert p.source_message_count == p.projected_message_count
    retrieval = _RetrievalEvidence(
        record=SimpleNamespace(call_id="retrieval", artifact_ref="a" * 64),
        settled_seq=1,
        evidence=EvidencePack.model_validate_json(original.messages[2].content),
        payload=original.messages[2].content,
        provider_tool_call_id=original.messages[2].tool_call_id,
    )
    reservation = SimpleNamespace(
        context_projection_ref=digest(p), run_memory_ref=None, run_memory_hash=None
    )
    artifacts = SimpleNamespace(read=lambda ref: p.model_dump_json().encode())
    assert _deliveries_for_model_call(reservation, 2, {"retrieval": retrieval}, artifacts) == ()


@pytest.mark.parametrize(
    "variant", ["incomplete", "write", "unknown_tool", "policy", "error", "degraded", "no_read"]
)
def test_read_preserving_compaction_never_elides_unsafe_or_failed_turns(variant):
    original = _frozen_mixed_unit()
    messages = list(original.messages)
    if variant == "incomplete":
        messages.pop()
    elif variant in {"write", "unknown_tool"}:
        calls = list(messages[0].tool_calls)
        calls[1] = calls[1].model_copy(
            update={
                "function": calls[1].function.model_copy(
                    update={"name": "replace_text" if variant == "write" else "custom_tool"}
                )
            }
        )
        messages[0] = messages[0].model_copy(update={"tool_calls": tuple(calls)})
    elif variant == "no_read":
        messages[1] = messages[1].model_copy(update={"content": "Read failed: permission denied"})
    else:
        content = "RetrievalQueryPolicy: new evidence required" if variant == "policy" else "error"
        if variant == "degraded":
            evidence = json.loads(messages[2].content)
            evidence.update(status="degraded", degradation_reasons=["index unavailable"])
            content = json.dumps(evidence)
        messages[2] = messages[2].model_copy(update={"content": content})
    unit = _units(tuple(messages))[0]
    assert _read_preserving_unit(unit) == unit
