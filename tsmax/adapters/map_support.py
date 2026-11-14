"""Bounded, no-follow configuration reads for map integration planners."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

MAX_CONFIG_BYTES = 1_000_000


def map_context(root: Path, home: Path):
    from . import REGISTRY
    from ..context import Context
    names = {name for adapter in REGISTRY for name in (*adapter.config_dir_env, *adapter.map_env_names)}
    env = {name: os.environ[name] for name in names if os.environ.get(name)}
    return Context(home=home, root=root, env=env)


def excluded_paths(ctx) -> list[Path]:
    from . import REGISTRY
    return [path for adapter in REGISTRY for path in adapter.map_excluded_paths(ctx)]


def current_exclusions(root: Path, home: Path | None = None) -> list[Path]:
    """Fresh path-only policy, safe for direct library callers as well as the CLI."""
    return excluded_paths(map_context(root, home or Path.home()))



def safe_path(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        try:
            mode = part.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError("map integration refuses symlink paths")
        if part != path and not stat.S_ISDIR(mode):
            raise ValueError("map integration requires directory ancestors")
    return path


def config_bytes(path: Path) -> bytes | None:
    from ..secure_files import read_bytes
    return read_bytes(safe_path(path), MAX_CONFIG_BYTES)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate configuration key")
        value[key] = item
    return value


def json_server_change(path: Path, name: str, server: dict) -> list[dict]:
    raw = config_bytes(path)
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non-finite JSON value"))) if raw is not None else {}
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("map integration requires valid JSON with unique keys") from exc
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise ValueError("map integration requires an object-valued mcpServers configuration")
    servers = data.setdefault("mcpServers", {})
    if name in servers:
        if servers[name] == server:
            return []
        raise ValueError("an existing tsmax-map server differs; review it before replacing it")
    servers[name] = server
    return [{"path": str(path), "content": json.dumps(data, indent=2, ensure_ascii=False) + "\n",
             "private": True, "secure": True, "kind": "modify" if raw is not None else "create",
             "key": f"mcpServers.{name}", "reason": "read-only local project map over stdio",
             "preimage": hashlib.sha256(raw).hexdigest() if raw is not None else None}]
