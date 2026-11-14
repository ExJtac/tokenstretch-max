"""MAX state, backups and hook ownership must stay separate from upstream."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tsmax.cli import main
from tsmax.context import build_context
from tsmax.installer import apply_changes, latest_manifest, undo_manifest
from tsmax.undo_keys import revert_own_entries
from test_install_script import _git_args, _run, release


def _context(tmp_path):
    home, root, snapshot = (tmp_path / name for name in ("home", "project", "sys"))
    for path in (home, root, snapshot):
        path.mkdir()
    return build_context(home, root, snapshot)


def test_apply_and_undo_keep_upstream_state_untouched(tmp_path):
    ctx = _context(tmp_path)
    old = ctx.home / ".tokenstretch"
    (old / "manifests").mkdir(parents=True)
    sentinel = old / "manifests" / "9999-old.json"
    sentinel.write_text('{"product":"tokenstretch","files":[]}\n')
    upstream_bytes = sentinel.read_bytes()
    target = ctx.root / "notes.txt"
    target.write_bytes(b"original\r\n")
    assert latest_manifest(ctx) is None

    applied = apply_changes(ctx, [{"path": str(target), "content": "new\n"}], stamp="test")
    manifest = Path(applied["manifest_path"])
    assert manifest.parent == ctx.home / ".tsmax" / "manifests"
    assert Path(applied["backup_dir"]).is_relative_to(ctx.home / ".tsmax" / "backups")
    assert latest_manifest(ctx) == manifest
    assert target.read_bytes() == b"new\n"
    undo_manifest(manifest)
    assert target.read_bytes() == b"original\r\n"
    assert sentinel.read_bytes() == upstream_bytes
    assert set(old.rglob("*")) == {old / "manifests", sentinel}


def test_undo_without_max_manifest_does_not_use_upstream_manifest(tmp_path, capsys):
    ctx = _context(tmp_path)
    old = ctx.home / ".tokenstretch" / "manifests"
    old.mkdir(parents=True)
    sentinel = old / "latest.json"
    sentinel.write_text("not a MAX manifest")
    code = main(["undo", "--home", str(ctx.home), "--root", str(ctx.root),
                 "--sys-snapshot", str(tmp_path / "sys")])
    assert code == 1
    assert ".tsmax/manifests" in capsys.readouterr().err
    assert sentinel.read_text() == "not a MAX manifest"


@pytest.mark.parametrize("upstream_command", [
    "tokenstretch nudge", "/opt/bin/tokenstretch auto", "python -m tokenstretch statusline",
])
def test_undo_preserves_later_upstream_hooks(upstream_command):
    old_hook = {"type": "command", "command": upstream_command}
    max_hook = {"type": "command", "command": "tsmax nudge"}
    before = {"theme": "dark"}
    after = {**before, "hooks": {"Stop": [{"hooks": [max_hook]}]}}
    current = {**before, "hooks": {"Stop": [{"hooks": [max_hook, old_hook]}]}}
    expected = {**before, "hooks": {"Stop": [{"hooks": [old_hook]}]}}
    for saved_after in (after, None):
        assert revert_own_entries(current, before, saved_after) == expected


def test_update_cannot_launch_downloader(monkeypatch):
    from tsmax import update

    def forbidden(*args, **kwargs):
        pytest.fail("MAX update attempted to launch a network command")

    monkeypatch.setattr(update.subprocess, "run", forbidden)
    with pytest.raises(update.UpdateError):
        update.fetch("https://example.invalid/release")


@pytest.mark.parametrize("apply", [False, True])
def test_proxy_cli_refuses_before_planning_or_writing(tmp_path, monkeypatch, capsys, apply):
    import tsmax.installer as installer

    ctx = _context(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("MAX proxy entry point reached the installer")

    monkeypatch.setattr(installer, "install", forbidden)
    argv = ["install", "--profile", "api-proxy", "--proxy", "headroom",
            "--home", str(ctx.home), "--root", str(ctx.root), "--json"]
    if apply:
        argv.append("--apply")
    assert main(argv) != 0
    assert "disabled" in json.dumps(json.loads(capsys.readouterr().out)).lower()
    assert not (ctx.home / ".tsmax").exists()


def test_install_and_uninstall_leave_upstream_installation_intact(tmp_path, release):
    home = tmp_path / "home"
    old_files = {
        home / ".local/bin/tokenstretch": b"#!/bin/sh\nprintf 'original tool\\n'\n",
        home / ".local/share/tokenstretch/install-manifest.txt": b"upstream install\n",
        home / ".tokenstretch/manifests/old.json": b'{"product":"tokenstretch"}\n',
        home / ".tokenstretch/backups/old.txt": b"upstream backup\n",
    }
    for path, content in old_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    old_launcher = home / ".local/bin/tokenstretch"
    old_launcher.chmod(0o755)
    old_mode = old_launcher.stat().st_mode
    result = _run(home, *_git_args(release))
    assert result.returncode == 0, result.stderr
    assert (home / ".local/bin/tsmax").is_file()
    assert (home / ".local/share/tsmax/src/tsmax/__init__.py").is_file()
    for path, content in old_files.items():
        assert path.read_bytes() == content
    result = _run(home, "--uninstall")
    assert result.returncode == 0, result.stderr
    assert not (home / ".local/bin/tsmax").exists()
    assert not (home / ".local/share/tsmax").exists()
    for path, content in old_files.items():
        assert path.read_bytes() == content
    assert old_launcher.stat().st_mode == old_mode


def test_setup_and_undo_preserve_upstream_shortcuts_and_handoff(tmp_path):
    from tsmax.setup import run_setup

    ctx = _context(tmp_path)
    settings = ctx.home / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text('{"model":"sonnet"}\n')
    (ctx.root / "CLAUDE.md").write_text("# Project\n")
    old_files = {}
    for name in ("brief", "credits", "handoff"):
        path = ctx.home / ".claude/skills" / name / "SKILL.md"
        old_files[path] = f"---\nname: {name}\n---\n<!-- added by tokenstretch -->\nOriginal {name}\n".encode()
    for name in ("handoff.md", "handoff.prev.md", "handoff-auto.md", "handoff-auto.prev.md"):
        old_files[ctx.root / ".claude" / name] = f"Upstream note: {name}\n".encode()
    for path, content in old_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    result = run_setup(ctx, yes=True, installs=False, out=lambda line: None)
    assert result.manifests
    for name in ("brief", "credits", "handoff"):
        skill = ctx.home / ".claude/skills" / f"tsmax-{name}" / "SKILL.md"
        assert skill.is_file()
        assert f"name: tsmax-{name}" in skill.read_text()
    for path, content in old_files.items():
        assert path.read_bytes() == content
    for manifest in reversed(result.manifests):
        undo_manifest(Path(manifest))
    for name in ("brief", "credits", "handoff"):
        assert not (ctx.home / ".claude/skills" / f"tsmax-{name}" / "SKILL.md").exists()
    for path, content in old_files.items():
        assert path.read_bytes() == content


def test_local_source_install_honors_explicit_commit(tmp_path, release):
    home = tmp_path / "home"
    home.mkdir()
    source = str(release["repo"])
    refused = _run(home, "--local-source", source, "--commit", "0" * 40)
    assert refused.returncode != 0
    assert "commit" in (refused.stdout + refused.stderr).lower()
    assert not (home / ".local/bin/tsmax").exists()
    assert not (home / ".local/share/tsmax").exists()
    installed = _run(home, "--local-source", source, "--commit", release["commit"])
    assert installed.returncode == 0, installed.stderr
    assert (home / ".local/bin/tsmax").is_file()
    manifest = (home / ".local/share/tsmax/install-manifest.txt").read_text()
    assert f"commit:{release['commit']}" in manifest
