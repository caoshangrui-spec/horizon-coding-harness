import pytest

from horizon.domain.promotion import PromotionPlan, WorkspaceChange


def test_historical_modified_change_without_kind_remains_readable():
    change = WorkspaceChange.model_validate(
        {
            "path": "src/parser.py",
            "before_sha256": "a" * 64,
            "after_sha256": "b" * 64,
        }
    )

    assert change.kind == "modified"
    assert change.before_sha256 == "a" * 64


def test_created_change_requires_an_absent_before_hash():
    created = WorkspaceChange(
        path="src/generated.py",
        kind="created",
        before_sha256=None,
        after_sha256="b" * 64,
    )

    assert created.before_sha256 is None
    with pytest.raises(ValueError, match="cannot have a before hash"):
        WorkspaceChange(
            path="src/generated.py",
            kind="created",
            before_sha256="a" * 64,
            after_sha256="b" * 64,
        )


def test_promotion_plan_rejects_more_than_one_created_file():
    changes = tuple(
        WorkspaceChange(
            path=f"src/generated_{index}.py",
            kind="created",
            before_sha256=None,
            after_sha256=str(index + 1) * 64,
        )
        for index in range(2)
    )

    with pytest.raises(ValueError, match="at most one created file"):
        PromotionPlan(
            source_path_hash="a" * 64,
            source_revision_before="b" * 64,
            source_manifest_ref_before="c" * 64,
            candidate_revision="d" * 64,
            candidate_manifest_ref="e" * 64,
            diff_artifact_ref="f" * 64,
            changes=changes,
        )


def test_promotion_plan_rejects_case_colliding_change_paths():
    changes = (
        WorkspaceChange(
            path="src/parser.py",
            before_sha256="1" * 64,
            after_sha256="2" * 64,
        ),
        WorkspaceChange(
            path="SRC/PARSER.PY",
            before_sha256="3" * 64,
            after_sha256="4" * 64,
        ),
    )

    with pytest.raises(ValueError, match="distinct paths"):
        PromotionPlan(
            source_path_hash="a" * 64,
            source_revision_before="b" * 64,
            source_manifest_ref_before="c" * 64,
            candidate_revision="d" * 64,
            candidate_manifest_ref="e" * 64,
            diff_artifact_ref="f" * 64,
            changes=changes,
        )
