from __future__ import annotations

import re
from collections.abc import Iterable

from horizon.domain.intake import (
    AdmissionContext,
    IntentEnvelope,
    IntentEvidenceSpan,
    RequestKind,
    TaskKind,
    UserRequest,
)
from horizon.domain.task import Mode, model_tools_for_mode

_MODE_RANK: dict[Mode, int] = {"read_only": 0, "plan_only": 1, "workspace_write": 2}
_RUN_ID = r"run_[A-Za-z0-9_.-]+"

_CONTROL_RULES: tuple[tuple[RequestKind, str, re.Pattern[str]], ...] = (
    (
        "status",
        "control_status",
        re.compile(
            rf"^\s*(?:status|状态|查看状态|查看进度|进度)(?:\s+(?P<run>{_RUN_ID}))?\s*[?？.!。]?\s*$",
            re.I,
        ),
    ),
    (
        "resume",
        "control_resume",
        re.compile(
            rf"^\s*(?:resume|continue|继续|恢复)(?:\s+(?P<run>{_RUN_ID}))?\s*[.!。]?\s*$", re.I
        ),
    ),
    (
        "cancel",
        "control_cancel",
        re.compile(rf"^\s*(?:cancel|stop|取消|停止)(?:\s+(?P<run>{_RUN_ID}))?\s*[.!。]?\s*$", re.I),
    ),
)

_TASK_RULES: tuple[tuple[TaskKind, str, re.Pattern[str]], ...] = (
    (
        "explain",
        "task_explain",
        re.compile(r"解释|讲解|说明.+(?:原理|代码|实现)|\bexplain\b|\bhow\s+does\b", re.I),
    ),
    (
        "review",
        "task_review",
        re.compile(
            r"代码审查|审查.+(?:补丁|代码)|评审.+(?:补丁|代码)|检查.+补丁|\bcode\s+review\b|\breview\b",
            re.I,
        ),
    ),
    ("refactor", "task_refactor", re.compile(r"重构|\brefactor(?:ing)?\b", re.I)),
    (
        "bugfix",
        "task_bugfix",
        re.compile(
            r"修复|解决.+(?:崩溃|报错|错误|bug)|\bfix(?:e[ds])?\b|\bbug(?:fix)?\b|\bcrash(?:es|ed)?\b",
            re.I,
        ),
    ),
    (
        "tests",
        "task_tests",
        re.compile(
            r"(?:新增|增加|补充|完善|丰富).{0,24}测试|测试覆盖|\badd\s+tests?\b|\btest\s+coverage\b",
            re.I,
        ),
    ),
    (
        "feature",
        "task_feature",
        re.compile(
            r"实现|新增|添加|开发|构建|设计|\bimplement\b|\badd\b|\bbuild\b|\bfeature\b",
            re.I,
        ),
    ),
)

_ANALYZE_ONLY = (
    "mode_read_only",
    re.compile(
        r"只读|只分析|仅分析|不要运行|不运行代码|\bread[ -]?only\b|\banaly[sz]e\s+only\b", re.I
    ),
)
_PLAN_ONLY = (
    "mode_plan_only",
    re.compile(
        r"先给方案|只给方案|仅给方案|先设计|不要改(?:代码)?|不修改(?:代码)?|\bplan\s+only\b|\bproposal\s+only\b|\bdesign\s+first\b|\bdo\s+not\s+(?:edit|modify|change)\b",
        re.I,
    ),
)


def _span(field: str, rule_id: str, match: re.Match[str]) -> IntentEvidenceSpan:
    text = match.group(0)[:500]
    return IntentEvidenceSpan(
        field=field,
        rule_id=rule_id,
        text=text,
        start=match.start(),
        end=match.start() + len(text),
    )


def _whole_request_span(field: str, rule_id: str, text: str) -> IntentEvidenceSpan:
    evidence = text[:500]
    return IntentEvidenceSpan(
        field=field,
        rule_id=rule_id,
        text=evidence,
        start=0,
        end=len(evidence),
    )


def _first_match(
    text: str,
    rules: Iterable[tuple[TaskKind, str, re.Pattern[str]]],
) -> tuple[TaskKind, str, re.Match[str]] | None:
    matches = [
        (kind, rule_id, match)
        for kind, rule_id, pattern in rules
        if (match := pattern.search(text)) is not None
    ]
    if not matches:
        return None
    return min(matches, key=lambda item: item[2].start())


def _read_write_conflict(text: str) -> tuple[re.Match[str], re.Match[str]] | None:
    matches = [
        (kind, match)
        for kind, _, pattern in _TASK_RULES
        if (match := pattern.search(text)) is not None
    ]
    read_matches = [match for kind, match in matches if kind in {"explain", "review"}]
    write_matches = [
        match for kind, match in matches if kind in {"bugfix", "feature", "refactor", "tests"}
    ]
    if not read_matches or not write_matches:
        return None
    return (
        min(read_matches, key=lambda match: match.start()),
        min(write_matches, key=lambda match: match.start()),
    )


def _cap_mode(requested: Mode, authority: Mode) -> Mode:
    return min((requested, authority), key=_MODE_RANK.__getitem__)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


