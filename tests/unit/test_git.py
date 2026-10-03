from pathlib import Path
from subprocess import CompletedProcess

import pytest

from horizon.adapters.vcs import git
from horizon.domain.errors import Conflict


def test_clean_checkout_verification_rejects_dirty_source(monkeypatch, tmp_path: Path):
    expected = "a" * 40
    monkeypatch.setattr(git, "read_git_head", lambda workspace: expected)
    statuses = iter(
        [
            CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            CompletedProcess(
                args=[],
                returncode=0,
                stdout=" M tqdm/contrib/__init__.py\n",
                stderr="",
            ),
        ]
    )
    monkeypatch.setattr(git.subprocess, "run", lambda *args, **kwargs: next(statuses))

    assert git.verify_clean_git_checkout(tmp_path, expected) == expected
    with pytest.raises(Conflict, match="must be clean"):
        git.verify_clean_git_checkout(tmp_path, expected)
