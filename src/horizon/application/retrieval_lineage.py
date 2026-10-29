from __future__ import annotations

from dataclasses import dataclass

from horizon.application.context import visible_run_memory_binding
from horizon.domain.common import canonical_json, digest
from horizon.domain.context import ContextProjection
from horizon.domain.errors import IntegrityError
from horizon.domain.events import Event
from horizon.domain.memory import RunMemorySnapshot
from horizon.domain.model import ModelCallRecord, ModelCallReservation, ModelResponse
from horizon.domain.ports import ArtifactStorePort
from horizon.domain.retrieval import EvidencePack
from horizon.domain.run import Run
from horizon.domain.run_evaluation import (
    RunABRetrievalLineageSummary,
    RunABWriteEvidenceLineage,
)
from horizon.domain.tools import WRITE_TOOLS, ToolCallRecord, ToolCallReservation


@dataclass(frozen=True)
class _RetrievalEvidence:
    record: ToolCallRecord
    settled_seq: int
    evidence: EvidencePack
    payload: str
    provider_tool_call_id: str


@dataclass(frozen=True)
class _DeliveredRetrieval:
    retrieval: _RetrievalEvidence
    channels: frozenset[str]
    memory_excerpt: str = ""


def _event_contracts(
    events: tuple[Event, ...],
) -> tuple[
    dict[str, tuple[ModelCallReservation, int]],
    dict[str, tuple[ToolCallReservation, int]],
    dict[str, tuple[ToolCallRecord, int]],
]:
    model_reservations: dict[str, tuple[ModelCallReservation, int]] = {}
    tool_reservations: dict[str, tuple[ToolCallReservation, int]] = {}
    tool_records: dict[str, tuple[ToolCallRecord, int]] = {}
    for event in events:
        if event.event_type == "MODEL_CALL_RESERVED":
            reservation = ModelCallReservation.model_validate(event.payload["reservation"])
            model_reservations[reservation.call_id] = (reservation, event.seq)
        elif event.event_type == "TOOL_CALL_RESERVED":
            reservation = ToolCallReservation.model_validate(event.payload["reservation"])
            tool_reservations[reservation.call_id] = (reservation, event.seq)
        elif event.event_type == "TOOL_CALL_SETTLED":
            record = ToolCallRecord.model_validate(event.payload["record"])
            tool_records[record.call_id] = (record, event.seq)
    return model_reservations, tool_reservations, tool_records


def _retrieval_provider_tool_call_ids(
    events: tuple[Event, ...],
    artifacts: ArtifactStorePort,
    model_reservations: dict[str, tuple[ModelCallReservation, int]],
    tool_reservations: dict[str, tuple[ToolCallReservation, int]],
) -> dict[str, str]:
    """Bind retrieval reservations to provider tool-call IDs by exact event order/hash."""

    model_reserved_seqs = sorted(seq for _, seq in model_reservations.values())
    ordered_tools = sorted(
        ((seq, reservation) for reservation, seq in tool_reservations.values()),
        key=lambda item: item[0],
    )
    result: dict[str, str] = {}
    for event in events:
        if event.event_type != "MODEL_CALL_SETTLED":
            continue
        record = ModelCallRecord.model_validate(event.payload["record"])
        if record.response_artifact_ref is None:
            raise IntegrityError("Retrieval lineage requires recorded model responses")
        response = ModelResponse.model_validate_json(
            artifacts.read(record.response_artifact_ref)
        )
        upper = next((seq for seq in model_reserved_seqs if seq > event.seq), None)
        candidates = [
            reservation
            for seq, reservation in ordered_tools
            if seq > event.seq and (upper is None or seq < upper)
        ]
        cursor = 0
        for provider_call in response.message.tool_calls:
            if provider_call.function.name != "retrieve_code":
                continue
            expected_hash = digest(provider_call.function.arguments)
            match_index = next(
                (
                    index
                    for index in range(cursor, len(candidates))
                    if candidates[index].name == provider_call.function.name
                    and candidates[index].arguments_hash == expected_hash
                ),
                None,
            )
            if match_index is None:
                raise IntegrityError(
                    "Model response tool call has no matching durable reservation: "
                    f"{provider_call.function.name}"
                )
            reservation = candidates[match_index]
            result[reservation.call_id] = provider_call.id
            cursor = match_index + 1
    return result


