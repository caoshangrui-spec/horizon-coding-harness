import json
from pathlib import Path

from typer.testing import CliRunner

from horizon.interfaces.cli.app import app

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "examples" / "agent-task.yaml"


def test_intake_cli_routes_read_only_without_database_or_model(tmp_path):
    result = runner.invoke(
        app,
        [
            "intake",
            "route",
            "解释一下 parser 怎么处理空输入",
            "--repository",
            str(tmp_path),
            "--authority",
            "workspace_write",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["admission_ready"] is True
    assert payload["intent"]["task_kind"] == "explain"
    assert payload["intent"]["execution_mode"] == "read_only"
    assert payload["intent"]["admission_model_calls"] == 0
    assert "replace_text" not in payload["intent"]["effective_model_tools"]


def test_intake_cli_control_command_is_zero_model_and_resolves_current_run():
    result = runner.invoke(
        app,
        [
            "intake",
            "route",
            "继续",
            "--known-run-id",
            "run_one",
            "--known-run-id",
            "run_two",
            "--current-run-id",
            "run_two",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["admission_ready"] is True
    assert payload["intent"]["request_kind"] == "resume"
    assert payload["intent"]["target_run_id"] == "run_two"
    assert payload["intent"]["effective_model_tools"] == []
    assert payload["intent"]["admission_cost_cny"] == "0"


def test_intake_cli_explicit_control_uses_exact_target_without_parsing_task_words():
    result = runner.invoke(
        app,
        [
            "intake",
            "route",
            "修复 parser",
            "--explicit-command",
            "cancel",
            "--target-run-id",
            "run_two",
            "--known-run-id",
            "run_one",
            "--known-run-id",
            "run_two",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["admission_ready"] is True
    assert payload["intent"]["request_kind"] == "cancel"
    assert payload["intent"]["target_run_id"] == "run_two"
    assert payload["intent"]["route_source"] == "explicit_command"


def test_intake_cli_refuses_to_grant_structured_write_task_without_authority():
    result = runner.invoke(
        app,
        [
            "intake",
            "route",
            "execute the supplied task",
            "--task-spec",
            str(TASK),
            "--authority",
            "read_only",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["admission_ready"] is False
    assert payload["intent"]["route_source"] == "structured_task"
    assert payload["intent"]["requested_execution_mode"] == "workspace_write"
    assert payload["intent"]["execution_mode"] == "read_only"
    assert payload["intent"]["missing_fields"] == ["workspace_write_authority"]


def test_intake_cli_exposes_ambiguous_request_instead_of_guessing():
    result = runner.invoke(app, ["intake", "route", "帮我处理一下"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["admission_ready"] is False
    assert payload["intent"]["route_source"] == "unclassified"
    assert payload["intent"]["model_classification_required"] is True
    assert payload["intent"]["admission_model_calls"] == 0
    assert payload["intent"]["missing_fields"] == ["task_kind", "repository"]
