# tokenstretch MAX (`tsmax`)

A separate, local-first offshoot of [tokenstretch](https://github.com/ExJtac/tokenstretch), with an isolated Python package, command, installation, and state directory. MIT licensed; see [LICENSE](LICENSE) and [NOTICE](NOTICE).

**Not released yet.** Based on tokenstretch 0.9.6, MAX adds a local project map, verification gate, provider-neutral planner/runner, reusable procedures and optional local text assistance. MAX reports version `0.1.0.dev0`, identifying an unreleased development build. The GitHub repository and release installer are not published yet.

## What works today

The inherited local tools check coding-tool setups, propose fixes with backups and undo, inspect local usage, trim instruction files, suggest models, manage task briefs and queues, and prepare benchmarks. Supported adapters include Claude Code, Codex, Gemini CLI, and Copilot CLI; additional adapters provide read-only checks. See [docs/DETAILS.md](docs/DETAILS.md) and [ADAPTERS.md](ADAPTERS.md).

MAX uses these names independently of tokenstretch:

| Purpose | MAX |
|---|---|
| Python distribution and package | `tsmax` |
| Command | `tsmax` |
| User state, backups, manifests, caches | `~/.tsmax/` |
| Project state | `.tsmax/` |
| Installer-managed copy | `~/.local/share/tsmax/` |
| Launcher | `~/.local/bin/tsmax` |
| Environment-variable prefix | `TSMAX_` |

Existing tokenstretch state is not migrated or used. Both packages can be installed side by side. Setup deliberately configures the selected coding tools, so preview proposed changes before applying them; each product retains its own backups and undo history.

## Install

Python 3.11 or newer is required. Runtime uses only the standard library.

Until a reviewed MAX release is published, run the local checkout directly:

```bash
python3 -m tsmax --version
python3 -m tsmax --help
```

To install a reviewed, **clean and committed** local checkout without downloading anything:

```bash
sh scripts/install.sh --local-source "$PWD" --dry-run
sh scripts/install.sh --local-source "$PWD"
```

The installer copies the checkout's exact HEAD commit into `~/.local/share/tsmax`, creates a private venv without pip, and writes `~/.local/bin/tsmax`. It does not run setup. It refuses an uncommitted source tree or a destination owned by another installer. `--add-to-path` optionally edits the shell startup file with a backup; otherwise it only prints the PATH instruction.

The remote release pins are intentionally empty. A normal remote installation fails before downloading until a MAX release is explicitly pinned. The installer also supports an explicit tag and commit with `--source`, or a tarball with its SHA-256; see `sh scripts/install.sh --help`.

`sh scripts/install.sh --uninstall` removes the MAX installation and leaves `~/.tsmax` backups available. Run `tsmax undo` before uninstalling if you want setup changes restored.

## Use

```bash
tsmax start                  # preview; asks before applying in a terminal
tsmax doctor                 # inspect setup
tsmax model suggest "fix the flaky login test"
tsmax trim                   # preview instruction-file trimming
tsmax report --weekly        # local usage report
tsmax bench --from-logs      # summarize existing measurements
tsmax undo                   # restore the latest MAX change
```

`python3 -m tsmax` works from the source checkout. A noninteractive `start` changes nothing without `--yes`.

## Find code with the project map

From your project directory, using an installed `tsmax`:

```bash
tsmax map build
tsmax map query "where is login"
tsmax map status
```

The map stores paths, symbols, imports and short summaries locally in `.tsmax/map.sqlite3`. Rebuilding extracts only changed files; an unchanged build writes nothing. Queries return at most ten `file:line` results. Rebuild when files change; query and status never update the index for you. Python symbols use the Python parser. JavaScript/TypeScript declarations and imports use a bounded lexer and are marked approximate; this is not a complete JS/TS parser. Binary, generated, oversized, ignored, known secret and provider-state files are excluded, as are symlinks and hard-linked files. Bounds and skipped files appear in build/status output. The first version uses file hashes and guarded directory traversal, not Git subprocesses. It supports local ignore rules with reported limits; native Git exclusions and Git-diff hints are deferred.

Each changed index is backed up through MAX's normal undo flow. Keep `.tsmax/` local and out of commits. The map requires a platform with descriptor-relative, no-follow file operations; it refuses unsafe fallbacks.

For Claude Code, preview the read-only integration before applying it:

```bash
tsmax map install --tool claude
tsmax map install --tool claude --apply
```

This adds a `tsmax-map` server to the project's `.mcp.json`, preserving other servers and backing up the change. `tsmax undo` restores it. The server answers map queries and status requests over stdio, with no listening port or write tools. Automatic installation for other clients is not implemented yet. `tsmax map serve --root /path/to/project` also provides the stdio server for manual configuration. A configuration created from a source checkout needs that checkout and Python interpreter to remain available.

## Verify project checks

Preview the project's detected tests, linter and typecheck, then authorize their execution:

```bash
tsmax verify
tsmax verify --run
tsmax verify --check test '["python3","-m","pytest","-q"]' --run
```

`--check KIND JSON_ARGV` supplies an exact command and replaces detected commands of that kind. Kinds are `test`, `lint`, `typecheck` and `acceptance`. Ambiguous or missing required commands need an explicit choice. MAX uses installed tools and does not install dependencies. Checks execute in the selected project directory; use `--root` to select another copy. They can change files, so review the preview first. The task runner supplies a separate staging worktree and checks the original project again after applying reviewed changes.

Checks run with external networking blocked by the operating system and a temporary home with a reduced environment. Localhost is allowed where the platform supports it. If isolation cannot be established, verification stays incomplete. This is network isolation, not filesystem containment: authorized project code can access host files. Output and execution time are bounded; cleanup covers the process group. Use synchronous checks that wait for their children. Detached background jobs are unsupported, and MAX cannot certify that every descendant has exited.

Only passed required checks with unchanged source, test, configuration and lock hashes produce a passed record. Missing tools, zero tests, reported skipped tests, unsupported test summaries, timeout or incomplete input snapshots remain incomplete. Test-summary recognition currently covers pytest, unittest, Jest/Vitest and TAP; authorized lint/typecheck/acceptance commands use exit status. Results are point-in-time evidence: later edits require verification again. Tool identity uses filesystem metadata, and dynamically loaded external inputs are not fully tracked.

Private records live in `.tsmax/verify/`, with backups and undo. Exit codes are `0` for preview/pass, `1` for failure and `2` for incomplete; `--json` returns structured evidence. Optional UI screenshots are not implemented yet.

## Plan and run small tasks

Choose an AI tool explicitly. Its adapter supplies model tiers, official CLI commands, permissions and usage parsing; the shared planner and gate do not prefer a provider.

```bash
tsmax plan "add keyboard navigation" --tool codex       # preview only
tsmax plan "add keyboard navigation" --tool codex --run # authorize one planning call
tsmax run                                             # review saved tasks and checks
tsmax run --prepare                                   # prepare/inspect worktree; no model calls
tsmax run --execute --max-calls 6                      # authorize at most six worker CLI sessions
# After reviewing the complete diff and passing checks:
tsmax run --apply T1 --review EXACT_TOKEN_FROM_REVIEW
```

The selected official CLI uses its existing login. Planning writes a validated `.tsmax/plan.json` and readable `.tsmax/plan.md`. Each task names its dependencies, allowed files, acceptance criteria and exact check commands. Execution uses a separate temporary Git worktree from a recorded commit. Workers get a fresh session with their task, current map results and bounded source excerpts. Two unsuccessful mid-tier attempts allow one top-tier attempt with the failure/spec/map context. All attempts count toward the persisted call limit; changing flags cannot reset it. This limit counts CLI sessions, each of which may contain multiple model turns. It is not an API-request, token or dollar cap; available native limits are adapter-specific.

Prepare dependencies explicitly in the printed worktree before execution. MAX does not install, copy or share dependencies automatically. Declared Node dependencies without a local `node_modules` directory stop before a worker call. The worktree preserves eligible source, check configuration, locks, SVGs, and bounded static images/fonts. Binary assets are read-only task inputs; their hashes participate in verification. Other excluded files are not materialized, so this is a filtered worktree. Git's own status may show omitted files as deleted; MAX's review uses the materialized baseline. Standard relative executable links contained inside staged `node_modules` are checked as metadata. Linked dependency directories, external links, source symlinks and hard links are refused.

A passing staging gate produces a review token for the complete proposed diff. Applying requires that exact token, fresh checks and unchanged source preimages. MAX then reruns the approved checks in the original project; only a fresh pass marks the task done and releases dependent tasks. A failed or incomplete check preserves the changes and blocks further work. The apply result names the source-change manifest for `tsmax undo MANIFEST`; later state records have their own manifests.

Unrelated dirty work stays in place. Relevant dirty inputs, changed gate definitions, out-of-scope edits, unsafe files and oversized reviews are refused. Source edits preserve reviewed regular permissions, and deletions are backed up and undoable. Backups and an undo manifest are prepared before source edits so a partial application can be inspected and undone. There is no automatic rollback or crash recovery. Interrupted/uncertain calls stop for inspection and are never replayed automatically. Worktrees separate files; they do not contain host or credential access. Use trusted synchronous checks.

Build outputs are never applied back: `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `dist`, `build`, `coverage`, `.next`, `target`, `.eslintcache`, and `*.tsbuildinfo`. These exceptions do not permit dependency edits. Inspection remains bounded to 100,000 filesystem entries and depth 64; oversized projects stop explicitly. Eligible source is limited to 1 MB per file, supported static assets to 8 MB per file, and included content to 64 MB total. A task producing no source change requires review.

Current execution profiles are locally verified for specific Claude Code and Codex CLI versions on macOS. Gemini CLI and Copilot CLI retain their adapters but do not yet have a verified Conductor execution profile. Missing tools, unsupported versions/platforms or unsafe startup configuration refuse before a model call. New adapters implement the same capability/request/response contract; tests exercise arbitrary provider IDs. No live model execution has been measured yet, and account/model availability remains unknown until an explicitly authorized call. Runtime stays Python standard library only.

See the [Conductor design](docs/CONDUCTOR.md) and [decisions](docs/DECISIONS.md).

## Reuse a verified approach

After a task passes verification in the original project, MAX offers a command to preview saving its approach:

```bash
tsmax skills save T1
tsmax skills save T1 --apply --review EXACT_TOKEN_FROM_PREVIEW
tsmax skills suggest "keyboard navigation"
```

The preview shows the procedure, applicability, limitations and original verification evidence. Explicit application writes private `pattern.json` and `SKILL.md` files under `.tsmax/skills/`, with backups and undo. Nothing is saved automatically. These project patterns work independently of a provider; export to a tool's user skill folder is not implemented.

Saving preserves historical evidence. If later source changes differ from the verified snapshot, the preview and saved record say so; that does not establish current correctness. A failed task or missing verification record cannot supply a pattern. New planning includes at most three relevant, bounded summaries as reference material. Every new task still needs its own checks.

## Optional local text assistance

If Ollama and a suitable model are already installed and the runtime is running:

```bash
tsmax local status
tsmax local summary --model EXACT_LOCAL_MODEL --stdin       # metadata preview
tsmax local handoff --model EXACT_LOCAL_MODEL --stdin --run # one local inference
tsmax local commit-message --model EXACT_LOCAL_MODEL --stdin --run
```

Supply text on standard input and finish with EOF, or use `--text`. Input and output are bounded to 16,000 UTF-8 bytes; each HTTP request has a total deadline. Preview does not send the supplied text. `--run` authorizes one generation and returns a text proposal; it does not edit files, execute the proposal or complete a coding task. MAX saves no prompts or outputs; the existing runtime controls its own logging.

Only numeric loopback HTTP endpoints are accepted (default `http://127.0.0.1:11434`), without DNS, environment proxies, redirects or retries. Model metadata must establish local GGUF weights and completion support; cloud/remote models and ambiguous metadata are refused. There are no model installation, download or daemon-start commands. This trusts the installed runtime's metadata, not a cryptographically verified daemon, and does not contain the daemon's network access. Stop concurrent model-management operations during a request. llama.cpp command detection reports availability only; its execution profile is not implemented. Real Ollama inference has not yet been tested in this development build; tests use synthetic local servers.

## Privacy and boundaries

- No telemetry, analytics, or update checks. The inherited `update` command reports that remote checks are disabled.
- No internet calls except pinned installations you confirm and calls made by your own logged-in AI CLI. The map makes no network calls. The dashboard and optional Ollama client use localhost, with no redirects, cloud models or automatic downloads in the local client.
- Credential and login files are never read, copied, or proxied; checking existence is allowed.
- API proxy installation is disabled in MAX. Doctor can still identify proxy risks in an existing setup.
- Tool-specific paths and configuration belong to their adapters.
- Configuration edits use backups and manifests under `~/.tsmax`; `tsmax undo` restores them.
- Tests use fake headless runners and synthetic fixtures. Network-blocked suite results must be reported separately from ordinary tests.

## Measurements

There are **no MAX savings or quality claims yet**. The deferred [map-only benchmark](docs/MAP-BENCH.md) uses the same task, provider/model, effort and acceptance checks, with and without the map, including map-building time. Later compare the same real multi-page build using a plain top model, tokenstretch and MAX. Record tokens, reported cost when available, wall time and quality; upstream measurements do not establish MAX results. Measurements do not block the provider-neutral build. We ask before starting live runs because they use your provider allowance.

## Development

```bash
python3 -m pytest -q
sh scripts/test-offline.sh   # OS-level outbound block (localhost allowed); TSMAX_TEST_PYTHON selects Python
```

The development extra pins pytest in `pyproject.toml`. No test should make a real provider call or download a model. Keep source changes focused, preserve upstream history and attribution, and use a separate commit for each reviewed milestone.
