# tokenstretch MAX (`tsmax`)

A separate, local-first offshoot of [tokenstretch](https://github.com/ExJtac/tokenstretch), with an isolated Python package, command, installation, and state directory. MIT licensed; see [LICENSE](LICENSE) and [NOTICE](NOTICE).

**Not released yet.** This is the renamed foundation based on tokenstretch 0.9.6. The Conductor is a proposed next stage, not an implemented feature. MAX reports version `0.1.0.dev0`, identifying an unreleased development build. The GitHub repository and release installer are not published yet.

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

## Conductor plan

The planned Conductor connects a local incremental project map, a project verification gate, short per-task sessions through official AI CLIs, an optional library of successful approaches, and optional already-installed local models. The [Conductor design](docs/CONDUCTOR.md) will be reviewed before implementation. Commands such as `tsmax map`, `tsmax plan`, and `tsmax run` are not available in this foundation.

## Privacy and boundaries

- No telemetry, analytics, or update checks. The inherited `update` command reports that remote checks are disabled.
- No outbound calls except user-confirmed pinned installations and the user's own official AI CLI. The optional inherited dashboard binds only to localhost.
- Credential and login files are never read, copied, or proxied; checking existence is allowed.
- API proxy installation is disabled in MAX. Doctor can still identify proxy risks in an existing setup.
- Tool-specific paths and configuration belong to their adapters.
- Configuration edits use backups and manifests under `~/.tsmax`; `tsmax undo` restores them.
- Tests use fake headless runners and synthetic fixtures. Network-blocked suite results must be reported separately from ordinary tests.

## Measurements

There are **no MAX savings or quality claims yet**. Upstream measurements do not establish MAX results. Before publishing numbers, benchmark the same real multi-page build using a plain top model, tokenstretch, and MAX, and record total tokens, Claude Code list-price cost, wall time, and passing tests and feature checks. Live runs require explicit authorization because they consume provider usage.

## Development

```bash
python3 -m pytest -q
sh scripts/test-offline.sh   # OS-level outbound block (localhost allowed); TSMAX_TEST_PYTHON selects Python
```

The development extra pins pytest in `pyproject.toml`. No test should make a real provider call or download a model. Keep source changes focused, preserve upstream history and attribution, and use a separate commit for each reviewed milestone.
