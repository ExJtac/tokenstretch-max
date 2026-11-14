# Map-only benchmark protocol

Status: deferred at the owner's direction to prioritize building MAX for any supported AI. No paid trials or savings measurements recorded. The paired methodology remains a future measurement protocol; neither a provider nor a trial budget is approved.

Run this comparison before making map savings claims. It isolates whether the map reduces repeated reading, without also changing the model or prompt quality. It does not block implementing plan/run and must work through the selected provider's adapter.

1. Choose one of the owner's real web applications at a fixed Git commit and a **medium task expected to touch 5–15 files**, with known acceptance checks. Do not pad a small change with artificial files; record actual files touched. Freeze the task text, model, reasoning effort, CLI version, permissions, instruction files, available tools and dependency versions. Keep the project's identity, source, prompts, screenshots, diffs and results private, outside this repository.
2. Prepare identical starting copies and at least three paired trials. Alternate which condition runs first. Use fresh sessions; do not reuse answers, handoffs or chat history. Record cache usage separately because a fresh session does not guarantee a cold provider cache.
3. Baseline: run the task without the MAX map integration. Treatment: run the same task with the map built and read-only `map query` available. Keep all other tools identical. Record the small map-use instruction as part of the treatment, rather than concealing its prompt cost.
4. Measure initial map-build time, incremental refresh time, query time and query/output volume. Include map overhead in treatment wall time. Preserve file-read operations and repeated reads when the CLI exposes them; do not infer unseen reads or token counts from characters.
5. Record the CLI's input, output, cached and reasoning tokens without double-counting totals; record model calls, wall time, errors, tests and feature acceptance. If using Claude Code, record its reported list-price cost separately from subscription charges. Missing fields stay unknown.
6. Run the same acceptance checks against both results. A cheaper run that fails acceptance is a failed run, not a quality-preserving saving. Preserve every trial, including failures. Report per-trial figures and median/spread; state sample size and cache conditions.
7. Agree the budget **before the first paid call**, so one approval covers the named task, fixed revision, exact model, trial count, per-run and aggregate stop rules, time/turn limits and handling of incomplete runs. Three pairs on Sonnet are a candidate, not a promise that a medium task costs only a few dollars. No paid CLI calls start without this approval. Do not bypass rate limits or reuse login files.
8. Save artifacts locally with owner-only permissions. Have an independent reviewer review all six per-trial records and send the private comparison to the owner for review before anything goes into a README. A protocol approval does not authorize publishing results or source. Publication requires a separate decision and privacy review.

## Trial approval and stop rules

The private run card must name the project/commit, task, expected file scope, acceptance checks, exact model and effort, and any baseline checks not yet verified. Before paid execution, verify the frozen source and synthetic-data test environment work without production access. If the baseline fails, stop before spending.

The previously proposed Claude-specific allowance was six calls with a $1 reported-cost threshold per call ($6 planned total), 25 turns and 15 minutes per call. It is deferred and unapproved. Any future run card must use the selected provider's actual limit and accounting capabilities. Reported-cost thresholds are not guaranteed billing ceilings: an in-flight call may cross a threshold, and estimates may exclude helper calls. Disclose the concrete provider's limits before approval. If a guaranteed hard ceiling is required, do not run until a verified provider-enforced cap is available.

Read the final cumulative cost once per run. Before starting another run, require enough remaining aggregate allowance for its full configured threshold; stop the batch after an overshoot, unknown cost, model mismatch, authentication problem, or unexpected external action. Do not replace failed or budget-exhausted trials with unapproved extra attempts. A timeout or exhausted budget is an incomplete result, not a pass.

Use the user's existing official CLI login without copying credentials, changing billing settings or buying credits. Do not use API-key-only modes or the existing before/after profile benchmark unchanged: profile edits would confound this map-only comparison. Record the model actually reported and refuse silent substitution. All acceptance tests use synthetic data and local endpoints; no production writes, provider generation, deployments or uploads are part of this task.

Suggested local results table:

| Pair | Condition | Commit/model/effort | Tokens by category | Reported cost | Build/query seconds | Total seconds | Repeated reads | Acceptance |
|---|---|---|---|---|---|---|---|---|
| — | baseline / map | — | unknown | unknown | — | — | unknown | not run |

Keep results outside tracked source by default. A synthetic fixture or a fast map query validates indexing behavior only; it does not replace this paired model benchmark.
