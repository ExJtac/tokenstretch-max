"""Read-only stdio MCP protocol, framing limits, and actual subprocess roundtrip."""
from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from tsmax.map_cli import server_command
from tsmax.map_mcp import MAX_MESSAGE_BYTES, Server, serve


def request(method, params=None, identifier=1):
    return {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params or {}}


def initialize(version="2025-06-18"):
    return request("initialize", {"protocolVersion": version, "capabilities": {},
                                  "clientInfo": {"name": "test", "version": "1"}})


def ready(root):
    server = Server(root)
    server.handle(initialize())
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    return server


def test_initialization_negotiation_and_lifecycle(tmp_path):
    server = Server(tmp_path)
    assert server.handle(request("tools/list"))["error"]["code"] == -32000
    assert server.handle(request("ping"))["result"] == {}
    assert server.handle(request("initialize"))["error"]["code"] == -32602
    response = server.handle(initialize("unknown-future-version"))
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert server.handle(request("tools/list"))["error"]["code"] == -32000
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    tools = server.handle(request("tools/list"))["result"]["tools"]
    assert {tool["name"] for tool in tools} == {"map_query", "map_status"}
    assert all(tool["annotations"]["readOnlyHint"] for tool in tools)
    assert server.handle(initialize())["error"]["code"] == -32600


@pytest.mark.parametrize("message,code", [
    ([], -32600), (None, -32600), ({"jsonrpc": "2.0", "id": True, "method": "ping"}, -32600),
    ({"jsonrpc": "2.0", "id": "a" * 257, "method": "ping"}, -32600),
    (request("tools/list", {"cursor": "forged"}), -32602),
    (request("resources/read", {"uri": "file:///private/secret"}), -32601),
    (request("tools/call", {"name": "map_build"}), -32602),
    (request("tools/call", {"name": {}}), -32602),
    (request("tools/call", {"name": []}), -32602),
    (request("tools/call", {"name": "shell", "arguments": {"command": "touch injected"}}), -32602),
    (request("tools/call", {"name": "map_status", "arguments": {"root": "/"}}), -32602),
    (request("tools/call", {"name": "map_query", "arguments": {"query": "x", "root": "/"}}), -32602),
    (request("tools/call", {"name": "map_query", "arguments": {"query": "x", "limit": True}}), -32602),
    (request("tools/call", {"name": "map_query", "arguments": {"query": "x", "limit": 11}}), -32602),
    (request("tools/call", {"name": "map_query", "arguments": {"query": "x" * 1025}}), -32602),
    (request("tools/call", {"name": "map_query", "arguments": {"query": "x" * 513}}), -32602),
    (request("tools/call", {"name": "map_query", "arguments": {"query": " "}}), -32602),
])
def test_malicious_requests_rejected_without_creating_files(tmp_path, message, code):
    assert ready(tmp_path).handle(message)["error"]["code"] == code
    assert list(tmp_path.iterdir()) == []


def test_notifications_cannot_call_tools(tmp_path, monkeypatch):
    from tsmax import project_map
    def forbidden(*args, **kwargs):
        pytest.fail("notification executed a map operation")
    monkeypatch.setattr(project_map, "query", forbidden)
    message = request("tools/call", {"name": "map_query", "arguments": {"query": "x"}})
    message.pop("id")
    assert ready(tmp_path).handle(message) is None


@pytest.mark.parametrize("raw", [b"{\n", b"\xff\n", b'{"a":1,"a":2}\n', b'{"x":NaN}\n'])
def test_invalid_frames_have_protocol_only_errors(tmp_path, raw):
    output = io.StringIO()
    assert serve(tmp_path, io.BytesIO(raw), output) == 0
    response = json.loads(output.getvalue())
    assert response["error"]["code"] == -32700


def test_oversized_frame_closes_without_draining_unbounded_input(tmp_path):
    output = io.StringIO()
    source = io.BytesIO(b"x" * (MAX_MESSAGE_BYTES * 3))
    assert serve(tmp_path, source, output) == 2
    assert source.tell() == MAX_MESSAGE_BYTES + 1
    assert json.loads(output.getvalue())["error"]["code"] == -32600


def test_missing_map_never_autobuilds(tmp_path, monkeypatch):
    from tsmax import project_map
    def forbidden(*args, **kwargs):
        pytest.fail("MCP attempted to build or update the map")
    monkeypatch.setattr(project_map, "build", forbidden)
    server = ready(tmp_path)
    result = server.handle(request("tools/call", {"name": "map_query", "arguments": {"query": "login"}}))
    assert result["result"]["isError"] is True
    result = server.handle(request("tools/call", {"name": "map_status"}))
    assert json.loads(result["result"]["content"][0]["text"])["exists"] is False
    assert list(tmp_path.iterdir()) == []


def test_large_dirty_status_reports_counts_without_path_lists(tmp_path, monkeypatch):
    from tsmax import project_map
    monkeypatch.setattr(project_map, "status", lambda root: {
        "exists": True, "fresh": False, "indexed": 20000,
        "added": ["file.py"] * 20000, "changed": ["file.py"] * 10000, "removed": [],
        "skipped": {"ignored": 50}, "limits": {"files": 20000}})
    response = ready(tmp_path).handle(request("tools/call", {"name": "map_status"}))
    assert response["result"]["isError"] is False
    value = json.loads(response["result"]["content"][0]["text"])
    assert value["added"] == 20000 and value["changed"] == 10000 and value["removed"] == 0
    assert len(json.dumps(response)) < 1000


def test_stdio_subprocess_queries_current_index_without_writes(tmp_path):
    from tsmax import project_map
    root, home = tmp_path / "project", tmp_path / "home"
    root.mkdir()
    home.mkdir()
    (root / "auth.py").write_text("def login_user():\n    return True\n")
    project_map.build(root, home)
    before = {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
              for p in root.rglob("*") if p.is_file()}
    messages = [initialize(), {"jsonrpc": "2.0", "method": "notifications/initialized"},
                request("tools/list", identifier=2),
                request("tools/call", {"name": "map_query", "arguments": {"query": "login"}}, 3),
                request("tools/call", {"name": "map_status"}, 4)]
    proc = subprocess.run(server_command(root), input="\n".join(map(json.dumps, messages)) + "\n",
                          text=True, capture_output=True, cwd=root, timeout=10)
    assert proc.returncode == 0, proc.stderr
    responses = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [response["id"] for response in responses] == [1, 2, 3, 4]
    assert "auth.py:1:" in responses[2]["result"]["content"][0]["text"]
    assert responses[3]["result"]["isError"] is False
    after = {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
             for p in root.rglob("*") if p.is_file()}
    assert after == before
