# Conductor decisions

## 2026-11-14 — Deliver the map and gate first

Ship an independent project map, then an independent test gate. Planning and task execution depend on those milestones and a map-only benchmark. No model is needed to build or search the map.

## 2026-11-14 — Review worktree changes before applying

The first task runner will work in a separate Git worktree and request approval before applying each task's verified diff. Worktrees do not sandbox a process or protect credentials. Existing CLI permissions still apply. Full OS containment and automatic crash recovery are deferred; interrupted work stops for inspection.

## 2026-11-14 — Permit local Ollama later

The owner accepts loopback-only Ollama alongside the existing local dashboard exception. Future offload must reject remote addresses, redirects, proxy settings, cloud-backed models and model downloads. This decision does not enable Ollama calls in the map milestone.

## 2026-11-14 — Prioritize web languages without overstating parsing

Use Python's AST for exact Python symbols and bounded lexical extraction for useful JavaScript/TypeScript coverage now. Label approximate results. A complete JS/TS parser needs a separate decision about optional installed tooling or dependencies; do not silently add downloads.

## 2026-11-14 — Measure retrieval independently

Before building plan/run, compare the same task/model with and without the map. Record raw usage and quality, including map construction overhead. Local indexing tests and timings are engineering evidence, not proof of token or subscription savings. Paid runs and publication still need approval.

## 2026-11-14 — Keep Git outside the first map reader

Git can open paths from repository configuration before MAX can apply its own exclusions. The initial map therefore uses guarded directory traversal and file hashes, with bounded local ignore handling. Defer Git-diff hints and native Git exclusion semantics until they can satisfy the same no-credential-read contract. Unsupported ignore syntax must not silently include files that the user intended to exclude.
