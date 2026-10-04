# Changelog

All notable changes to this project are documented here. The project follows semantic versioning
for public releases; unreleased work must not be presented as a published release.

## Unreleased

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
