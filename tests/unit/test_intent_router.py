from decimal import Decimal

import pytest

from horizon.application.intake import DeterministicIntentRouter
from horizon.domain.intake import AdmissionContext, IntentEnvelope, UserRequest
from horizon.domain.task import (
    AcceptanceCheck,
    BudgetSpec,
    Constraints,
    Repository,
    TaskSpec,
)


def admission(**overrides) -> AdmissionContext:
    values = {
        "authority_scope": "workspace_write",
        "repository_ref": "D:/repo",
        "known_run_ids": (),
    }
    values.update(overrides)
    return AdmissionContext(**values)


def route(text: str, **context) -> IntentEnvelope:
    return DeterministicIntentRouter().route(UserRequest(text=text), admission(**context))


@pytest.mark.parametrize(
    ("text", "task_kind", "mode"),
    [
        ("解释一下 parser 怎么处理空输入", "explain", "read_only"),
        ("检查这个补丁的问题", "review", "read_only"),
        ("设计缓存接口，先给方案", "feature", "plan_only"),
        ("修复空输入崩溃，保持返回类型不变", "bugfix", "workspace_write"),
        ("重构缓存模块", "refactor", "workspace_write"),
        ("新增 parser 的边界测试", "tests", "workspace_write"),
    ],
)
def test_routes_common_task_intents_without_a_model(text, task_kind, mode):
    envelope = route(text)

    assert envelope.request_kind == "task"
    assert envelope.task_kind == task_kind
    assert envelope.execution_mode == mode
    assert envelope.route_source == "deterministic_rule"
    assert envelope.admission_model_calls == 0
    assert envelope.admission_cost_cny == Decimal("0")
    assert envelope.model_classification_required is False
    assert envelope.missing_fields == ()
    assert all(span.text == text[span.start : span.end] for span in envelope.evidence_spans)


def test_negative_instruction_and_authority_can_only_narrow_execution():
    plan = route("修复空输入崩溃，但不要改代码，先给方案")
    denied = route("修复空输入崩溃", authority_scope="read_only")

    assert plan.task_kind == "bugfix"
    assert plan.requested_execution_mode == "plan_only"
    assert plan.execution_mode == "plan_only"
    assert "replace_text" not in plan.effective_model_tools
    assert denied.requested_execution_mode == "workspace_write"
    assert denied.execution_mode == "read_only"
    assert denied.missing_fields == ("workspace_write_authority",)
    assert "replace_text" not in denied.effective_model_tools


def test_control_commands_are_anchored_and_never_invoke_a_model():
    current = admission(
        known_run_ids=("run_123", "run_456"),
        current_run_id="run_123",
    )
    router = DeterministicIntentRouter()

    for text, expected in (
        ("状态", "status"),
        ("继续", "resume"),
        ("取消 run_456", "cancel"),
    ):
        envelope = router.route(UserRequest(text=text), current)
        assert envelope.request_kind == expected
        assert envelope.effective_model_tools == ()
        assert envelope.admission_model_calls == 0
        assert envelope.model_classification_required is False

    assert router.route(UserRequest(text="修复取消按钮"), current).request_kind == "task"


def test_ambiguous_cancel_does_not_guess_between_multiple_runs():
    envelope = route(
        "取消",
        known_run_ids=("run_123", "run_456"),
        repository_ref=None,
    )

    assert envelope.request_kind == "cancel"
    assert envelope.target_run_id is None
    assert envelope.missing_fields == ("target_run_id",)
    assert envelope.effective_model_tools == ()


def test_unknown_task_is_safe_and_exposes_unperformed_classification():
    envelope = route("帮我处理一下", repository_ref=None)

    assert envelope.request_kind == "task"
    assert envelope.task_kind == "unknown"
    assert envelope.execution_mode == "read_only"
    assert envelope.route_source == "unclassified"
    assert envelope.model_classification_required is True
    assert envelope.admission_model_calls == 0
    assert envelope.admission_cost_cny == 0
    assert envelope.missing_fields == ("task_kind", "repository")


