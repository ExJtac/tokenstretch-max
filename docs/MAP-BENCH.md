# Map-only benchmark protocol

Status: protocol prepared; no paid trials or savings measurements recorded.

Run this comparison before implementing plan/run. It isolates whether the map reduces repeated reading, without also changing the model or prompt quality.

1. Choose a real repository at one fixed commit and a concrete task with known acceptance checks. Freeze the task text, model, reasoning effort, CLI version, permissions, instruction files, available tools and dependency versions. Do not include credentials or private material in publishable artifacts.
2. Prepare identical starting copies and at least three paired trials. Alternate which condition runs first. Use fresh sessions; do not reuse answers, handoffs or chat history. Record cache usage separately because a fresh session does not guarantee a cold provider cache.
3. Baseline: run the task without the MAX map integration. Treatment: run the same task with the map built and read-only `map query` available. Keep all other tools identical. Record the small map-use instruction as part of the treatment, rather than concealing its prompt cost.
4. Measure initial map-build time, incremental refresh time, query time and query/output volume. Include map overhead in treatment wall time. Preserve file-read operations and repeated reads when the CLI exposes them; do not infer unseen reads or token counts from characters.
5. Record the CLI's input, output, cached and reasoning tokens without double-counting totals; record model calls, wall time, errors, tests and feature acceptance. If using Claude Code, record its reported list-price cost separately from subscription charges. Missing fields stay unknown.
6. Run the same acceptance checks against both results. A cheaper run that fails acceptance is a failed run, not a quality-preserving saving. Preserve every trial, including failures. Report per-trial figures and median/spread; state sample size and cache conditions.
7. Save raw measurements locally and have the owner review the evidence before publishing claims. No paid CLI calls start without an agreed trial count and spending/usage authorization. Do not bypass rate limits or reuse login files.

Suggested local results table:

| Pair | Condition | Commit/model/effort | Tokens by category | Reported cost | Build/query seconds | Total seconds | Repeated reads | Acceptance |
|---|---|---|---|---|---|---|---|---|
| — | baseline / map | — | unknown | unknown | — | — | unknown | not run |

Keep results outside tracked source by default. A synthetic fixture or a fast map query validates indexing behavior only; it does not replace this paired model benchmark.
