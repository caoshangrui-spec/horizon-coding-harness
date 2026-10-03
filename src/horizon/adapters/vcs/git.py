from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from horizon.domain.errors import Conflict

COMMIT = re.compile(r"^[0-9a-f]{40}$")


def read_git_head(workspace: Path) -> str | None:
    """Return HEAD for a Git worktree, or None for a non-Git directory."""
    try:
        probe = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        if (workspace / ".git").exists():
            raise Conflict("Git is required to verify this source workspace") from None
        return None
    except subprocess.TimeoutExpired as exc:
        raise Conflict("Git worktree probe timed out") from exc
    if probe.returncode != 0:
        return None
    top_level = Path(probe.stdout.strip()).resolve(strict=True)
    if os.path.normcase(str(top_level)) != os.path.normcase(str(workspace.resolve(strict=True))):
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise Conflict("Git HEAD probe timed out") from exc
    head = result.stdout.strip().lower()
    if result.returncode != 0 or COMMIT.fullmatch(head) is None:
        raise Conflict("Git worktree has no verifiable HEAD commit")
    return head


def verify_clean_git_checkout(workspace: Path, expected_head: str) -> str:
    """Require an exact, clean checkout before claiming upstream-source execution."""
    head = read_git_head(workspace)
    if head != expected_head:
        raise Conflict("External fixture Git HEAD does not match its frozen buggy commit")
    try:
        status = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored=matching",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise Conflict("Git cleanliness probe timed out") from exc
    if status.returncode != 0:
        raise Conflict("External fixture Git cleanliness could not be verified")
    if status.stdout.strip():
        raise Conflict("External full-checkout fixture must be clean before evaluation")
    return head