def _projection(
    reservation: ModelCallReservation,
    artifacts: ArtifactStorePort,
) -> ContextProjection:
    if reservation.context_projection_ref is None:
        raise IntegrityError("Execution model call has no context projection")
    projection = ContextProjection.model_validate_json(
        artifacts.read(reservation.context_projection_ref)
    )
    if digest(projection) != reservation.context_projection_ref:
        raise IntegrityError("Context projection is not bound to its artifact reference")
    if (
        projection.run_memory_ref != reservation.run_memory_ref
        or projection.run_memory_hash != reservation.run_memory_hash
    ):
        raise IntegrityError("Context projection Run memory does not match its reservation")
    return projection


def _memory_delivery(
    reservation: ModelCallReservation,
    projection: ContextProjection,
    artifacts: ArtifactStorePort,
) -> dict[str, tuple[str, str, str | None]]:
    if reservation.run_memory_ref is None:
        return {}
    memory = RunMemorySnapshot.model_validate_json(
        artifacts.read(reservation.run_memory_ref)
    )
    binding = visible_run_memory_binding(memory, reservation.run_memory_ref)
    system_content = projection.messages[0].content or ""
    if canonical_json(binding) not in system_content:
        raise IntegrityError("Stored context projection omits its visible Run-memory binding")
    return {
        entry.source_call_id: (
            entry.excerpt[:240],
            entry.evidence_ref,
            entry.dependency_revision,
        )
        for entry in memory.entries[-2:]
        if entry.tool_name == "retrieve_code"
        and entry.outcome == "success"
        and entry.status == "active"
        and entry.evidence_ref is not None
        and entry.excerpt
    }


def _deliveries_for_model_call(
    reservation: ModelCallReservation,
    reservation_seq: int,
    retrievals: dict[str, _RetrievalEvidence],
    artifacts: ArtifactStorePort,
) -> tuple[_DeliveredRetrieval, ...]:
    projection = _projection(reservation, artifacts)
    memory_excerpts = _memory_delivery(reservation, projection, artifacts)
    direct_messages = {
        (message.tool_call_id, message.content)
        for message in projection.messages
        if message.role == "tool"
    }
    delivered: list[_DeliveredRetrieval] = []
    for retrieval in retrievals.values():
        if retrieval.settled_seq >= reservation_seq:
            continue
        channels: set[str] = set()
        if (retrieval.provider_tool_call_id, retrieval.payload) in direct_messages:
            channels.add("direct_tool_message")
        memory_binding = memory_excerpts.get(retrieval.record.call_id)
        memory_excerpt = memory_binding[0] if memory_binding is not None else ""
        if (
            memory_binding is not None
            and memory_binding[1] == retrieval.record.artifact_ref
            and memory_binding[2] == retrieval.evidence.workspace_revision
        ):
            channels.add("run_memory_excerpt")
        if channels:
            delivered.append(
                _DeliveredRetrieval(
                    retrieval=retrieval,
                    channels=frozenset(channels),
                    memory_excerpt=memory_excerpt,
                )
            )
    return tuple(sorted(delivered, key=lambda item: item.retrieval.settled_seq))


