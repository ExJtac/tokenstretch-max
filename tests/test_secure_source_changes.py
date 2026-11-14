"""Source edits preserve modes and remain undoable after partial application."""
from pathlib import Path
import hashlib
import json
import os

import pytest

from tsmax.context import Context
from tsmax import installer, secure_files as sf


@pytest.fixture
def ctx(tmp_path):
    root, home = tmp_path / "project", tmp_path / "home"
    root.mkdir()
    home.mkdir()
    return Context(root=root, home=home)


def change(path, content="after\n", *, mode=0o644, kind="modify"):
    before = path.read_bytes() if path.exists() else None
    return {"path": str(path), "kind": kind, "secure": True, "private": True,
            "preimage": hashlib.sha256(before).hexdigest() if before is not None else None,
            "preimage_mode": path.stat().st_mode & 0o777 if before is not None else None,
            "content": "" if kind == "delete" else content, "mode": mode}


def test_executable_edit_delete_and_create_restore_modes(ctx):
    script, deleted, created = ctx.root / "run.sh", ctx.root / "old.py", ctx.root / "new.py"
    script.write_text("before\n"); script.chmod(0o755)
    deleted.write_text("original\n"); deleted.chmod(0o640)
    result = installer.apply_changes(ctx, [change(script, mode=0o755), change(deleted, kind="delete"), change(created)])
    assert script.read_text() == "after\n" and sf.file_mode(script) == 0o755
    assert not deleted.exists() and sf.file_mode(created) == 0o644
    for artifact in Path(result["backup_dir"]).rglob("*"):
        if artifact.is_file():
            assert sf.file_mode(artifact) == 0o600
    assert sf.file_mode(Path(result["manifest_path"])) == 0o600
    installer.undo_manifest(Path(result["manifest_path"]))
    assert script.read_text() == "before\n" and sf.file_mode(script) == 0o755
    assert deleted.read_text() == "original\n" and sf.file_mode(deleted) == 0o640
    assert not created.exists()
    assert len(installer.undo_manifest(Path(result["manifest_path"]))["skipped"]) == 3


def test_mode_only_change_is_not_mistaken_for_already_undone(ctx):
    path = ctx.root / "script.py"
    path.write_text("same\n"); path.chmod(0o644)
    result = installer.apply_changes(ctx, [change(path, "same\n", mode=0o755)])
    assert sf.file_mode(path) == 0o755
    installer.undo_manifest(Path(result["manifest_path"]))
    assert sf.file_mode(path) == 0o644


def test_mode_drift_and_recreated_deletion_refuse_whole_undo(ctx):
    one, two = ctx.root / "one.py", ctx.root / "two.py"
    one.write_text("one\n"); one.chmod(0o644)
    two.write_text("two\n"); two.chmod(0o644)
    result = installer.apply_changes(ctx, [change(one), change(two, kind="delete")])
    one.chmod(0o755)
    with pytest.raises(installer.DriftError):
        installer.undo_manifest(Path(result["manifest_path"]))
    assert one.read_text() == "after\n" and not two.exists()
    one.chmod(0o644); two.write_text("later user edit\n"); two.chmod(0o644)
    with pytest.raises(installer.DriftError):
        installer.undo_manifest(Path(result["manifest_path"]))
    assert one.read_text() == "after\n" and two.read_text() == "later user edit\n"


def test_all_preimages_checked_before_any_source_write(ctx):
    one, two = ctx.root / "one.py", ctx.root / "two.py"
    one.write_text("one\n"); two.write_text("two\n")
    changes = [change(one), change(two)]
    two.write_text("newer\n")
    with pytest.raises(ValueError, match="changed"):
        installer.apply_changes(ctx, changes)
    assert one.read_text() == "one\n" and two.read_text() == "newer\n"


def test_partial_application_has_complete_undo_manifest(ctx, monkeypatch):
    one, two = ctx.root / "one.py", ctx.root / "two.py"
    one.write_text("one\n"); two.write_text("two\n")
    one.chmod(0o644); two.chmod(0o644)
    real = sf.write_bytes
    def fail_second(path, data, *args, **kwargs):
        if path == two:
            raise OSError("simulated interruption")
        return real(path, data, *args, **kwargs)
    monkeypatch.setattr(sf, "write_bytes", fail_second)
    with pytest.raises(OSError, match="interruption"):
        installer.apply_changes(ctx, [change(one), change(two)], stamp="partial")
    manifest = ctx.home / ".tsmax/manifests/partial.json"
    assert manifest.exists() and len(json.loads(manifest.read_text())["files"]) == 2
    assert one.read_text() == "after\n" and two.read_text() == "two\n"
    monkeypatch.setattr(sf, "write_bytes", real)
    result = installer.undo_manifest(manifest)
    assert str(one) in result["restored"] and str(two) in result["skipped"]
    assert one.read_text() == "one\n"


@pytest.mark.parametrize("mode", [0o777, 0o6755, 0, 0o100, -1, True, "bad"])
def test_unsafe_modes_refuse_before_write(ctx, mode):
    path = ctx.root / "source.py"
    path.write_text("before\n")
    with pytest.raises(ValueError):
        installer.apply_changes(ctx, [change(path, mode=mode)])
    assert path.read_text() == "before\n"


def test_mode_preimage_and_reused_manifest_refuse(ctx):
    path = ctx.root / "source.py"
    path.write_text("before\n"); path.chmod(0o644)
    planned = change(path)
    path.chmod(0o755)
    with pytest.raises(ValueError, match="permissions"):
        installer.apply_changes(ctx, [planned])
    result = installer.apply_changes(ctx, [change(path)], stamp="once")
    with pytest.raises(ValueError, match="manifest already"):
        installer.apply_changes(ctx, [change(path)], stamp="once")
    assert json.loads(Path(result["manifest_path"]).read_text())["files"][0]["mode_before"] == 0o755


def test_secure_mode_not_filtered_by_process_umask(ctx):
    path = ctx.root / "script.py"
    previous = os.umask(0o077)
    try:
        sf.write_bytes(path, b"source\n", mode=0o755, expected_hash=None)
    finally:
        os.umask(previous)
    assert sf.file_mode(path) == 0o755


def test_default_private_undo_remains_private(ctx):
    path = ctx.root / "settings.json"
    path.write_text('{"old":true}\n'); path.chmod(0o644)
    planned = change(path, '{"new":true}\n')
    planned.pop("mode")
    result = installer.apply_changes(ctx, [planned])
    assert sf.file_mode(path) == 0o600
    installer.undo_manifest(Path(result["manifest_path"]))
    assert sf.file_mode(path) == 0o600


def test_deleted_path_symlink_cannot_be_restored_through(ctx):
    path, outside = ctx.root / "source.py", ctx.home / "private.txt"
    path.write_text("original\n"); outside.write_text("do not touch")
    result = installer.apply_changes(ctx, [change(path, kind="delete")])
    path.symlink_to(outside)
    with pytest.raises(OSError):
        installer.undo_manifest(Path(result["manifest_path"]))
    assert outside.read_text() == "do not touch"


def test_partial_json_undo_keeps_new_source_file_permissions(ctx):
    path = ctx.root / "source.json"
    result = installer.apply_changes(ctx, [change(path, '{"owned":true}\n')])
    path.write_text('{"owned":true,"user":true}\n')
    undone = installer.undo_manifest(Path(result["manifest_path"]))
    assert str(path) in undone["partial"]
    assert json.loads(path.read_text()) == {"user": True}
    assert sf.file_mode(path) == 0o644
