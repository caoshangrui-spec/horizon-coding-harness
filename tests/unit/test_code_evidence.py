import hashlib
import json
from pathlib import Path

from horizon.application.code_evidence import source_excerpt
from horizon.application.retrieval_control import (
    evidence_is_visible,
    find_evidence_reacquisition,
    read_evidence_missing,
)
from horizon.domain.model import ModelMessage
from horizon.domain.retrieval import EvidencePack

FIXTURES = Path(__file__).parents[1] / "fixtures" / "cookiecutter3"


def frozen_inputs():
    return json.loads((FIXTURES / "failure-inputs.json").read_text(encoding="utf-8"))


def test_real_receipt_matches_frozen_hash_and_source():
    frozen = frozen_inputs()
    assert (
        hashlib.sha256(frozen["read"].encode()).hexdigest() == frozen["provenance"]["read_sha256"]
    )
    source = (FIXTURES / "prompt.py.txt").read_text(encoding="utf-8")
    assert frozen["read"].splitlines()[1:] == source.splitlines()[:100]
    assert "def read_user_variable" in source
    assert "def read_user_yes_no" in source
    assert len(source.splitlines()) == 242


def test_verified_retrieval_coordinates_beat_other_functions_without_named_task_symbol():
    frozen = frozen_inputs()
    chunk = EvidencePack.model_validate(frozen["retrieval"]).chunks[0]
    excerpt = source_excerpt(
        frozen["read"],
        222,
        ("default", "prompt", "return", "click", "value", "choice"),
        (chunk,),
    )
    assert excerpt is not None
    assert "user_choice = click.prompt(" in excerpt[2]
    assert "prompt, type=click.Choice(choices), default=default" in excerpt[2]
    assert excerpt[2] == "\n".join(frozen["read"].splitlines()[excerpt[0] : excerpt[1] + 1])


def test_excerpt_does_not_cut_an_over_budget_source_line():
    assert source_excerpt("call(" + "argument" * 80 + ")", 40, ("argument",)) is None


def test_real_reacquisition_is_not_blocked_when_evidence_is_only_in_canonical_history():
    frozen = frozen_inputs()
    messages = tuple(ModelMessage.model_validate(m) for m in frozen["messages"])
    revision = frozen["retrieval"]["workspace_revision"]
    hidden = (ModelMessage(role="system", content="Only artifact references survived."),)
    assert (
        find_evidence_reacquisition(
            messages,
            "retrieve_code",
            {"query": "read_user_choice click prompt"},
            revision,
            visible_messages=hidden,
        )
        is None
    )
    hint = find_evidence_reacquisition(
        messages,
        "retrieve_code",
        {"query": "read_user_choice click prompt"},
        revision,
        visible_messages=messages,
    )
    assert hint is not None
    assert "user_choice = click.prompt(" in hint.snippet
    assert evidence_is_visible(hint.snippet, messages)
    assert not evidence_is_visible(hint.snippet, hidden)
    # A legitimately wider or different range is not the previously seen evidence.
    assert (
        find_evidence_reacquisition(
            messages,
            "read_file",
            {"path": "cookiecutter/prompt.py", "start_line": 101},
            revision,
            visible_messages=messages,
        )
        is None
    )
    assert (
        find_evidence_reacquisition(
            messages,
            "retrieve_code",
            {"query": "read_user_choice click prompt"},
            "b" * 64,
            visible_messages=messages,
        )
        is None
    )
    read_args = next(
        call.function.arguments
        for message in messages
        for call in message.tool_calls
        if call.function.name == "read_file"
    )
    assert read_evidence_missing(messages, hidden, read_args)
    assert not read_evidence_missing(messages, messages, read_args)
