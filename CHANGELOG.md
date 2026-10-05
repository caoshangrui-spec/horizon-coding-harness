# Changelog

All notable changes to this project are documented here. The project follows semantic versioning
for public releases; unreleased work must not be presented as a published release.

## Unreleased

- Added explicit recovery for an uncertain single-file `create_file`: the recorded response,
  argument hash, WorkItem authority, pre-dispatch manifest, and exact content-derived manifest
  classify the live workspace as pre-effect, expected-effect, or diverged. Trusted offline
  `--accept-write` / `--rollback-write` decisions settle only the first two states, persist the
  recovery observation and next Agent session atomically, never replay the unknown creation, and
  keep partial or externally drifted states blocked. Real subprocess exit, CLI, rollback conflict
  restoration, Trace replay, and snapshot-hidden path rejection are covered.
- Extended explicit staging promotion to carry at most one validated new UTF-8 file within the
  existing eight-change cap. The plan records `kind=created`, an absent before hash, the candidate
  hash, path authority, source/candidate revisions, optional Git HEAD, and a `/dev/null` audit diff.
  Exclusive publication, identity-bound ordinary rollback, exact absent/after crash recovery,
  mixed partial-effect continuation, CLI dry-run/confirmation, and Trace replay are covered; delete,
  rename, and multiple created files remain rejected.
- Added a deliberately narrow `create_file` tool: one allowed UTF-8 file, a 64 KiB byte cap,
  an existing-parent requirement, and exclusive no-overwrite creation. Exact arguments are bound
  through the recorded model response and intent hash; successful receipts bind revisions and the
  post-effect manifest. Ordinary failures clean up the file, while a real subprocess hard exit
  remains blocking `unknown` without replay or unsupported automatic recovery.
- Persisted a controller-generated client Trace ID in every new planning/execution model intent
  before provider dispatch. Canonical response publication is now shared and verified; ordinary
  post-response persistence/receipt failures immediately quarantine both ledgers, while a real
  subprocess hard exit after model return but before Artifact publication replays as blocking
  `unknown` without an automatic model retry.
- Unified input sizing with the exact canonical OpenAI-compatible body sent by the adapter. New
  reservations persist the payload hash, total UTF-8 bytes, per-top-level-field value bytes, and
  JSON structural bytes under estimator `openai_payload_utf8_bytes_x2_plus_1024_v2`; historical
  v1 traces and recovery remain readable.
- Added an offline `model sizing-report` over five deterministic request shapes: minimal ASCII,
  multibyte messages, nested tool schema, JSON-in-JSON tool arguments, and an 8 KiB tool result.
  It performs no network/model call and does not promote the candidate estimator.
- Persisted pre-dispatch model request sizing beside typed budget stops, including call/request
  identity, purpose, estimator input bytes, input ceiling, configured input cap, and output ceiling;
  planning/execution CLI output and Trace replay expose the same evidence without changing old
  Trace projection hashes.
- Added a zero-cost candidate-estimator replay to `trace reservation-report`. On the three
  historical calls that contain request-byte metadata, `request_bytes + 1024` had zero observed
  underestimates and a 4.137869 median ceiling/usage ratio; the sample is explicitly insufficient
  to change the production gate.
- Added an offline `trace reservation-report` command that replay-verifies one or more traces,
  correlates model reservations with settlements, quantifies token/cost pressure, rejects duplicate
  Runs, and preserves the distinction between PriceCard estimates and provider invoices.
- Measured all six real-model traces without another provider call: 20 settled calls had a 6.861621
  aggregate reserved-to-settled cost ratio; the evidence diagnoses over-reservation but does not
  automatically weaken the estimator or budget gates.
- Recorded a sixth replayable real-model negative result: revision-bound retrieval corrected an
  unsupported Plan location to the actual `tenumerate` implementation at rank 1, but the next
  model dispatch stopped at the Campaign gate before any edit or validation.
- Clarified that complete-inventory validation proves only path existence, not semantic location,
  and restored the planning instruction that exact paths require immutable-task grounding.
- Rejected model-generated paths that are absent from a complete planning inventory while
  preserving abstention when that inventory is truncated; the real-model response is still
  settled once and falls back to the existing human-plan path without an implicit retry.
- Recorded a fifth replayable real-model negative result: planning cost CNY 0.005574, then the
  first execution dispatch stopped before the provider because its reservation exceeded the Run
  remainder by CNY 0.000096; no tool, edit, or validation occurred.
- Added a Trace-verifiable retrieval-to-write lineage to the offline portfolio demo: report schema
  v2 binds the retrieval Artifact, write-time context projection, exact preimage hash, target path,
  and workspace revision while retaining read compatibility for schema v1 reports.
- Added a second, independently frozen external localization holdout across Luigi, Sanic, and
  Tornado; current v7 retrieval ranked all three changed production files first while preserving
  two degraded cases caused by explicitly skipped binary/minified assets.
- Added a three-project external localization blind baseline and a bounded path-diversity
  treatment; the treatment recovers all four changed production paths in top 5 while the
  unchanged 0/3 Hit@1 result remains an explicit limitation.
- Added a frozen same-name-symbol retrieval diagnostic and revision-aware path-context ranking;
  the pre-fix Hit@1/leakage failure remains recorded alongside the zero-model treatment report.
- Added a replayable full-request input-token budget: execution context projection now compacts
  against both character and conservative token ceilings, while planning and probes fail before
  provider dispatch when the same configured ceiling is exceeded.
- Added real-Docker hard-exit coverage for the indistinguishable pre-create and post-cleanup
  `run_check` recovery windows.

## 0.1.0 - 2026-10-03

- Added an event-sourced, replayable Run state with idempotent commands and fenced Worker leases.
- Added durable checkpoints, recovery classification, typed budget stops, and CNY campaign limits.
- Added bounded tool calling, protected validation, staged promotion, and fault-injection tests.
- Added deterministic context projection, mandatory fact ledgers, Run Memory, and lexical Code RAG.
- Added narrow persistent HITL paths and one evidence-bound execution replan.
- Added offline retrieval, reliability, and source-bound Run A/B evaluations.
- Added a one-command portfolio demo with a self-verified EvidencePack.
- Added task-specific real-model preflight evidence for the initial planning reservation.
- Stabilized POSIX artifact verification after hard-link publication and enforced LF checkouts.

Evidence boundaries remain explicit: the release does not claim a real-model Issue success,
official benchmark score, production sandbox security, or completion of the full design backlog.
