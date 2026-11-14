"""P00: network imports are confined to reviewed loopback boundaries.

The full suite must pass with external networking blocked. Authorized official
CLI calls and confirmed pinned installs have their own subprocess boundaries.
This static check catches accidental imports; it is not network containment.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "tsmax"

NETWORK_MODULES = {'socket', 'http', 'urllib', 'requests', 'httpx', 'aiohttp', 'httplib'}
ALLOWED_IMPORTS = {
    'dashboard.py': {'socket', 'http.server'},
    'dashlink.py': {'socket'},
    'adapters/ollama_local.py': {'socket', 'http.client'},
}


def network_imports(source):
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names = [node.module or '']
        else:
            continue
        found.update(name for name in names if name.split('.')[0] in NETWORK_MODULES)
    return found


def test_p00_network_imports_stay_in_reviewed_loopback_modules():
    hits = []
    for path in SRC.rglob("*.py"):
        imports = network_imports(path.read_text(encoding="utf-8"))
        unexpected = imports - ALLOWED_IMPORTS.get(path.relative_to(SRC).as_posix(), set())
        if unexpected:
            hits.append((str(path.relative_to(REPO)), sorted(unexpected)))
    assert hits == []


@pytest.mark.parametrize('source, expected', [
    ('import http.client', {'http.client'}),
    ('import json, requests as client', {'requests'}),
    ('from urllib import request', {'urllib'}),
    ('from socket import socket as connect', {'socket'}),
    ('from . import local_offload', set()),
])
def test_p00_import_check_covers_dotted_and_aliased_forms(source, expected):
    assert network_imports(source) == expected


@pytest.mark.p00_outer
def test_p00_suite_under_unshare_rn():
    if os.environ.get("TSMAX_P00_INNER") == "1":
        pytest.skip("already inside the network-blocked inner run")
    if not sys.platform.startswith("linux") or shutil_which("unshare") is None:
        pytest.skip(f"unshare -rn needs Linux with util-linux unshare (platform: {sys.platform})")
    probe = subprocess.run(["unshare", "-rn", "true"], capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip(f"unshare -rn unavailable: {probe.stderr.strip() or probe.stdout.strip() or probe.returncode}")
    env = os.environ.copy()
    env["TSMAX_P00_INNER"] = "1"
    proc = subprocess.run(
        [
            # No route off the machine; loopback is brought up (when `ip` exists) so the
            # dashboard's 127.0.0.1 server tests run here too instead of skipping.
            "unshare", "-rn", "sh", "-c", 'ip link set lo up 2>/dev/null || true; exec "$@"', "sh",
            sys.executable, "-m", "pytest", "-q", "--tb=short",
            "-p", "no:cacheprovider",
            "-m", "not p00_outer",
            str(REPO / "tests"),
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"network-blocked suite failed (exit {proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def shutil_which(name: str):
    import shutil
    return shutil.which(name)
