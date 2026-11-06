import pytest

from horizon.application.agent_loop import AgentLoopConfig, execution_model_iteration_limit


def test_execution_iteration_limit_honors_remaining_task_model_calls():
    assert execution_model_iteration_limit(14, 1) == 13
    assert execution_model_iteration_limit(8, 1) == 7


def test_execution_iteration_limit_rejects_an_exhausted_task_budget():
    with pytest.raises(ValueError, match="no model call available"):
        execution_model_iteration_limit(1, 1)


@pytest.mark.parametrize("limit", [256, 2048, 16384])
def test_execution_output_limit_preserves_positive_provider_configuration(limit):
    assert AgentLoopConfig(max_output_tokens=limit).max_output_tokens == limit