class DeterministicIntentRouter:
    """Conservative zero-model router; ambiguous work remains unclassified."""

    def route(self, request: UserRequest, admission: AdmissionContext) -> IntentEnvelope:
        if request.explicit_command is not None and request.explicit_command != "task":
            return self._route_control(
                request,
                admission,
                request.explicit_command,
                None,
                "explicit_command",
            )

        for request_kind, rule_id, pattern in _CONTROL_RULES:
            if match := pattern.fullmatch(request.text):
                return self._route_control(
                    request,
                    admission,
                    request_kind,
                    match,
                    rule_id,
                )

        if request.task_spec is not None:
            return self._route_structured_task(request, admission)
        return self._route_rule_task(request, admission)

    def _route_control(
        self,
        request: UserRequest,
        admission: AdmissionContext,
        request_kind: RequestKind,
        match: re.Match[str] | None,
        rule_id: str,
    ) -> IntentEnvelope:
        target_run_id = request.target_run_id
        if target_run_id is None and match is not None and "run" in match.groupdict():
            target_run_id = match.groupdict().get("run")
        if target_run_id is None and match is None:
            if run_match := re.search(_RUN_ID, request.text):
                target_run_id = run_match.group(0)
        if target_run_id is None:
            target_run_id = admission.current_run_id
        if target_run_id is None and len(admission.known_run_ids) == 1:
            target_run_id = admission.known_run_ids[0]

        missing: list[str] = []
        if request_kind in {"resume", "cancel", "approval", "clarification"}:
            if target_run_id is None:
                missing.append("target_run_id")
        if request_kind in {"approval", "clarification"} and request.target_request_id is None:
            missing.append("target_request_id")
        if (
            target_run_id is not None
            and admission.known_run_ids
            and target_run_id not in admission.known_run_ids
        ):
            missing.append("known_target_run_id")

        evidence = (
            _span("request_kind", rule_id, match)
            if match is not None
            else _whole_request_span("request_kind", rule_id, request.text)
        )
        return IntentEnvelope(
            request_ref=request.sha256,
            request_kind=request_kind,
            requested_execution_mode="read_only",
            execution_mode="read_only",
            authority_scope=admission.authority_scope,
            target_run_id=target_run_id,
            target_request_id=request.target_request_id,
            route_source=("explicit_command" if match is None else "deterministic_rule"),
            missing_fields=_unique(missing),
            evidence_spans=(evidence,),
        )

    def _route_structured_task(
        self,
        request: UserRequest,
        admission: AdmissionContext,
    ) -> IntentEnvelope:
        task = request.task_spec
        assert task is not None
        requested_mode = task.execution_mode
        effective_mode = _cap_mode(requested_mode, admission.authority_scope)
        missing: list[str] = []
        if requested_mode == "workspace_write" and effective_mode != "workspace_write":
            missing.append("workspace_write_authority")
        tools = model_tools_for_mode(effective_mode)
        if task.constraints.allowed_tools is not None:
            tools = tuple(tool for tool in tools if tool in task.constraints.allowed_tools)
        return IntentEnvelope(
            request_ref=request.sha256,
            request_kind="task",
            task_kind=task.task_kind,
            requested_execution_mode=requested_mode,
            execution_mode=effective_mode,
            authority_scope=admission.authority_scope,
            task_spec_hash=task.sha256,
            route_source="structured_task",
            missing_fields=_unique(missing),
            evidence_spans=(_whole_request_span("request_kind", "structured_task", request.text),),
            effective_model_tools=tools,
        )

    def _route_rule_task(
        self,
        request: UserRequest,
        admission: AdmissionContext,
    ) -> IntentEnvelope:
        evidence: list[IntentEvidenceSpan] = []
        conflict = _read_write_conflict(request.text)
        classified = _first_match(request.text, _TASK_RULES)
        if conflict is not None:
            task_kind = "unknown"
            evidence.extend(
                (
                    _span("task_kind", "conflicting_read_intent", conflict[0]),
                    _span("task_kind", "conflicting_write_intent", conflict[1]),
                )
            )
        elif classified is None:
            task_kind: TaskKind = "unknown"
        else:
            task_kind, rule_id, match = classified
            evidence.append(_span("task_kind", rule_id, match))

        analyze_match = _ANALYZE_ONLY[1].search(request.text)
        plan_match = _PLAN_ONLY[1].search(request.text)
        if task_kind in {"explain", "review"} or analyze_match is not None:
            requested_mode: Mode = "read_only"
            mode_match = analyze_match
            mode_rule = _ANALYZE_ONLY[0]
        elif plan_match is not None:
            requested_mode = "plan_only"
            mode_match = plan_match
            mode_rule = _PLAN_ONLY[0]
        elif task_kind in {"bugfix", "feature", "refactor", "tests"}:
            requested_mode = "workspace_write"
            mode_match = classified[2] if classified is not None else None
            mode_rule = "mode_task_write"
        else:
            requested_mode = "read_only"
            mode_match = None
            mode_rule = "mode_unknown_safe_default"

        if mode_match is not None:
            evidence.append(_span("execution_mode", mode_rule, mode_match))
        if not evidence:
            evidence.append(
                _whole_request_span("request_kind", "unclassified_request", request.text)
            )

        effective_mode = _cap_mode(requested_mode, admission.authority_scope)
        missing: list[str] = []
        if task_kind == "unknown":
            missing.append("task_kind")
        if admission.repository_ref is None:
            missing.append("repository")
        if requested_mode == "workspace_write" and effective_mode != "workspace_write":
            missing.append("workspace_write_authority")

        return IntentEnvelope(
            request_ref=request.sha256,
            request_kind="task",
            task_kind=task_kind,
            requested_execution_mode=requested_mode,
            execution_mode=effective_mode,
            authority_scope=admission.authority_scope,
            route_source=("unclassified" if task_kind == "unknown" else "deterministic_rule"),
            missing_fields=_unique(missing),
            evidence_spans=tuple(evidence),
            effective_model_tools=model_tools_for_mode(effective_mode),
            model_classification_required=task_kind == "unknown",
        )