def test_long_unknown_request_keeps_a_bounded_exact_evidence_span():
    text = "请处理 " + "x" * 2_000
    envelope = route(text)

    assert envelope.model_classification_required is True
    assert envelope.evidence_spans[0].text == text[:500]
    assert envelope.evidence_spans[0].end == 500


def test_mixed_read_and_write_request_abstains_instead_of_granting_write():
    envelope = route("先 review 这个补丁，然后修复问题")

    assert envelope.task_kind == "unknown"
    assert envelope.execution_mode == "read_only"
    assert envelope.route_source == "unclassified"
    assert envelope.model_classification_required is True
    assert envelope.missing_fields == ("task_kind",)
    assert {span.rule_id for span in envelope.evidence_spans} == {
        "conflicting_read_intent",
        "conflicting_write_intent",
    }


def test_structured_task_has_priority_and_intersects_tools_with_admission():
    task = TaskSpec(
        task_id="intent-task",
        title="Fix parser",
        objective="Handle empty input",
        repository=Repository(
            source="local",
            path="D:/repo",
            base_commit="a" * 40,
        ),
        constraints=Constraints(
            allowed_tools=("read_file", "replace_text", "run_check"),
        ),
        acceptance=(AcceptanceCheck(id="parser-test", command="pytest -q"),),
        budgets=BudgetSpec(
            max_steps=10,
            max_model_calls=5,
            max_tool_calls=10,
            max_wall_time_seconds=600,
            max_cost_usd=Decimal("1"),
        ),
        task_kind="bugfix",
        execution_mode="workspace_write",
        authority_scope="workspace_write",
    )
    envelope = DeterministicIntentRouter().route(
        UserRequest(text="ambiguous prose", task_spec=task),
        admission(authority_scope="plan_only", repository_ref=None),
    )

    assert envelope.route_source == "structured_task"
    assert envelope.task_spec_hash == task.sha256
    assert envelope.task_kind == "bugfix"
    assert envelope.requested_execution_mode == "workspace_write"
    assert envelope.execution_mode == "plan_only"
    assert envelope.effective_model_tools == ("read_file",)
    assert envelope.missing_fields == ("workspace_write_authority",)


def test_explicit_control_command_precedes_task_words_and_checks_known_target():
    envelope = DeterministicIntentRouter().route(
        UserRequest(
            text="修复 parser",
            explicit_command="cancel",
            target_run_id="run_missing",
        ),
        admission(known_run_ids=("run_known",)),
    )

    assert envelope.request_kind == "cancel"
    assert envelope.task_kind == "unknown"
    assert envelope.target_run_id == "run_missing"
    assert envelope.missing_fields == ("known_target_run_id",)
    assert envelope.route_source == "explicit_command"


def test_approval_requires_exact_run_and_human_request_ids():
    missing = DeterministicIntentRouter().route(
        UserRequest(text="approve", explicit_command="approval"),
        admission(known_run_ids=("run_known",)),
    )
    accepted = DeterministicIntentRouter().route(
        UserRequest(
            text="approve request_1 for run_known",
            explicit_command="approval",
            target_run_id="run_known",
            target_request_id="request_1",
        ),
        admission(known_run_ids=("run_known",)),
    )

    assert missing.target_run_id == "run_known"
    assert missing.missing_fields == ("target_request_id",)
    assert accepted.target_run_id == "run_known"
    assert accepted.target_request_id == "request_1"
    assert accepted.missing_fields == ()


def test_domain_contract_rejects_tools_above_effective_mode():
    with pytest.raises(ValueError, match="cannot exceed the effective execution mode"):
        IntentEnvelope(
            request_ref="a" * 64,
            request_kind="task",
            task_kind="bugfix",
            requested_execution_mode="workspace_write",
            execution_mode="read_only",
            authority_scope="read_only",
            route_source="deterministic_rule",
            evidence_spans=(
                {
                    "field": "task_kind",
                    "rule_id": "task_bugfix",
                    "text": "fix",
                    "start": 0,
                    "end": 3,
                },
            ),
            effective_model_tools=("replace_text",),
        )


def test_blank_request_is_rejected_before_routing():
    with pytest.raises(ValueError, match="cannot be blank"):
        UserRequest(text="   ")