def derive_run_ab_retrieval_lineage(
    run: Run,
    events: list[Event] | tuple[Event, ...],
    artifacts: ArtifactStorePort,
) -> RunABRetrievalLineageSummary:
    """Derive structural retrieval delivery/consumption evidence for one completed Run.

    The result proves that content-addressed retrieval evidence was visible in a stored model
    projection and aligned with a successful write's target/revision.  It intentionally does not
    infer semantic causality from a scripted model action.
    """

    ordered_events = tuple(sorted(events, key=lambda event: event.seq))
    if not ordered_events or ordered_events[-1].seq != run.seq:
        raise IntegrityError("Retrieval lineage events do not cover the requested Run")
    model_reservations, tool_reservations, tool_records = _event_contracts(ordered_events)
    provider_call_ids = _retrieval_provider_tool_call_ids(
        ordered_events,
        artifacts,
        model_reservations,
        tool_reservations,
    )

    retrievals: dict[str, _RetrievalEvidence] = {}
    for call_id, (record, settled_seq) in tool_records.items():
        if record.name != "retrieve_code" or record.status != "success":
            continue
        if record.artifact_ref is None:
            raise IntegrityError("Successful retrieval has no EvidencePack artifact")
        if record.artifact_ref != record.output_hash:
            raise IntegrityError("Successful retrieval artifact is not content addressed")
        provider_tool_call_id = provider_call_ids.get(call_id)
        if provider_tool_call_id is None:
            raise IntegrityError("Successful retrieval is not bound to a model tool call")
        payload_bytes = artifacts.read(record.artifact_ref)
        evidence = EvidencePack.model_validate_json(payload_bytes)
        if not (
            evidence.workspace_revision
            == record.workspace_revision_before
            == record.workspace_revision_after
        ):
            raise IntegrityError("Retrieval EvidencePack revision does not match its receipt")
        retrievals[call_id] = _RetrievalEvidence(
            record=record,
            settled_seq=settled_seq,
            evidence=evidence,
            payload=payload_bytes.decode("utf-8"),
            provider_tool_call_id=provider_tool_call_id,
        )

    model_deliveries: dict[str, tuple[_DeliveredRetrieval, ...]] = {}
    delivered_ids: set[str] = set()
    for call_id, (reservation, reservation_seq) in model_reservations.items():
        if reservation.purpose != "execution":
            continue
        delivered = _deliveries_for_model_call(
            reservation,
            reservation_seq,
            retrievals,
            artifacts,
        )
        model_deliveries[call_id] = delivered
        delivered_ids.update(item.retrieval.record.call_id for item in delivered)

    writes: list[RunABWriteEvidenceLineage] = []
    for call_id, (record, _settled_seq) in sorted(
        tool_records.items(), key=lambda item: item[1][1]
    ):
        if record.name not in WRITE_TOOLS or record.status != "success":
            continue
        reservation_entry = tool_reservations.get(call_id)
        if reservation_entry is None:
            raise IntegrityError("Successful write has no durable reservation")
        tool_reservation, _ = reservation_entry
        precondition = tool_reservation.write_precondition
        if precondition is None:
            raise IntegrityError("Successful write has no model-context precondition")
        model_entry = model_reservations.get(precondition.source_model_call_id)
        if model_entry is None:
            raise IntegrityError("Successful write references an unknown model reservation")
        model_reservation, _ = model_entry
        if model_reservation.context_projection_ref is None:
            raise IntegrityError("Write source model call has no context projection")
        delivered = model_deliveries.get(precondition.source_model_call_id, ())
        same_revision = [
            item
            for item in delivered
            if item.retrieval.evidence.workspace_revision
            == precondition.expected_workspace_revision
            == precondition.observed_workspace_revision
            == record.workspace_revision_before
        ]
        covered: set[str] = set()
        channels: set[str] = set()
        for item in delivered:
            channels.update(item.channels)
        for item in same_revision:
            chunk_paths = {chunk.path for chunk in item.retrieval.evidence.chunks}
            for target_path in precondition.target_paths:
                direct_visible = "direct_tool_message" in item.channels
                memory_visible = (
                    "run_memory_excerpt" in item.channels
                    and canonical_json(target_path) in item.memory_excerpt
                )
                if target_path in chunk_paths and (direct_visible or memory_visible):
                    covered.add(target_path)
        delivered_call_ids = tuple(
            item.retrieval.record.call_id for item in delivered
        )
        covered_paths = tuple(sorted(covered))
        evidence_backed = bool(precondition.target_paths) and set(
            precondition.target_paths
        ) <= covered
        writes.append(
            RunABWriteEvidenceLineage(
                write_call_id=record.call_id,
                write_tool=record.name,
                source_model_call_id=precondition.source_model_call_id,
                context_projection_ref=model_reservation.context_projection_ref,
                run_memory_ref=model_reservation.run_memory_ref,
                target_paths=precondition.target_paths,
                delivered_retrieval_call_ids=delivered_call_ids,
                delivery_channels=tuple(sorted(channels)),
                covered_target_paths=covered_paths,
                same_revision_retrieval_count=len(same_revision),
                context_bound=bool(delivered_call_ids),
                evidence_backed=evidence_backed,
            )
        )

    ordered_retrievals = tuple(
        item.record.call_id
        for item in sorted(retrievals.values(), key=lambda item: item.settled_seq)
    )
    ordered_delivered = tuple(call_id for call_id in ordered_retrievals if call_id in delivered_ids)
    write_lineages = tuple(writes)
    return RunABRetrievalLineageSummary(
        successful_retrieval_call_ids=ordered_retrievals,
        delivered_retrieval_call_ids=ordered_delivered,
        successful_write_count=len(write_lineages),
        write_context_bound_count=sum(item.context_bound for item in write_lineages),
        evidence_backed_write_count=sum(item.evidence_backed for item in write_lineages),
        writes=write_lineages,
    )
