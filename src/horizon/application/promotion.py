from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from horizon.adapters.vcs.git import read_git_head
from horizon.adapters.workspace.promotion import WorkspacePromoter, workspace_path_hash
from horizon.application.services import HarnessService
from horizon.domain.errors import Conflict
from horizon.domain.promotion import PromotionIntent, PromotionPlan, PromotionReceipt
from horizon.domain.run import Run
from horizon.domain.states import RunStatus


class PromotionService:
    """Plan and apply an explicit staging-to-source promotion with durable intent."""

    def __init__(self, service: HarnessService, promoter: WorkspacePromoter):
        self.service = service
        self.promoter = promoter

    @staticmethod
    def _candidate(run: Run) -> Path:
        if run.task.repository.source != "local" or run.task.repository.path is None:
            raise Conflict("Promotion requires a local staging workspace")
        candidate = Path(run.task.repository.path).resolve(strict=True)
        if not candidate.is_dir():
            raise Conflict("Staging candidate is not a directory")
        return candidate

    def plan(self, run_id: str, source: Path) -> PromotionPlan:
        run = self.service.store.get(run_id)
        if run.status != RunStatus.SUCCEEDED or run.workspace_origin is None:
            raise Conflict("Promotion planning requires a successful Run with a bound origin")
        if run.promotion_receipt is not None:
            return run.promotion_intent.plan
        if run.promotion_intent is not None:
            plan = run.promotion_intent.plan
            if workspace_path_hash(source) != plan.source_path_hash:
                raise Conflict("Source path does not match the pending promotion")
            return plan
        plan = self.promoter.build_plan(
            source.resolve(strict=True),
            self._candidate(run),
            run.workspace_origin,
            run.task.constraints,
        )
        if (
            plan.candidate_revision != run.workspace_revision
            or run.last_checkpoint is None
            or plan.candidate_manifest_ref != run.last_checkpoint["manifest_sha256"]
        ):
            raise Conflict("Promotion candidate does not match successful validation evidence")
        return plan

    def promote(self, run_id: str, source: Path) -> Run:
        run = self.service.store.get(run_id)
        source = source.resolve(strict=True)
        if run.promotion_receipt is not None:
            plan = run.promotion_intent.plan
            if workspace_path_hash(source) != plan.source_path_hash:
                raise Conflict("Source path does not match the settled promotion")
            current, _ = self.promoter.snapshots.capture(
                source,
                denied_paths=run.task.constraints.denied_paths,
            )
            if (
                current.workspace_revision != plan.candidate_revision
                or read_git_head(source) != plan.git_head_before
            ):
                raise Conflict("Promoted source changed after the recorded receipt")
            return run

        if run.promotion_intent is None:
            plan = self.plan(run_id, source)
            intent = PromotionIntent(
                promotion_id=f"promotion_{uuid4().hex}",
                plan=plan,
            )
            run = self.service.reserve_promotion(
                run_id,
                intent,
                f"reserve_{intent.promotion_id}",
            )
        else:
            intent = run.promotion_intent
            plan = intent.plan
            if workspace_path_hash(source) != plan.source_path_hash:
                raise Conflict("Source path does not match the pending promotion")

        revision, manifest, recovered = self.promoter.apply_or_recover(
            plan,
            source,
            self._candidate(run),
            run.task.constraints,
        )
        receipt = PromotionReceipt(
            promotion_id=intent.promotion_id,
            plan_hash=plan.sha256,
            source_revision_after=revision,
            source_manifest_ref_after=manifest,
            git_head_after=read_git_head(source),
            recovered_after_crash=recovered,
        )
        return self.service.settle_promotion(
            run_id,
            receipt,
            f"settle_{intent.promotion_id}",
        )
