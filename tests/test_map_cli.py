"""Project-map CLI and adapter installation: isolated projects and undo."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tsmax.cli import main
from tsmax.context import Context
from tsmax.installer import undo_manifest
from tsmax.map_cli import excluded_paths, install, map_context, result_lines, server_command


def world(tmp_path):
    root, home = tmp_path / "project", tmp_path / "home"
    root.mkdir()
    home.mkdir()
    return Context(home=home, root=root)


def test_cli_build_query_status_and_limit(tmp_path, capsys):
    ctx = world(tmp_path)
    (ctx.root / "login.py").write_text("\n".join(f"def login_{i}():\n    return True" for i in range(20)))
    assert main(["map", "status", "--root", str(ctx.root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["exists"] is False
    assert main(["map", "build", "--root", str(ctx.root), "--home", str(ctx.home), "--json"]) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["indexed"] == 1 and built["manifest_path"]
    assert main(["map", "query", "where is login", "--root", str(ctx.root)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert 1 <= len(lines) <= 10
    assert all(line.startswith("login.py:") for line in lines)
    assert main(["map", "query", "login", "--root", str(ctx.root), "--limit", "2", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) <= 2


def test_build_exclusions_never_call_unsafe_adapter_discovery(tmp_path, monkeypatch):
    from tsmax.adapters import REGISTRY
    ctx = world(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("map must not call discovery that reads instructions or config")
    for adapter in REGISTRY:
        monkeypatch.setattr(adapter, "config_locations", forbidden)
        monkeypatch.setattr(adapter, "read_config", forbidden)
    redirected = ctx.root / "relocated-login"
    monkeypatch.setenv("CODEX_HOME", str(redirected))
    safe_ctx = map_context(ctx.root, ctx.home)
    paths = excluded_paths(safe_ctx)
    assert redirected in paths
    assert ctx.home / ".claude" in paths and ctx.root / ".mcp.json" in paths
    assert "OPENAI_API_KEY" not in safe_ctx.env


def test_relative_provider_relocation_is_project_relative(tmp_path, monkeypatch):
    ctx = world(tmp_path)
    monkeypatch.setenv("CODEX_HOME", "private-provider")
    paths = excluded_paths(map_context(ctx.root, ctx.home))
    assert ctx.root / "private-provider" in paths


def test_result_lines_mark_approximate_and_escape_control_characters():
    lines = result_lines([{"path": "src/login.ts", "line": 7, "name": "login\n\x1b[31m", "approximate": True}])
    assert lines[0].startswith("src/login.ts:7:")
    assert lines[0].endswith("[approximate]")
    assert "\n" not in lines[0] and "\x1b" not in lines[0]


def test_install_preview_apply_idempotence_undo_preserve_other_servers(tmp_path):
    ctx = world(tmp_path)
    config = ctx.root / ".mcp.json"
    original = b'{"mcpServers":{"existing":{"command":"existing-tool","env":{"EXAMPLE":"keep"}}},"other":true}\n'
    config.write_bytes(original)
    preview = install(ctx, "claude")
    assert preview["changes"] and not preview["applied"]
    assert config.read_bytes() == original and not (ctx.home / ".tsmax").exists()
    result = install(ctx, "claude", True)
    assert result["applied"]
    data = json.loads(config.read_text())
    assert data["other"] and data["mcpServers"]["existing"]["env"] == {"EXAMPLE": "keep"}
    server = data["mcpServers"]["tsmax-map"]
    assert server["type"] == "stdio" and server["args"][-2:] == ["--root", str(ctx.root)]
    assert config.stat().st_mode & 0o777 == 0o600
    assert install(ctx, "claude", True)["changes"] == []
    undo_manifest(Path(result["manifest_path"]))
    assert config.read_bytes() == original


def test_install_created_config_undo_and_later_user_edits(tmp_path):
    ctx = world(tmp_path)
    result = install(ctx, "claude", True)
    config = ctx.root / ".mcp.json"
    data = json.loads(config.read_text())
    data["mcpServers"]["added-later"] = {"command": "other"}
    config.write_text(json.dumps(data))
    undo_manifest(Path(result["manifest_path"]))
    assert json.loads(config.read_text()) == {"mcpServers": {"added-later": {"command": "other"}}}


@pytest.mark.parametrize("content", ["[]", '{"mcpServers":[]}', '{"mcpServers":{},"mcpServers":{}}', '{"bad":', '{"bad":NaN}'])
def test_install_refuses_malformed_config_without_changes(tmp_path, content):
    ctx = world(tmp_path)
    config = ctx.root / ".mcp.json"
    config.write_text(content)
    with pytest.raises(ValueError):
        install(ctx, "claude", True)
    assert config.read_text() == content and not (ctx.home / ".tsmax").exists()


def test_install_refuses_conflicting_server_and_unsupported_tool(tmp_path):
    ctx = world(tmp_path)
    (ctx.root / ".mcp.json").write_text('{"mcpServers":{"tsmax-map":{"command":"foreign"}}}')
    with pytest.raises(ValueError, match="differs"):
        install(ctx, "claude", True)
    with pytest.raises(NotImplementedError, match="not supported"):
        install(ctx, "codex", True)


def test_install_refuses_symlinks_and_hardlinks_without_reading_target(tmp_path):
    ctx = world(tmp_path)
    target = tmp_path / "secret"
    target.write_text("must never be parsed")
    config = ctx.root / ".mcp.json"
    config.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        install(ctx, "claude", True)
    config.unlink()
    os.link(target, config)
    with pytest.raises(OSError, match="without links"):
        install(ctx, "claude", True)
    assert target.read_text() == "must never be parsed"


def test_config_open_rejects_ancestor_swapped_to_symlink(tmp_path, monkeypatch):
    from tsmax.adapters import map_support
    ctx = world(tmp_path)
    target = tmp_path / "outside"
    target.mkdir()
    (target / ".mcp.json").write_text("must never be opened")
    original_safe = map_support.safe_path
    def swap(path):
        result = original_safe(path)
        ctx.root.rename(tmp_path / "old-project")
        ctx.root.symlink_to(target, target_is_directory=True)
        return result
    monkeypatch.setattr(map_support, "safe_path", swap)
    with pytest.raises(OSError):
        map_support.config_bytes(ctx.root / ".mcp.json")


def test_installed_server_command_works_from_other_project_without_import_shadowing(tmp_path):
    ctx = world(tmp_path)
    shadow = ctx.root / "tsmax"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("raise RuntimeError('project import was executed')")
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
    proc = subprocess.run(server_command(ctx.root), input=json.dumps(request) + "\n", text=True,
                          cwd=ctx.root, capture_output=True, timeout=10,
                          env={**os.environ, "PYTHONPATH": str(ctx.root)})
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["result"]["serverInfo"]["name"] == "tsmax-map"
    assert not (ctx.root / ".tsmax").exists()
