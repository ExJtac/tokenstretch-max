"""Atomic private writes must not follow an attacker's predictable temporary path."""
import stat
import json
import os

import pytest

from tsmax.installer import _write_private
from tsmax.installer import apply_changes, sha256_bytes, undo_manifest
from tsmax.context import Context
from tsmax import secure_files


def test_private_write_does_not_follow_old_temporary_symlink(tmp_path):
    target = tmp_path / "untouched"
    target.write_bytes(b"original")
    output = tmp_path / "map.sqlite3"
    trap = tmp_path / "map.sqlite3.tmp"
    trap.symlink_to(target)
    _write_private(output, b"private index")
    assert target.read_bytes() == b"original"
    assert trap.is_symlink()
    assert output.read_bytes() == b"private index"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".map.sqlite3.*.tmp"))


def test_failed_private_replace_preserves_previous_file_and_cleans_temp(tmp_path, monkeypatch):
    output = tmp_path / "map.sqlite3"
    output.write_bytes(b"old index")

    def fail(*args):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("tsmax.installer.os.replace", fail)
    with pytest.raises(OSError, match="simulated replace failure"):
        _write_private(output, b"new index")
    assert output.read_bytes() == b"old index"
    assert not list(tmp_path.glob(".map.sqlite3.*.tmp"))


def test_secure_apply_and_undo_preserve_bytes_and_reject_changed_preimage(tmp_path):
    home, root = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    root.mkdir()
    target = root / "index.bin"
    target.write_bytes(b"old\x00index")
    ctx = Context(home, root)
    change = {"path": str(target), "content": b"new\x00index", "private": True,
              "secure": True, "preimage": sha256_bytes(b"old\x00index")}
    result = apply_changes(ctx, [change], stamp="one")
    assert result["manifest"]["secure"]
    assert target.read_bytes() == b"new\x00index"
    with pytest.raises(ValueError, match="planned"):
        apply_changes(ctx, [change], stamp="two")
    undo_manifest(result["manifest_path"])
    assert target.read_bytes() == b"old\x00index"


@pytest.mark.parametrize("ancestor", [False, True])
def test_secure_apply_refuses_credential_symlinks_before_backup(tmp_path, ancestor):
    home, root = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    root.mkdir()
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    secret = secret_dir / "config.json"
    secret.write_bytes(b"DO_NOT_READ")
    if ancestor:
        (root / "config").symlink_to(secret_dir, target_is_directory=True)
        target = root / "config" / "config.json"
    else:
        target = root / "config.json"
        target.symlink_to(secret)
    change = {"path": str(target), "content": "new", "private": True,
              "secure": True, "preimage": None}
    with pytest.raises((OSError, ValueError)):
        apply_changes(Context(home, root), [change], stamp="blocked")
    assert secret.read_bytes() == b"DO_NOT_READ"
    assert not list((home / ".tsmax").rglob("config.json"))


def test_secure_read_does_not_follow_ancestor_swapped_at_open(tmp_path, monkeypatch):
    parent = tmp_path / "source"
    parent.mkdir()
    (parent / "file").write_bytes(b"safe")
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "file").write_bytes(b"DO_NOT_READ")
    original = secure_files.os.open
    swapped = False

    def swap(name, flags, *args, **kwargs):
        nonlocal swapped
        if name == "source" and not swapped:
            swapped = True
            parent.rename(tmp_path / "old-source")
            parent.symlink_to(secret, target_is_directory=True)
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(secure_files.os, "open", swap)
    # Retain the platform capability record when wrapping the supported function.
    monkeypatch.setattr(secure_files.os, "supports_dir_fd", {*secure_files.os.supports_dir_fd, swap})
    with pytest.raises(OSError):
        secure_files.read_bytes(parent / "file")
    assert swapped


def test_secure_undo_rejects_replaced_target_without_reading_link(tmp_path):
    home, root = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    root.mkdir()
    target = root / "index"
    result = apply_changes(Context(home, root), [{"path": str(target), "content": "new",
        "private": True, "secure": True, "preimage": None}], stamp="one")
    target.unlink()
    secret = tmp_path / "credential"
    secret.write_bytes(b"DO_NOT_READ")
    target.symlink_to(secret)
    with pytest.raises(OSError):
        undo_manifest(result["manifest_path"])
    assert secret.read_bytes() == b"DO_NOT_READ"


def test_secure_partial_undo_refuses_edits_after_merge_snapshot(tmp_path, monkeypatch):
    import tsmax.installer as installer
    home, root = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    root.mkdir()
    target = root / "settings.json"
    original = b'{"theme":"dark"}\n'
    target.write_bytes(original)
    result = apply_changes(Context(home, root), [{"path": str(target),
        "content": '{"theme":"dark","added":true}\n', "private": True,
        "secure": True, "preimage": sha256_bytes(original)}], stamp="one")
    target.write_text('{"theme":"light","added":true}\n')
    merge = installer._key_level_undo

    def racing_merge(*args):
        merged = merge(*args)
        target.write_text('{"theme":"blue","added":true,"newEdit":true}\n')
        return merged

    monkeypatch.setattr(installer, "_key_level_undo", racing_merge)
    with pytest.raises(installer.DriftError):
        undo_manifest(result["manifest_path"])
    assert json.loads(target.read_text())["newEdit"] is True


def test_latest_manifest_uses_write_order_not_random_suffix_or_timezone(tmp_path):
    from tsmax.installer import latest_manifest
    directory = tmp_path / ".tsmax" / "manifests"
    directory.mkdir(parents=True)
    older = directory / "9999-map-z.json"
    newer = directory / "0000-map-a.json"
    older.write_text("{}")
    newer.write_text("{}")
    os.utime(older, ns=(1_000_000, 1_000_000))
    os.utime(newer, ns=(2_000_000, 2_000_000))
    assert latest_manifest(Context(tmp_path, tmp_path)) == newer
