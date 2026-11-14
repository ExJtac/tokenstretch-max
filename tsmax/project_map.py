"""A bounded, local project map with no-follow reads and reversible writes.

Queries and status only read. Every rebuild hashes eligible content. Git metadata is never opened: its config
includes could escape the no-follow read policy.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fnmatch
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from typing import Iterable
import uuid
import unicodedata

from .adapters.map_support import current_exclusions
from .context import Context
from .installer import apply_changes
from .map_extract import extract, one_line

INDEX = ".tsmax/map.sqlite3"
SCHEMA_VERSION = 1
MAX_FILE_BYTES = 1_000_000
MAX_FILES = 20_000
MAX_TOTAL_BYTES = 64_000_000
MAX_INDEX_BYTES = 32_000_000
MAX_QUERY_CHARS = 512
GENERATED_DIRS = frozenset({".git", ".hg", ".svn", ".tsmax", "node_modules", "vendor", "dist", "build", "coverage", ".next", ".nuxt", ".output", ".cache", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "target"})
SECRET_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".azure", ".kube", "secrets", "credentials", "keychains"})
SECRET_NAMES = frozenset({"auth.json", "config.json.secret", "providers.json", "globalstate.json", "state.vscdb", ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "google_accounts.json", "oauth_creds.json", "gemini-credentials.json", "mcp-oauth-tokens.json", "a2a-oauth-tokens.json", "oauth-keys.env"})
SECRET_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".keychain", ".keychain-db", ".env"})
GENERATED_NAMES = frozenset({"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "cargo.lock", "composer.lock"})
TEXT_SUFFIXES = frozenset({".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".md", ".mdx", ".rst", ".txt", ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml", ".html", ".htm", ".css", ".scss", ".sass", ".less", ".sh", ".bash", ".zsh", ".fish", ".sql", ".graphql", ".gql", ".go", ".rs", ".java", ".kt", ".kts", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".vue", ".svelte", ".ex", ".exs", ".erl", ".hrl", ".clj", ".cljs", ".lua", ".r", ".dart", ".proto", ".prisma", ".tf", ".tfvars.example"})
TEXT_NAMES = frozenset({"makefile", "dockerfile", "containerfile", "license", "notice", "readme", "justfile", "gemfile", "rakefile", ".gitignore", ".dockerignore", ".editorconfig", ".gitattributes"})
_SCHEMA = {
    "map_meta": "CREATE TABLE map_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "map_files": "CREATE TABLE map_files (path TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL)",
}


class MapError(ValueError):
    """Unsafe input, unsupported filesystem, or invalid map; no implicit repair."""


def _absolute(path: Path) -> Path:
    # abspath normalizes components but intentionally does not resolve symlinks.
    return Path(os.path.abspath(os.fspath(path)))


def _rel_ok(rel: str) -> bool:
    p = PurePosixPath(rel)
    return bool(rel) and not p.is_absolute() and p.as_posix() == rel and all(part not in {"", ".", ".."} for part in p.parts) and not any(unicodedata.category(c).startswith("C") or c in "\u2028\u2029" for c in rel)


@contextmanager
def _root_fd(root: Path):
    if not hasattr(os, "O_NOFOLLOW") or os.name != "posix":
        raise MapError("Project maps require no-follow directory-descriptor filesystem support.")
    root = _absolute(root)
    if any(part.lower() in SECRET_DIRS or part == ".tsmax" for part in root.parts):
        raise MapError("Project root is inside a secret/state directory.")
    fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.parts[1:]:
            new_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new_fd
        yield fd
    except OSError as exc:
        raise MapError("Project directory is unavailable or has a symlink ancestor.") from exc
    finally:
        os.close(fd)


def _exclusions(root: Path, excluded_paths: Iterable[Path]) -> list[str]:
    result = set()
    for path in excluded_paths:
        absolute = _absolute(path if Path(path).is_absolute() else root / path)
        if absolute == root or absolute in root.parents:
            raise MapError("The project root is inside an excluded sensitive directory.")
        try:
            rel = absolute.relative_to(root).as_posix()
        except ValueError:
            continue
        if rel == ".":
            raise MapError("The project root is an excluded sensitive directory.")
        if _rel_ok(rel):
            result.add(rel)
    return sorted(result)


class Policy:
    def __init__(self, exclusions=(), max_file_bytes=MAX_FILE_BYTES, max_files=MAX_FILES, max_total_bytes=MAX_TOTAL_BYTES):
        self.exclusions = tuple(exclusions)
        self.max_file_bytes = min(MAX_FILE_BYTES, max(1, int(max_file_bytes)))
        self.max_files = min(MAX_FILES, max(1, int(max_files)))
        self.max_total_bytes = min(MAX_TOTAL_BYTES, max(1, int(max_total_bytes)))

    def json(self):
        return {"exclusions": list(self.exclusions), "max_file_bytes": self.max_file_bytes,
                "max_files": self.max_files, "max_total_bytes": self.max_total_bytes}

    def reason(self, rel: str, *, directory=False) -> str | None:
        if not _rel_ok(rel):
            return "unsafe path"
        if any(rel == p or rel.startswith(p + "/") for p in self.exclusions):
            return "adapter-sensitive path"
        parts = PurePosixPath(rel).parts
        lowered = [part.lower() for part in parts]
        if ".tsmax" in lowered:
            return "map state"
        if any(part in GENERATED_DIRS or part.endswith(".egg-info") for part in lowered):
            return "generated/state directory"
        if any(part in SECRET_DIRS for part in lowered):
            return "secret directory"
        name = lowered[-1]
        # Classify by pathname before opening, including credentials with a text suffix.
        if (name in SECRET_NAMES or name == ".env" or name.startswith(".env.")
                or re.search(r"(^|[._-])(secret|secrets|credential|credentials|password|passwords|token|tokens)([._-]|$)", name)
                or PurePosixPath(name).suffix in SECRET_SUFFIXES):
            return "secret filename"
        if directory:
            return None
        if name in GENERATED_NAMES or name.endswith((".min.js", ".min.css", ".map", ".generated.ts", ".generated.js")):
            return "generated file"
        if name not in TEXT_NAMES and PurePosixPath(name).suffix not in TEXT_SUFFIXES:
            return "binary/unsupported extension"
        return None


def _identity(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _read(root_fd: int, rel: str, policy: Policy, *, state_file=False, max_bytes=None) -> tuple[bytes | None, str | None]:
    """No path component is followed; fstat verifies the checked file before/after reading."""
    reason = None if state_file and rel == INDEX else policy.reason(rel)
    if reason:
        return None, reason
    if not _rel_ok(rel):
        return None, "unsafe path"
    bound = max_bytes if max_bytes is not None else policy.max_file_bytes
    parent_fd = os.dup(root_fd)
    fd = None
    try:
        parts = PurePosixPath(rel).parts
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        before = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            return None, "symlink/nonregular/hardlink"
        if before.st_size > bound:
            return None, "oversized"
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=parent_fd)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or _identity(before) != _identity(opened):
            return None, "changed during read"
        chunks, length = [], 0
        while length <= bound:
            chunk = os.read(fd, min(65_536, bound + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
        if length > bound:
            return None, "oversized"
        if _identity(opened) != _identity(os.fstat(fd)):
            return None, "changed during read"
        current = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        if _identity(opened) != _identity(current) or not stat.S_ISREG(current.st_mode):
            return None, "changed during read"
        return b"".join(chunks), None
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable/symlink ancestor"
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


@lru_cache(maxsize=8192)
def _match_path(pattern: str, candidate: str) -> bool:
    """Segment-wise glob matching; ** spans zero or more whole directories.

    A small dynamic-programming table avoids regex backtracking. Other glob
    operators use stdlib fnmatch on one segment, where * cannot cross a slash.
    """
    patterns, parts = pattern.split("/"), candidate.split("/")
    if len(patterns) > 65 or len(parts) > 65:
        return False
    previous = [False] * (len(parts) + 1)
    previous[0] = True
    for segment in patterns:
        current = [False] * (len(parts) + 1)
        if segment == "**":
            current[0] = previous[0]
            for i in range(1, len(parts) + 1):
                current[i] = previous[i] or current[i - 1]
        else:
            for i, part in enumerate(parts, 1):
                current[i] = previous[i - 1] and fnmatch.fnmatchcase(part, segment)
        previous = current
    return previous[-1]


def _ignored(rel: str, directory: bool, rules: list[tuple[str, str]]) -> bool:
    ignored = False
    for base, raw in rules:
        negate = raw.startswith("!")
        pattern = raw[1:] if negate else raw
        dir_only = pattern.endswith("/")
        pattern = pattern.rstrip("/")
        if not pattern:
            continue
        local = rel[len(base) + 1:] if base and rel.startswith(base + "/") else rel if not base else ""
        if not local:
            continue
        anchored = pattern.startswith("/")
        pattern = pattern.lstrip("/")
        components = local.split("/")
        # A directory-only pattern may match ancestors, never the current
        # regular file merely because its relative path contains a slash.
        end = len(components) + (1 if directory or not dir_only else 0)
        candidates = ["/".join(components[:i]) for i in range(1, end)]
        if negate:
            # Conservatively require an explicit match on this entry to undo
            # an exclusion; an ancestor's negation cannot reinclude a file.
            candidates = [local] if directory or not dir_only else []
        if anchored or "/" in pattern:
            match = any(_match_path(pattern, candidate) for candidate in candidates)
        else:
            match = any(fnmatch.fnmatchcase(candidate.rsplit("/", 1)[-1], pattern) for candidate in candidates)
        if match:
            ignored = not negate
    return ignored


def _walk(root_fd: int, policy: Policy) -> tuple[list[str], Counter, bool]:
    """Non-Git fallback: bounded no-follow walk and nested .gitignore rules."""
    paths: list[str] = []
    skipped: Counter = Counter()
    count = 0
    limited = False

    def visit(fd, prefix, inherited):
        nonlocal count, limited
        if limited:
            return
        if len(PurePosixPath(prefix).parts) > 64:
            skipped["directory depth cap"] += 1
            return
        rules = list(inherited)
        ignore_rel = prefix + "/.gitignore" if prefix else ".gitignore"
        raw, ignore_reason = _read(root_fd, ignore_rel, policy, max_bytes=65_536)
        if ignore_reason not in (None, "missing"):
            skipped["unreadable ignore rules"] += 1
            return
        if raw:
            try:
                for line in raw.decode("utf-8").splitlines():
                    if not line or line.startswith("#"):
                        continue
                    pattern = line.removeprefix("!").strip("/")
                    # Bounded pathname globs; skip this subtree rather than
                    # misinterpret escaped or unsupported Git syntax.
                    if (line != line.strip() or not pattern or "\\" in line or len(pattern) > 4096 or len(pattern.split("/")) > 65
                            or "[" in re.sub(r"\[!?[-A-Za-z0-9_.]+\]", "", line)
                            or "]" in re.sub(r"\[!?[-A-Za-z0-9_.]+\]", "", line) or "\x00" in line):
                        skipped["unsupported ignore rules"] += 1
                        return
                    rules.append((prefix, line))
            except UnicodeDecodeError:
                skipped["unreadable ignore rules"] += 1
                return
        try:
            with os.scandir(fd) as scan:
                entries = []
                for entry in scan:
                    count += 1
                    if count > MAX_FILES * 5:
                        limited = True
                        skipped["entry cap"] += 1
                        break
                    entries.append(entry)
            entries.sort(key=lambda e: e.name)
            for entry in entries:
                rel = prefix + "/" + entry.name if prefix else entry.name
                is_dir = entry.is_dir(follow_symlinks=False)
                reason = policy.reason(rel, directory=is_dir)
                if reason:
                    if reason != "map state":
                        skipped[reason] += 1
                elif entry.is_symlink():
                    skipped["symlink/nonregular/hardlink"] += 1
                elif _ignored(rel, is_dir, rules):
                    skipped["ignored"] += 1
                elif is_dir:
                    child = None
                    try:
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        visit(child, rel, rules)
                    except OSError:
                        skipped["unreadable/symlink ancestor"] += 1
                    finally:
                        if child is not None:
                            os.close(child)
                else:
                    paths.append(rel)
        except OSError:
            skipped["unreadable directory"] += 1

    visit(root_fd, "", [])
    return sorted(paths), skipped, limited


def _enumerate(root: Path, root_fd: int, policy: Policy) -> tuple[list[str], Counter, bool, bool, list[str]]:
    # Do not launch Git: repository includes/excludesFile settings can cause Git
    # itself to read credential files outside the map's no-follow policy.
    paths, skipped, limited = _walk(root_fd, policy)
    if len(paths) > policy.max_files:
        skipped["file cap"] += len(paths) - policy.max_files
        paths = paths[:policy.max_files]
        limited = True
    return paths, skipped, limited, False, []


def _text(raw: bytes) -> str | None:
    if b"\0" in raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(ord(char) < 32 and char not in "\n\r\t\f" for char in text):
        return None
    return text


def _valid_text(value, limit):
    return isinstance(value, str) and len(value) <= limit


def _validate_meta(meta):
    if set(meta) != {"schema_version", "policy", "skipped", "limited", "git", "built_at"}:
        raise MapError("Map metadata is invalid.")
    policy = meta["policy"]
    if not isinstance(policy, dict) or set(policy) != {"exclusions", "max_file_bytes", "max_files", "max_total_bytes"}:
        raise MapError("Map policy is invalid.")
    exclusions = policy["exclusions"]
    if (not isinstance(exclusions, list) or len(exclusions) > 1024
            or any(not _valid_text(p, 4096) or not _rel_ok(p) for p in exclusions)):
        raise MapError("Map exclusion policy is invalid.")
    for key, bound in (("max_file_bytes", MAX_FILE_BYTES), ("max_files", MAX_FILES), ("max_total_bytes", MAX_TOTAL_BYTES)):
        if type(policy[key]) is not int or not 1 <= policy[key] <= bound:
            raise MapError("Map limits are invalid.")
    if not isinstance(meta["skipped"], dict) or len(meta["skipped"]) > 100 or any(not _valid_text(k, 100) or type(v) is not int or not 0 <= v <= 1_000_000 for k, v in meta["skipped"].items()):
        raise MapError("Map skip counts are invalid.")
    if type(meta["limited"]) is not bool or type(meta["git"]) is not bool or not _valid_text(meta["built_at"], 100):
        raise MapError("Map metadata fields are invalid.")


def _validate_source(row):
    if not isinstance(row, dict) or set(row) != {"size", "language", "approximate", "symbols", "imports", "summary", "limits", "text"}:
        raise MapError("Map source record is invalid.")
    if type(row["size"]) is not int or not 0 <= row["size"] <= MAX_FILE_BYTES or type(row["approximate"]) is not bool:
        raise MapError("Map source size/precision is invalid.")
    if not _valid_text(row["text"], 65_536) or not _valid_text(row["summary"], 160) or not _valid_text(row["language"], 30):
        raise MapError("Map source text is invalid.")
    if not isinstance(row["limits"], list) or len(row["limits"]) > 20 or any(not _valid_text(s, 300) for s in row["limits"]):
        raise MapError("Map source limits are invalid.")
    for key, bound in (("symbols", 2000), ("imports", 1000)):
        if not isinstance(row[key], list) or len(row[key]) > bound:
            raise MapError("Map declarations are invalid.")
        for item in row[key]:
            if not isinstance(item, dict) or set(item) != ({"name", "line", "kind"} if key == "symbols" else {"name", "line"}):
                raise MapError("Map declaration fields are invalid.")
            if not _valid_text(item["name"], 1000) or type(item["line"]) is not int or not 1 <= item["line"] <= MAX_FILE_BYTES + 1:
                raise MapError("Map declaration name/line is invalid.")
            if key == "symbols" and not _valid_text(item["kind"], 30):
                raise MapError("Map declaration kind is invalid.")


def _load(root_fd: int) -> tuple[dict, dict] | None:
    raw, reason = _read(root_fd, INDEX, Policy(), state_file=True, max_bytes=MAX_INDEX_BYTES)
    if reason == "missing":
        return None
    if reason:
        raise MapError(f"Map index cannot be read safely ({reason}).")
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(raw)
        connection.execute("PRAGMA trusted_schema=OFF")
        actual = dict(connection.execute("SELECT name, sql FROM sqlite_master WHERE type='table'"))
        if actual != _SCHEMA or connection.execute("SELECT count(*) FROM sqlite_master WHERE type IN ('view','trigger')").fetchone()[0]:
            raise MapError("Map schema is invalid; restore or remove the index explicitly.")
        meta = {key: json.loads(value) for key, value in connection.execute("SELECT key, value FROM map_meta")}
        if meta.get("schema_version") != SCHEMA_VERSION or not isinstance(meta.get("policy"), dict):
            raise MapError("Map schema version is unsupported; restore or remove the index explicitly.")
        _validate_meta(meta)
        files = {}
        for path, digest, payload in connection.execute("SELECT path, digest, payload FROM map_files ORDER BY path"):
            if len(files) >= MAX_FILES or not _rel_ok(path) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise MapError("Map file record is invalid.")
            data = json.loads(payload)
            _validate_source(data)
            files[path] = {"digest": digest, **data}
        return meta, files
    except (sqlite3.Error, ValueError, TypeError, KeyError, RecursionError) as exc:
        if isinstance(exc, MapError):
            raise
        raise MapError("Map index is corrupt; restore or remove the index explicitly.") from exc
    finally:
        connection.close()


def _serialize(meta: dict, files: dict) -> bytes:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in _SCHEMA.values():
            connection.execute(statement)
        connection.executemany("INSERT INTO map_meta VALUES (?, ?)", [(key, json.dumps(value, sort_keys=True)) for key, value in sorted(meta.items())])
        connection.executemany("INSERT INTO map_files VALUES (?, ?, ?)", [(path, row["digest"], json.dumps({key: value for key, value in row.items() if key != "digest"}, sort_keys=True)) for path, row in sorted(files.items())])
        connection.commit()
        raw = connection.serialize()
        if len(raw) > MAX_INDEX_BYTES:
            raise MapError("Map exceeds the index byte cap; narrow the project or exclusions.")
        return raw
    finally:
        connection.close()


def _policy(meta):
    try:
        return Policy(**meta["policy"])
    except (TypeError, ValueError) as exc:
        raise MapError("Map policy is invalid.") from exc


def _read_sources(root, root_fd, policy):
    paths, skipped, limited, git, hints = _enumerate(root, root_fd, policy)
    sources = {}
    total = 0
    for path in paths:
        if total >= policy.max_total_bytes:
            skipped["source byte cap"] += 1
            limited = True
            break
        bound = min(policy.max_file_bytes, policy.max_total_bytes - total)
        raw, reason = _read(root_fd, path, policy, max_bytes=bound)
        if reason:
            if reason == "oversized" and bound < policy.max_file_bytes:
                limited = True
                skipped["source byte cap"] += 1
            else:
                skipped[reason] += 1
            continue
        total += len(raw)
        if total > policy.max_total_bytes:
            skipped["source byte cap"] += 1
            limited = True
            break
        text = _text(raw)
        if text is None:
            skipped["binary/non-UTF8 content"] += 1
            continue
        sources[path] = (hashlib.sha256(raw).hexdigest(), text, len(raw))
    incomplete_reasons = {"unreadable ignore rules", "unsupported ignore rules", "unreadable directory",
                          "unreadable/symlink ancestor", "changed during read", "directory depth cap", "missing"}
    limited = limited or any(skipped[reason] for reason in incomplete_reasons)
    return sources, dict(sorted(skipped.items())), limited, git, hints


def _safe_write_paths(root: Path, home: Path):
    """Refuse symlink/non-directory ancestors before invoking the shared writer."""
    for base, rel in ((root, ".tsmax"), (home, ".tsmax"), (home, ".tsmax/backups"), (home, ".tsmax/manifests")):
        with _root_fd(base) as base_fd:
            current = os.dup(base_fd)
            try:
                for part in PurePosixPath(rel).parts:
                    try:
                        new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                    except FileNotFoundError:
                        break
                    except OSError as exc:
                        raise MapError("Map state/backup directory has an unsafe ancestor.") from exc
                    os.close(current)
                    current = new
            finally:
                os.close(current)


def build(root: Path, home: Path, *, excluded_paths: Iterable[Path] = (), max_file_bytes: int = MAX_FILE_BYTES,
          max_files: int = MAX_FILES, max_total_bytes: int = MAX_TOTAL_BYTES) -> dict:
    """Build the index with backups. Unchanged content performs no writes at all."""
    root, home = _absolute(root), _absolute(home)
    policy = Policy(_exclusions(root, [*current_exclusions(root, home), *excluded_paths]), max_file_bytes, max_files, max_total_bytes)
    with _root_fd(root) as root_fd:
        prior_bytes, prior_reason = _read(root_fd, INDEX, policy, state_file=True, max_bytes=MAX_INDEX_BYTES)
        preimage = hashlib.sha256(prior_bytes).hexdigest() if prior_reason is None else None
        previous = _load(root_fd)
        old_meta, old_files = previous or ({}, {})
        sources, skipped, limited, git, hints = _read_sources(root, root_fd, policy)
        files, changed, unchanged = {}, 0, 0
        for path, (digest, text, size) in sources.items():
            old = old_files.get(path)
            if old and old["digest"] == digest:
                files[path] = old
                unchanged += 1
            else:
                files[path] = {"digest": digest, "size": size, **extract(path, text)}
                _validate_source({key: value for key, value in files[path].items() if key != "digest"})
                changed += 1
        removed = len(old_files.keys() - files.keys())
        meta = {"schema_version": SCHEMA_VERSION, "policy": policy.json(), "skipped": skipped, "limited": limited, "git": git}
        stable_meta = {key: value for key, value in old_meta.items() if key != "built_at"}
        result = {"indexed": len(files), "changed": changed, "unchanged": unchanged, "removed": removed,
                  "skipped": skipped, "limited": limited, "git": git, "enumeration": "safe-walk", "git_changed_hints": hints,
                  "limits": _limits(files, policy, git), "manifest_path": None}
        if files == old_files and meta == stable_meta:
            result["written"] = False
            return result
        meta["built_at"] = datetime.now(timezone.utc).isoformat()
        raw = _serialize(meta, files)
        # Ensure the destination is still safe and unchanged since it was read.
        current = _load(root_fd)
        if current != previous:
            raise MapError("Map changed during build; rerun against the current map.")
        _safe_write_paths(root, home)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-map-" + uuid.uuid4().hex
        applied = apply_changes(Context(home=home, root=root), [{"path": str(root / INDEX), "kind": "modify" if previous else "create",
                                "content": raw, "private": True, "secure": True, "preimage": preimage, "reason": "Build local project map from eligible file hashes."}],
                                stamp=stamp, profile="map")
        result.update(written=True, manifest_path=applied["manifest_path"])
        return result


def _limits(files, policy, git):
    limits = {limit for row in files.values() for limit in row.get("limits", [])}
    limits.add(f"caps: {policy.max_files} files, {policy.max_file_bytes} bytes/file, {policy.max_total_bytes} source bytes, {MAX_INDEX_BYTES} index bytes")
    limits.add("query validates at most the best 1000 candidates and 8 MB of matched source; stale matches omitted")
    limits.add("UTF-8 text allowlist only; generated, secret, symlink and hardlinked paths excluded")
    if not git:
        limits.add("safe walk only: Git metadata/diff hints and global/info excludes are deferred; local .gitignore path globs (*, **, ?, classes) supported; unsupported escaped syntax skips subtree")
    return sorted(limits)


def status(root: Path, *, excluded_paths: Iterable[Path] = ()) -> dict:
    """Hash eligible sources and compare with the map; never writes or refreshes it."""
    root = _absolute(root)
    fresh_exclusions = _exclusions(root, [*current_exclusions(root), *excluded_paths])
    with _root_fd(root) as root_fd:
        saved = _load(root_fd)
        if saved is None:
            return {"exists": False, "fresh": False, "indexed": 0, "reason": "Run tsmax map build first."}
        meta, files = saved
        policy = _policy(meta)
        policy.exclusions = tuple(sorted(set(policy.exclusions) | set(fresh_exclusions)))
        sources, skipped, limited, git, _ = _read_sources(root, root_fd, policy)
        added = sorted(sources.keys() - files.keys())
        removed = sorted(files.keys() - sources.keys())
        changed = sorted(path for path in files.keys() & sources.keys() if files[path]["digest"] != sources[path][0])
        fresh = not (added or removed or changed) and meta.get("skipped") == skipped and meta.get("limited") == limited and meta.get("git") == git
        return {"exists": True, "fresh": fresh, "complete": not limited, "indexed": len(files), "added": added,
                "changed": changed, "removed": removed, "skipped": skipped, "limited": limited,
                "built_at": meta.get("built_at"), "git": git, "enumeration": "safe-walk", "limits": _limits(files, policy, git)}


_STOP_WORDS = {"where", "is", "are", "the", "a", "an", "find", "show", "me", "does", "do", "in", "of", "for", "to", "and", "with"}


def _words(text):
    # Split camelCase before case folding so "login" matches handleLogin.
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return set(re.findall(r"[\w]+", text.casefold().replace("_", " ")))


def _score(text, terms, exact, partial):
    words = _words(text)
    lowered = text.casefold()
    return sum(exact if term in words else partial if term in lowered else 0 for term in terms)


def _rank(item):
    return -item["score"], item["path"], item["line"], item["name"]


def query(root: Path, text: str, limit: int = 10, *, excluded_paths: Iterable[Path] = ()) -> list[dict]:
    """Rank at most ten current matches. Stale/deleted/now-excluded files are omitted."""
    if not isinstance(text, str) or len(text) > MAX_QUERY_CHARS:
        raise MapError(f"Query must be text of at most {MAX_QUERY_CHARS} characters.")
    try:
        limit = min(10, max(0, int(limit)))
    except (ValueError, TypeError) as exc:
        raise MapError("Query limit must be an integer.") from exc
    terms = sorted(_words(text) - _STOP_WORDS)[:32]
    if not terms or limit == 0:
        return []
    root = _absolute(root)
    fresh_exclusions = _exclusions(root, [*current_exclusions(root), *excluded_paths])
    with _root_fd(root) as root_fd:
        saved = _load(root_fd)
        if saved is None:
            raise MapError("No project map exists; run tsmax map build first.")
        meta, files = saved
        policy = _policy(meta)
        policy.exclusions = tuple(sorted(set(policy.exclusions) | set(fresh_exclusions)))
        paths, _, _, _, _ = _enumerate(root, root_fd, policy)
        eligible = set(paths)
        matches = []
        for path, row in files.items():
            if path not in eligible:
                continue
            path_score = _score(path, terms, 40, 15)
            summary_score = _score(row.get("summary", ""), terms, 12, 5)
            local = []
            for item in row["symbols"]:
                score = _score(item["name"], terms, 100, 50)
                if score:
                    local.append({"path": path, "line": item["line"], "kind": item["kind"], "name": one_line(item["name"]),
                                  "summary": one_line(row.get("summary", "")), "approximate": bool(row.get("approximate")),
                                  "score": score + path_score + summary_score})
            if not local:
                for item in row["imports"]:
                    score = _score(item["name"], terms, 30, 15)
                    if score:
                        local.append({"path": path, "line": item["line"], "kind": "import", "name": one_line(item["name"]),
                                      "summary": one_line(row.get("summary", "")), "approximate": bool(row.get("approximate")),
                                      "score": score + path_score + summary_score})
            if not local:
                content_score = _score(row["text"], terms, 5, 2)
                if path_score or summary_score or content_score:
                    line = next((number for number, value in enumerate(row["text"].splitlines(), 1) if any(term in value.casefold() for term in terms)), 1)
                    local.append({"path": path, "line": line, "kind": "file", "name": PurePosixPath(path).name,
                                  "summary": one_line(row.get("summary", "")), "approximate": bool(row.get("approximate")),
                                  "score": path_score + summary_score + content_score})
            matches.extend(local)
            if len(matches) > 2000:
                matches = sorted(matches, key=_rank)[:1000]
        # Only the best bounded candidates need current content checks.
        matches = sorted(matches, key=_rank)[:1000]
        valid: dict[str, bool] = {}
        result, read_bytes = [], 0
        for item in matches:
            path = item["path"]
            if path not in valid:
                if read_bytes >= 8_000_000:
                    raise MapError("Query validation byte cap reached; rebuild the map or narrow the query.")
                raw, reason = _read(root_fd, path, policy, max_bytes=min(policy.max_file_bytes, 8_000_000 - read_bytes))
                read_bytes += len(raw) if raw is not None else 0
                valid[path] = reason is None and hashlib.sha256(raw).hexdigest() == files[path]["digest"]
            if valid[path]:
                result.append(item)
                if len(result) == limit:
                    break
        return result
