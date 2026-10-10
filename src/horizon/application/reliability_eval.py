from __future__ import annotations

from horizon.domain.common import digest
from horizon.domain.errors import PolicyDenied
from horizon.domain.human import classify_no_progress
from horizon.domain.plan import Plan, check_execution_replan
from horizon.domain.reliability import (
    ExecutionReplanEvalCase,
    ExecutionReplanEvalCaseResult,
    NoProgressEvalCase,
    NoProgressEvalCaseResult,
    NoProgressStepResult,
    ReliabilityEvalCaseResult,
    ReliabilityEvalManifest,
    ReliabilityEvalReport,
)
from horizon.domain.tools import ToolCallRecord


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 6)


class ReliabilityEvaluator:
    """Evaluate frozen controller-policy traces without a model, network, or repository code."""

    def evaluate(self, manifest: ReliabilityEvalManifest) -> ReliabilityEvalReport:
        results: list[ReliabilityEvalCaseResult] = []
        for case in manifest.cases:
            if isinstance(case, NoProgressEvalCase):
                results.append(self._evaluate_no_progress(manifest, case))
            else:
                results.append(self._evaluate_replan(manifest, case))

        no_progress = [case for case in results if isinstance(case, NoProgressEvalCaseResult)]
        replans = [case for case in results if isinstance(case, ExecutionReplanEvalCaseResult)]
        decision_count = sum(case.decision_count for case in no_progress) + len(replans)
        correct_count = sum(case.correct_decision_count for case in no_progress) + sum(
            case.passed for case in replans
        )
        passed_count = sum(case.passed for case in results)
        return ReliabilityEvalReport(
            benchmark_id=manifest.benchmark_id,
            manifest_digest=manifest.sha256,
            case_count=len(results),
            passed_case_count=passed_count,
            case_pass_rate=_ratio(passed_count, len(results)),
            no_progress_case_count=len(no_progress),
            execution_replan_case_count=len(replans),
            policy_decision_count=decision_count,
            correct_policy_decision_count=correct_count,
            policy_decision_accuracy=_ratio(correct_count, decision_count),
            false_positive_count=sum(case.false_positive_count for case in no_progress),
            false_negative_count=sum(case.false_negative_count for case in no_progress),
            cases=tuple(results),
        )

    @staticmethod
    def _evaluate_no_progress(
        manifest: ReliabilityEvalManifest,
        case: NoProgressEvalCase,
    ) -> NoProgressEvalCaseResult:
        records: list[ToolCallRecord] = []
        reset_tool_count = 0
        steps: list[NoProgressStepResult] = []

        for index, action in enumerate(case.actions, start=1):
            if action.reset_before:
                reset_tool_count = len(records)
            arguments_hash = digest(action.arguments)
            decision = classify_no_progress(
                records,
                reset_tool_count,
                name=action.tool,
                arguments_hash=arguments_hash,
                workspace_revision=action.workspace_revision_before,
                max_identical_actions=manifest.max_identical_actions,
            )
            observed = (
                "hard_stop"
                if decision.hard_stop
                else "soft_block"
                if decision.pattern is not None
                else "allow"
            )
            correct = (
                observed == action.expected_decision
                and decision.pattern == action.expected_pattern
                and decision.cycle_period == action.expected_cycle_period
            )
            steps.append(
                NoProgressStepResult(
                    index=index,
                    tool=action.tool,
                    expected_decision=action.expected_decision,
                    observed_decision=observed,
                    expected_pattern=action.expected_pattern,
                    observed_pattern=decision.pattern,
                    expected_cycle_period=action.expected_cycle_period,
                    observed_cycle_period=decision.cycle_period,
                    correct=correct,
                )
            )
            records.append(
                ToolCallRecord(
                    call_id=f"{case.case_id}-{index}",
                    name=action.tool,
                    arguments_hash=arguments_hash,
                    status="error" if observed != "allow" else "success",
                    output_hash=digest(
                        {
                            "case_id": case.case_id,
                            "index": index,
                            "observed": observed,
                        }
                    ),
                    workspace_revision_before=action.workspace_revision_before,
                    workspace_revision_after=action.settled_revision,
                )
            )

        correct_count = sum(step.correct for step in steps)
        return NoProgressEvalCaseResult(
            case_id=case.case_id,
            passed=correct_count == len(steps),
            decision_count=len(steps),
            correct_decision_count=correct_count,
            false_positive_count=sum(
                step.expected_decision == "allow" and step.observed_decision != "allow"
                for step in steps
            ),
            false_negative_count=sum(
                step.expected_decision != "allow" and step.observed_decision == "allow"
                for step in steps
            ),
            steps=tuple(steps),
        )

    @staticmethod
    def _evaluate_replan(
        manifest: ReliabilityEvalManifest,
        case: ExecutionReplanEvalCase,
    ) -> ExecutionReplanEvalCaseResult:
        candidate = Plan(
            version=manifest.base_plan.version + 1,
            items=case.candidate_items,
        )
        rejection_reason = None
        try:
            check_execution_replan(
                manifest.base_plan,
                candidate,
                set(case.passed_items),
                manifest.task,
            )
            observed = "accept"
        except PolicyDenied as exc:
            observed = "reject"
            rejection_reason = str(exc)
        return ExecutionReplanEvalCaseResult(
            case_id=case.case_id,
            passed=observed == case.expected_decision,
            expected_decision=case.expected_decision,
            observed_decision=observed,
            rejection_reason=rejection_reason,
        )
