from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from horizon.adapters.sandbox.docker import DockerSandbox
from horizon.adapters.sandbox.validation import DockerAcceptanceExecutor
from horizon.domain.task import AcceptanceCheck

pytestmark = pytest.mark.docker


@pytest.fixture
def acceptance_executor(tmp_path: Path) -> tuple[DockerAcceptanceExecutor, Path]:
    image = os.environ.get("HORIZON_TEST_DOCKER_IMAGE")
    if not image:
        pytest.skip("Set HORIZON_TEST_DOCKER_IMAGE to an existing shell-capable Linux image")

    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    workspace = staging_root / "candidate"
    fixture = Path(__file__).parents[2] / "examples" / "agent-fixture"
    shutil.copytree(fixture, workspace)
    workspace.chmod(0o777)
    return DockerAcceptanceExecutor(DockerSandbox(staging_root, image)), workspace


def test_fixture_validation_fails_then_passes_after_bounded_fix(
    acceptance_executor: tuple[DockerAcceptanceExecutor, Path],
) -> None:
    executor, workspace = acceptance_executor
    check = AcceptanceCheck(
        id="greeting_regression",
        command="sh tests/test_greet.sh",
        timeout_seconds=30,
    )

    before = executor.execute(workspace, check)
    assert not before.passed
    assert before.exit_code == 1
    assert "actual:   Helo, Codex!" in before.output

    target = workspace / "src" / "greet.sh"
    source = target.read_bytes()
    target.write_bytes(source.replace(b"Helo, ${name}!", b"Hello, ${name}!"))

    after = executor.execute(workspace, check)
    assert after.passed
    assert after.exit_code == 0
    assert not after.timed_out
    assert "greeting regression passed" in after.output
    assert after.output_hash != before.output_hash
