# tokenstretch MAX (`tsmax`)

A separate, local-first offshoot of [tokenstretch](https://github.com/ExJtac/tokenstretch), with an isolated Python package, command, installation, and state directory. MIT licensed; see [LICENSE](LICENSE) and [NOTICE](NOTICE).

**Not released yet.** Based on tokenstretch 0.9.6, MAX now adds a local project map and a standalone verification gate. Planning and task execution are next. MAX reports version `0.1.0.dev0`, identifying an unreleased development build. The GitHub repository and release installer are not published yet.

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

`--check KIND JSON_ARGV` supplies an exact command and replaces detected commands of that kind. Kinds are `test`, `lint`, `typecheck` and `acceptance`. Ambiguous or missing required commands need an explicit choice. MAX uses installed tools and does not install dependencies. Checks execute in the selected project directory; use `--root` to select another copy. They can change files, so review the preview first. The future task runner will supply a separate worktree.

Checks run with external networking blocked by the operating system and a temporary home with a reduced environment. Localhost is allowed where the platform supports it. If isolation cannot be established, verification stays incomplete. This is network isolation, not filesystem containment: authorized project code can access host files. Output and execution time are bounded; cleanup covers the process group. Use synchronous checks that wait for their children. Detached background jobs are unsupported, and MAX cannot certify that every descendant has exited.

Only passed required checks with unchanged source, test, configuration and lock hashes produce a passed record. Missing tools, zero tests, reported skipped tests, unsupported test summaries, timeout or incomplete input snapshots remain incomplete. Test-summary recognition currently covers pytest, unittest, Jest/Vitest and TAP; authorized lint/typecheck/acceptance commands use exit status. Results are point-in-time evidence: later edits require verification again. Tool identity uses filesystem metadata, and dynamically loaded external inputs are not fully tracked.

Private records live in `.tsmax/verify/`, with backups and undo. Exit codes are `0` for preview/pass, `1` for failure and `2` for incomplete; `--json` returns structured evidence. Optional UI screenshots are not implemented yet.

## Conductor plan

Next comes a map-only benchmark before planning and execution. The first runner will use a separate Git worktree and ask before applying each task's verified changes; a worktree is not a sandbox. Later work adds reusable approaches and optional models already installed locally. See the revised [Conductor design](docs/CONDUCTOR.md) and [decisions](docs/DECISIONS.md). `tsmax plan` and `tsmax run` are not implemented yet.

## Privacy and boundaries

- No telemetry, analytics, or update checks. The inherited `update` command reports that remote checks are disabled.
- No internet calls except pinned installations you confirm and calls made by your own logged-in AI CLI. The map makes no network calls. The dashboard uses localhost; later Ollama support will also be limited to localhost, with no redirects, cloud models or automatic downloads.
- Credential and login files are never read, copied, or proxied; checking existence is allowed.
- API proxy installation is disabled in MAX. Doctor can still identify proxy risks in an existing setup.
- Tool-specific paths and configuration belong to their adapters.
- Configuration edits use backups and manifests under `~/.tsmax`; `tsmax undo` restores them.
- Tests use fake headless runners and synthetic fixtures. Network-blocked suite results must be reported separately from ordinary tests.

## Measurements

There are **no MAX savings or quality claims yet**. First, follow the [map-only benchmark](docs/MAP-BENCH.md): same task, model, effort and acceptance checks, with and without the map. Include map-building time. Later compare the same real multi-page build using a plain top model, tokenstretch and MAX. Record tokens, Claude Code list-price cost, wall time and quality; upstream measurements do not establish MAX results. We ask before starting live runs because they use your provider allowance.

## Development

```bash
python3 -m pytest -q
sh scripts/test-offline.sh   # OS-level outbound block (localhost allowed); TSMAX_TEST_PYTHON selects Python
```

The development extra pins pytest in `pyproject.toml`. No test should make a real provider call or download a model. Keep source changes focused, preserve upstream history and attribution, and use a separate commit for each reviewed milestone.
