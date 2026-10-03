import pytest
from pydantic import ValidationError

from horizon.domain.errors import InvalidTransition, PolicyDenied
from horizon.domain.plan import Plan, WorkItem
from horizon.domain.states import TERMINAL, RunStatus, check_transition
from horizon.domain.task import TaskSpec, relative_pattern


def test_contract_roundtrip_and_immutable(task):
    restored = TaskSpec.model_validate_json(task.model_dump_json())
    assert restored.sha256 == task.sha256
    assert isinstance(restored.acceptance, tuple)
    with pytest.raises(ValidationError):
        task.objective = "changed"
    with pytest.raises(ValidationError):
        task.budgets.max_steps = 100


@pytest.mark.parametrize("field", ["repository", "objective", "acceptance", "budgets"])
def test_required_fields(task_dict, field):
    del task_dict[field]
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(task_dict)


@pytest.mark.parametrize("value", [0, -1, True, "10", 1.5])
def test_invalid_budgets(task_dict, value):
    task_dict["budgets"]["max_steps"] = value
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(task_dict)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", "-0.01"])
def test_invalid_costs(task_dict, value):
    task_dict["budgets"]["max_cost_usd"] = value
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(task_dict)


@pytest.mark.parametrize("path", ["../x", "/etc", "C:/x", "src\\a.py", "a/../x", "a//x"])
def test_relative_path_contract(path):
    with pytest.raises(ValueError):
        relative_pattern(path)


def test_duplicates_unknown_fields_and_authority(task_dict):
    task_dict["acceptance"] *= 2
    with pytest.raises(ValidationError, match="unique"):
        TaskSpec.model_validate(task_dict)
    task_dict["acceptance"] = task_dict["acceptance"][:1]
    task_dict["authority_scope"] = "read_only"
    with pytest.raises(ValidationError, match="authority"):
        TaskSpec.model_validate(task_dict)
    task_dict["execution_mode"] = "read_only"
    task_dict["mystery"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        TaskSpec.model_validate(task_dict)


def item(id_, deps=(), checks=("unit",), tools=("read_file",)):
    return WorkItem(
        work_item_id=id_,
        title=id_,
        objective=id_,
        dependencies=deps,
        expected_artifacts=("patch",),
        acceptance_ids=checks,
        allowed_tools=tools,
    )


@pytest.mark.parametrize(
    "items",
    [
        [item("a"), item("a")],
        [item("a", ("a",))],
        [item("a", ("missing",))],
        [item("a", ("b",)), item("b", ("a",))],
    ],
)
def test_illegal_dags(items):
    with pytest.raises(ValidationError):
        Plan(items=items)


def test_schedule_and_coverage(task):
    plan = Plan(items=(item("a"), item("b", ("a",))))
    assert [item.work_item_id for item in plan.ready_items(set())] == ["a"]
    assert [item.work_item_id for item in plan.ready_items({"a"})] == ["b"]
    Plan(items=(item("a"),)).check_task(task)
    with pytest.raises(PolicyDenied, match="exactly one WorkItem"):
        plan.check_task(task)
    # Historical event projection may replay plans admitted before this rule existed.
    plan.check_task(task, enforce_unique_acceptance_ownership=False)
    with pytest.raises(PolicyDenied):
        Plan(items=(item("a", checks=("invented",)),)).check_task(task)
    readonly = task.model_copy(update={"execution_mode": "read_only"})
    with pytest.raises(PolicyDenied):
        Plan(items=(item("a", tools=("run_command",)),)).check_task(readonly)
    with pytest.raises(PolicyDenied, match="execution authority"):
        Plan(items=(item("a", tools=("shell_exec",)),)).check_task(task)


@pytest.mark.parametrize("state", [state for state in RunStatus if state not in TERMINAL])
def test_cancel_and_failure_allowed_from_all_nonterminal_states(state):
    check_transition(state, RunStatus.CANCELLED)
    check_transition(state, RunStatus.FAILED)


@pytest.mark.parametrize("state", list(TERMINAL))
def test_terminals_are_final(state):
    for target in RunStatus:
        with pytest.raises(InvalidTransition):
            check_transition(state, target)


def test_waiting_restores_original_phase():
    check_transition(RunStatus.WAITING_FOR_USER, RunStatus.VALIDATING, RunStatus.VALIDATING)
    with pytest.raises(InvalidTransition):
        check_transition(RunStatus.WAITING_FOR_USER, RunStatus.RUNNING, RunStatus.VALIDATING)
