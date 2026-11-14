"""Project-map extraction, safety, incremental freshness, and undo contracts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest

from tsmax import project_map as pm
from tsmax.context import Context
from tsmax.installer import undo_manifest
from tsmax.map_extract import extract


@pytest.fixture
def world(tmp_path):
    root, home = tmp_path / "project", tmp_path / "home"
    root.mkdir()
    home.mkdir()
    return root, home


def write(root, path, text):
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def git(root, *args):
    return subprocess.run(["git", "-c", "user.name=Map Test", "-c", "user.email=map@example.invalid", "-c", "commit.gpgsign=false", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def snapshot(*roots):
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for root in roots for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


def test_python_symbols_imports_and_correct_lines(world):
    root, home = world
    write(root, "accounts.py", '"""Account authentication helpers."""\nimport json\nfrom .users import User\nclass Accounts:\n    async def login(self):\n        def validate():\n            return True\n        return validate()\n')
    result = pm.build(root, home)
    assert result["indexed"] == result["changed"] == 1
    matches = pm.query(root, "where is login")
    assert matches[0]["path"] == "accounts.py" and matches[0]["line"] == 5
    assert matches[0]["name"] == "Accounts.login" and not matches[0]["approximate"]
    assert pm.query(root, "validate")[0]["name"] == "Accounts.login.validate"
    imports = pm.query(root, "users")
    assert imports[0]["kind"] == "import" and imports[0]["line"] == 3
    assert pm.status(root)["fresh"]


def test_useful_typescript_and_javascript_without_comment_or_string_symbols(world):
    root, home = world
    source = '''// function fakeLogin() {}
import { User } from "./users";
export interface LoginRequest { user: string }
export type LoginResult = Promise<User>;
export class AuthService {}
export async function login(req: LoginRequest) { return req; }
export const logout = async (id: string): Promise<void> => { return; };
const message = "function fabricated() {}";
const template = `class Pretend {}`;
const pattern = /function phantom/;
'''
    write(root, "auth.ts", source)
    write(root, "client.jsx", 'const React = require("react");\nexport const LoginForm = () => <form />;\n')
    pm.build(root, home)
    exact = extract("auth.ts", source)
    names = {item["name"] for item in exact["symbols"]}
    assert {"LoginRequest", "LoginResult", "AuthService", "login", "logout"} <= names
    assert not {"fakeLogin", "fabricated", "Pretend", "phantom"} & names
    assert exact["imports"] == [{"name": "./users", "line": 2}]
    match = next(item for item in pm.query(root, "login") if item["name"] == "login")
    assert match["line"] == 6 and match["approximate"] is True
    assert pm.query(root, "logout")[0]["line"] == 7
    assert pm.query(root, "LoginForm")[0]["path"] == "client.jsx"
    assert any("JS/TS lexical" in value for value in pm.status(root)["limits"])


def test_noop_build_query_and_status_do_not_write_or_reextract(world, monkeypatch):
    root, home = world
    write(root, "app.py", "def login():\n    return True\n")
    first = pm.build(root, home)
    before = snapshot(root, home)
    monkeypatch.setattr(pm, "extract", lambda *_: pytest.fail("unchanged source was re-extracted"))
    second = pm.build(root, home)
    assert second["written"] is False and second["manifest_path"] is None
    assert second["changed"] == 0 and second["unchanged"] == 1
    assert pm.query(root, "login") and pm.status(root)["fresh"]
    assert snapshot(root, home) == before
    assert first["manifest_path"]


def test_hashes_detect_same_size_same_mtime_changes_and_only_reextract_changed(world, monkeypatch):
    root, home = world
    path = write(root, "app.py", "def login(): return 1\n")
    write(root, "other.py", "def stable(): return 0\n")
    pm.build(root, home)
    old_time = path.stat().st_mtime_ns
    path.write_text("def login(): return 2\n")
    os.utime(path, ns=(old_time, old_time))
    assert pm.status(root)["changed"] == ["app.py"]
    assert pm.query(root, "login") == []  # stale line/summary must not escape
    calls = []
    original = pm.extract
    monkeypatch.setattr(pm, "extract", lambda path, text: (calls.append(path), original(path, text))[1])
    result = pm.build(root, home)
    assert calls == ["app.py"] and result["changed"] == 1 and result["unchanged"] == 1
    assert pm.status(root)["fresh"] and pm.query(root, "login")


def test_git_project_sources_ignores_rename_and_deletion_without_git_execution(world, monkeypatch):
    root, home = world
    git(root, "init", "-q")
    write(root, ".gitignore", "ignored/\n*.skip.py\n")
    write(root, "tracked.py", "def login(): pass\n")
    write(root, "ignored/private.py", "def ignored_secret(): pass\n")
    git(root, "add", ".gitignore", "tracked.py")
    git(root, "commit", "-qm", "base")
    write(root, "untracked.py", "def fresh(): pass\n")
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_kw: pytest.fail("map launched a subprocess"))
    pm.build(root, home)
    assert pm.query(root, "fresh") and all(item["path"] != "ignored/private.py" for item in pm.query(root, "ignored_secret"))
    (root / "tracked.py").rename(root / "renamed.py")
    (root / "untracked.py").unlink()
    result = pm.build(root, home)
    assert result["removed"] == 2 and result["changed"] == 1
    assert pm.query(root, "login")[0]["path"] == "renamed.py"
    assert result["git_changed_hints"] == [] and result["enumeration"] == "safe-walk"


def test_nongit_nested_ignore_rules_and_negation(world):
    root, home = world
    write(root, ".gitignore", "ignored/\n*.tmp.py\n!keep.tmp.py\n")
    write(root, "ignored/private.py", "def hidden(): pass\n")
    write(root, "drop.tmp.py", "def dropped(): pass\n")
    write(root, "keep.tmp.py", "def kept(): pass\n")
    write(root, "src/.gitignore", "local.py\n")
    write(root, "src/local.py", "def localhidden(): pass\n")
    pm.build(root, home)
    assert pm.query(root, "kept")
    assert all(not pm.query(root, term) for term in ("hidden", "dropped", "localhidden"))


def test_secret_generated_binary_and_oversized_names_rejected_before_content_open(world, monkeypatch):
    root, home = world
    for name in (".env", ".env.example", "auth.json", "credentials.json", "secrets/key.py", "node_modules/lib/index.js", "image.png", "huge.py"):
        write(root, name, "CANARY_DO_NOT_READ" + ("x" * 100 if name == "huge.py" else ""))
    write(root, "app.py", "def login(): pass\n")
    forbidden = {".env", ".env.example", "auth.json", "credentials.json", "secrets", "node_modules", "image.png", "huge.py"}
    original = os.open
    def guarded(path, flags, *args, **kwargs):
        assert Path(path).name not in forbidden, f"opened forbidden path {path}"
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", guarded)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {guarded})
    result = pm.build(root, home, max_file_bytes=64)
    assert result["indexed"] == 1 and pm.query(root, "login")
    assert pm.status(root)["fresh"]


def test_adapter_exclusions_and_new_relocation_apply_before_query_or_status_reads(world, monkeypatch):
    root, home = world
    custom = root / "custom-provider"
    write(root, "custom-provider/settings.json", '{"description":"unicorn login"}')
    write(root, "app.py", "def login(): pass\n")
    pm.build(root, home)
    assert any(item["path"].startswith("custom-provider/") for item in pm.query(root, "unicorn"))
    monkeypatch.setenv("CODEX_HOME", str(custom))
    original = os.open
    def guarded(path, flags, *args, **kwargs):
        assert Path(path).name not in {"custom-provider", "settings.json"}
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", guarded)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {guarded})
    assert pm.query(root, "unicorn") == []
    assert "custom-provider/settings.json" in pm.status(root)["removed"]
    assert pm.build(root, home)["removed"] == 1


def test_excluded_ancestor_and_secret_project_roots_are_refused(world, monkeypatch):
    root, home = world
    provider = root / "provider"
    project = provider / "project"
    project.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(provider))
    with pytest.raises(pm.MapError, match="sensitive"):
        pm.build(project, home)
    secret = root / "secrets" / "project"
    secret.mkdir(parents=True)
    with pytest.raises(pm.MapError, match="secret"):
        pm.build(secret, home)


def test_symlink_files_directories_and_root_ancestors_are_never_followed(world):
    root, home = world
    outside = write(home, "private.py", "def forbidden(): pass\n")
    (root / "linked.py").symlink_to(outside)
    (root / "linked-dir").symlink_to(home, target_is_directory=True)
    write(root, "app.py", "def login(): pass\n")
    assert pm.build(root, home)["indexed"] == 1
    assert pm.query(root, "forbidden") == []
    alias = home / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(pm.MapError, match="symlink"):
        pm.status(alias)


def test_symlink_swap_after_stat_cannot_read_target(world, monkeypatch):
    root, home = world
    path = write(root, "app.py", "def login(): pass\n")
    target = write(home, "private.py", "def secret_canary(): pass\n")
    original = os.open
    swapped = False
    def swap(path_arg, flags, *args, **kwargs):
        nonlocal swapped
        if str(path_arg) == "app.py" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            path.unlink()
            path.symlink_to(target)
        return original(path_arg, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", swap)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {swap})
    result = pm.build(root, home)
    assert swapped and result["indexed"] == 0
    assert pm.query(root, "secret_canary") == []


def test_hardlinks_and_fifo_are_not_opened(world, monkeypatch):
    root, home = world
    original = write(home, "private.py", "canary")
    os.link(original, root / "alias.py")
    os.mkfifo(root / "pipe.py")
    real_open = os.open
    def guarded(path, flags, *args, **kwargs):
        assert Path(path).name not in {"alias.py", "pipe.py"}
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", guarded)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {guarded})
    assert pm.build(root, home)["indexed"] == 0


def test_query_omits_removed_newly_ignored_or_symlinked_matches(world):
    root, home = world
    write(root, "one.py", "def login_one(): pass\n")
    two = write(root, "two.py", "def login_two(): pass\n")
    three = write(root, "three.py", "def login_three(): pass\n")
    pm.build(root, home)
    two.unlink()
    three.unlink()
    three.symlink_to(root / "one.py")
    write(root, ".gitignore", "one.py\n")
    assert pm.query(root, "login") == []
    assert set(pm.status(root)["removed"]) == {"one.py", "two.py", "three.py"}


def test_map_first_build_and_rebuild_undo_restore_exact_bytes(world):
    root, home = world
    write(root, "app.py", "def login(): return 1\n")
    first = pm.build(root, home)
    original = (root / pm.INDEX).read_bytes()
    write(root, "app.py", "def login(): return 2\n")
    second = pm.build(root, home)
    assert first["manifest_path"] != second["manifest_path"]
    assert (root / pm.INDEX).stat().st_mode & 0o777 == 0o600
    manifest = json.loads(Path(second["manifest_path"]).read_text())
    assert manifest["profile"] == "map" and manifest["files"][0]["secure"]
    undo_manifest(Path(second["manifest_path"]))
    assert (root / pm.INDEX).read_bytes() == original
    undo_manifest(Path(first["manifest_path"]))
    assert not (root / pm.INDEX).exists()


def test_failed_extraction_preserves_previous_index_and_creates_no_manifest(world, monkeypatch):
    root, home = world
    write(root, "app.py", "def login(): return 1\n")
    pm.build(root, home)
    before = snapshot(home)
    old = (root / pm.INDEX).read_bytes()
    write(root, "app.py", "def changed(): return 2\n")
    def fail(*_):
        raise RuntimeError("test extraction failure")
    monkeypatch.setattr(pm, "extract", fail)
    with pytest.raises(RuntimeError, match="extraction"):
        pm.build(root, home)
    assert (root / pm.INDEX).read_bytes() == old and snapshot(home) == before


def test_state_and_backup_symlinks_are_refused_without_writing_targets(world):
    root, home = world
    write(root, "app.py", "def login(): pass\n")
    outside = home / "outside"
    outside.mkdir()
    (root / ".tsmax").symlink_to(outside, target_is_directory=True)
    with pytest.raises(pm.MapError):
        pm.build(root, home)
    assert list(outside.iterdir()) == []
    (root / ".tsmax").unlink()
    (home / ".tsmax").mkdir()
    (home / ".tsmax/backups").symlink_to(outside, target_is_directory=True)
    with pytest.raises(pm.MapError):
        pm.build(root, home)
    assert list(outside.iterdir()) == [] and not (root / pm.INDEX).exists()


def test_file_source_and_index_caps_are_explicit_and_preserve_existing_index(world, monkeypatch):
    root, home = world
    for name in ("a.py", "b.py", "c.py"):
        write(root, name, "def login(): pass\n")
    result = pm.build(root, home, max_files=2)
    assert result["indexed"] == 2 and result["limited"]
    assert pm.status(root)["complete"] is False
    old = (root / pm.INDEX).read_bytes()
    monkeypatch.setattr(pm, "_serialize", lambda *_: (_ for _ in ()).throw(pm.MapError("index cap")))
    with pytest.raises(pm.MapError, match="index cap"):
        pm.build(root, home, max_total_bytes=20)
    assert (root / pm.INDEX).read_bytes() == old


def test_invalid_python_and_other_text_have_honest_fallback(world):
    root, home = world
    write(root, "broken.py", "def login(:\n    pass\n")
    write(root, "guide.md", "# Login workflow\nUseful local instructions.\n")
    (root / "binary.py").write_bytes(b"hello\0canary")
    (root / "nonutf.py").write_bytes(b"\xff\xfe")
    result = pm.build(root, home)
    assert result["indexed"] == 2
    found = pm.query(root, "login")
    assert found and all(item["approximate"] for item in found)
    assert any("parse failed" in limit for limit in result["limits"])


def test_query_is_bounded_deterministic_and_single_line(world):
    root, home = world
    for i in range(15):
        write(root, f"auth{i:02}.py", '"""Login \\x1b[31m helpers.\\nNo extra line."""\ndef login(): pass\n')
    write(root, "bad\nname.py", "def login(): pass\n")
    pm.build(root, home)
    found = pm.query(root, "where is login", limit=100)
    assert len(found) == 10 and found == pm.query(root, "where is login", limit=100)
    assert [item["path"] for item in found] == [f"auth{i:02}.py" for i in range(10)]
    assert all("\n" not in item["summary"] and "\x1b" not in item["summary"] for item in found)
    assert pm.query(root, "unknownsymbol") == [] and pm.query(root, "where is the") == []
    with pytest.raises(pm.MapError, match="512"):
        pm.query(root, "x" * 513)


def test_missing_index_and_corrupt_payload_fail_without_implicit_rebuild(world):
    root, home = world
    assert pm.status(root)["exists"] is False
    with pytest.raises(pm.MapError, match="No project map"):
        pm.query(root, "login")
    assert not (root / ".tsmax").exists()
    write(root, "app.py", "def login(): pass\n")
    pm.build(root, home)
    with sqlite3.connect(root / pm.INDEX) as db:
        payload = json.loads(db.execute("SELECT payload FROM map_files").fetchone()[0])
        payload["symbols"] = [{}]
        db.execute("UPDATE map_files SET payload=?", (json.dumps(payload),))
    before = snapshot(root, home)
    for fn in (pm.status, lambda root: pm.query(root, "login")):
        with pytest.raises(pm.MapError, match="declaration"):
            fn(root)
    assert snapshot(root, home) == before


def test_unsupported_ignore_syntax_skips_subtree_and_never_reads_git_metadata(world, monkeypatch):
    root, home = world
    write(root, ".gitignore", "private\\ file.py\n")
    write(root, "app.py", "def login(): pass\n")
    write(root, ".git/config", "[include]\npath = ../.env\n")
    write(root, ".env", "DO_NOT_READ")
    original = os.open
    def guarded(path, flags, *args, **kwargs):
        assert Path(path).name not in {".git", "config", ".env", "app.py"}
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", guarded)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {guarded})
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_kw: pytest.fail("map launched Git"))
    result = pm.build(root, home)
    assert result["indexed"] == 0 and result["skipped"]["unsupported ignore rules"] == 1
    assert pm.status(root)["complete"] is False


def test_long_valid_identifier_is_bounded_and_never_corrupts_index(world):
    root, home = world
    name = "login" + "x" * 1100
    write(root, "long.py", f"def {name}(): pass\n")
    result = pm.build(root, home)
    assert pm.status(root)["fresh"]
    assert pm.query(root, "login")[0]["approximate"]
    assert "symbol/import names truncated" in result["limits"]


def test_common_basename_ignore_character_classes_are_supported(world):
    root, home = world
    write(root, ".gitignore", "*.py[cod]\n")
    write(root, "app.py", "def login(): pass\n")
    result = pm.build(root, home)
    assert pm.query(root, "login") and not result["limited"]


def test_directory_only_negation_does_not_reinclude_a_regular_file(world, monkeypatch):
    root, home = world
    write(root, ".gitignore", "*.json\n!/a/config.json/\n")
    write(root, "a/config.json", '{"private":"must stay ignored"}')
    original = os.open
    def guarded(path, flags, *args, **kwargs):
        assert Path(path).name != "config.json"
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", guarded)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {guarded})
    pm.build(root, home)
    assert all(row["path"] != "a/config.json" for row in pm.query(root, "private"))


def test_globstar_matches_zero_or_many_directories_without_hiding_other_source(world):
    root, home = world
    write(root, ".gitignore", "src/**/private.py\n**/generated/*.js\nassets/**/*.json\n")
    for name in ("src/private.py", "src/a/b/private.py", "generated/a.js", "src/generated/b.js", "assets/a.json", "assets/deep/b.json"):
        write(root, name, "must not index")
    write(root, "src/login.ts", "export function login() { return true; }\n")
    result = pm.build(root, home)
    assert result["indexed"] == 2 and not result["limited"]
    assert pm.query(root, "login")[0]["path"] == "src/login.ts"
    assert pm.status(root)["fresh"] and pm.status(root)["complete"]
