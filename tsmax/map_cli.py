"""Project-map CLI and adapter-mediated, undoable integration installation."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

from .context import Context
from .adapters.map_support import map_context, excluded_paths


def add_parser(subparsers):
    parser = subparsers.add_parser("map", help="local project index and read-only stdio MCP")
    actions = parser.add_subparsers(dest="map_command", required=True)
    for name in ("build", "query", "status", "serve", "install"):
        item = actions.add_parser(name)
        item.add_argument("--root", type=Path, default=None, help="project directory (default: current directory)")
        if name in {"build", "install"}:
            item.add_argument("--home", type=Path, default=None, help="home for private backups and adapter exclusions")
        if name != "serve":
            item.add_argument("--json", action="store_true")
        if name == "query":
            item.add_argument("text")
            item.add_argument("--limit", type=int, default=10, choices=range(1, 11))
        if name == "install":
            item.add_argument("--tool", required=True, help="claude or claude-code; other integrations are not supported yet")
            item.add_argument("--apply", action="store_true", help="apply the displayed configuration change with backup and undo")


def server_command(root: Path) -> list[str]:
    # Isolate imports from the indexed project's cwd, PYTHONPATH and user site packages.
    # Works from a source checkout as well as an installed package, without downloading.
    root = Path(os.path.abspath(root))
    source = str(Path(__file__).resolve().parent.parent)
    bootstrap = f"import sys,runpy;sys.path.insert(0,{source!r});runpy.run_module('tsmax',run_name='__main__')"
    return [sys.executable, "-I", "-B", "-S", "-c", bootstrap, "map", "serve", "--root", str(root)]


def install(ctx: Context, tool: str, apply: bool = False) -> dict:
    from .adapters import REGISTRY
    from .adapters.map_support import config_bytes, safe_path
    from .installer import apply_changes
    adapter_id = {"claude": "claude-code"}.get(tool, tool)
    adapter = next((ad for ad in REGISTRY if ad.id == adapter_id), None)
    if adapter is None:
        raise ValueError(f"unknown map integration: {tool}")
    safe_path(ctx.root)
    safe_path(ctx.home)
    changes = adapter.plan_map_server(ctx, server_command(ctx.root))
    result = {"tool": adapter.id, "applied": False, "changes": [
        {k: change[k] for k in ("path", "kind", "key", "reason")} for change in changes],
        "command": server_command(ctx.root), "manifest_path": None}
    if apply and changes:
        for path in (ctx.home / ".tsmax", ctx.home / ".tsmax/backups", ctx.home / ".tsmax/manifests"):
            safe_path(path)
        for change in changes:
            raw = config_bytes(Path(change["path"]))
            current = hashlib.sha256(raw).hexdigest() if raw is not None else None
            if current != change["preimage"]:
                raise ValueError("configuration changed after planning; no map integration was applied")
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z") + f"-map-{uuid.uuid4().hex}"
        applied = apply_changes(ctx, changes, stamp=stamp, profile="map-integration")
        result.update(applied=True, manifest_path=applied["manifest_path"])
    return result


def result_lines(results: list[dict]) -> list[str]:
    def one_line(value, limit):
        text = "".join(char if char.isprintable() or char.isspace() else "?" for char in str(value))
        return " ".join(text.split())[:limit]
    return [f"{one_line(row['path'], 240)}:{int(row['line'])}: "
            f"{one_line(row.get('name') or row.get('summary') or '', 220)}"
            f"{' [approximate]' if row.get('approximate') else ''}"
            for row in results[:10]]


def run(args) -> int:
    from . import project_map
    root = Path(os.path.abspath(args.root or Path.cwd()))
    try:
        if args.map_command == "serve":
            from .map_mcp import serve
            return serve(root)
        if args.map_command == "build":
            ctx = map_context(root, Path(os.path.abspath(args.home or Path.home())))
            result = project_map.build(root, ctx.home, excluded_paths=excluded_paths(ctx))
        elif args.map_command == "query":
            result = project_map.query(root, args.text, limit=args.limit)
        elif args.map_command == "status":
            result = project_map.status(root)
        else:
            ctx = map_context(root, Path(os.path.abspath(args.home or Path.home())))
            result = install(ctx, args.tool, args.apply)
        if args.json:
            print(json.dumps(result, ensure_ascii=True))
        elif args.map_command == "query":
            print("\n".join(result_lines(result)) if result else "No current matches. Run `tsmax map build` to refresh the index.")
        elif args.map_command == "install":
            if not result["changes"]:
                print("Map integration is already configured.")
            else:
                print("Applied map integration." if result["applied"] else "Preview only; add --apply to install.")
                for change in result["changes"]:
                    print(f"  {change['path']}: {change['kind']} {change['key']} (read-only stdio)")
                print("Command: " + json.dumps(result["command"]))
                if result["manifest_path"]:
                    print(f"Undo: tsmax undo {json.dumps(result['manifest_path'])}")
        else:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (ValueError, OSError, NotImplementedError) as exc:
        # Never dump config contents or parser errors that could include embedded secrets.
        message = str(exc) if isinstance(exc, (ValueError, NotImplementedError)) else "map operation could not access its local files"
        if getattr(args, "json", False):
            print(json.dumps({"error": message}))
        else:
            print(message, file=sys.stderr)
        return 2
