"""Synthetic local-only transport and text consent tests; no actual model inference."""
from __future__ import annotations

import argparse
import copy
import http.server
import io
import ipaddress
import json
from pathlib import Path
import socket
import threading
import time

import pytest

from conftest import NetworkBlockedError, credential_guard
from tsmax import local_cli, local_offload
from tsmax.adapters import ollama_local as ol

_REAL_CONNECT = socket.socket.connect
MODEL = 'fixture:latest'
ENTRY = {'name': MODEL, 'model': MODEL, 'size': 123456, 'digest': 'a' * 64, 'details': {'format': 'gguf'}}
SHOW = {'details': {'format': 'gguf'}, 'model_info': {'general.architecture': 'fixture', 'general.parameter_count': 1000},
        'capabilities': ['completion']}
GENERATED = {'model': MODEL, 'response': 'A concise synthetic proposal.', 'done': True, 'done_reason': 'stop'}


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.models = [copy.deepcopy(ENTRY)]
        self.show = copy.deepcopy(SHOW)
        self.generated = copy.deepcopy(GENERATED)

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        return copy.deepcopy({'/api/version': {'version': '0.13.0'}, '/api/tags': {'models': self.models},
                              '/api/show': self.show, '/api/generate': self.generated}[path])


@pytest.fixture
def fake(tmp_path):
    executable = tmp_path / 'ollama'
    executable.write_text('#!/bin/sh\nexit 99\n')
    executable.chmod(0o700)
    transport = FakeTransport()
    adapter = ol.OllamaLocal(transport=transport, finder=lambda name: str(executable) if name == 'ollama' else None)
    return adapter, transport


def test_missing_runtime_never_probes_network_or_starts_cli():
    transport = FakeTransport()
    adapter = ol.OllamaLocal(transport=transport, finder=lambda _: None)
    with credential_guard():
        result = local_offload.status(adapter=adapter)
        assert result['status'] == 'unavailable'
        with pytest.raises(ol.LocalError, match='not installed'):
            local_offload.transform('summary', 'hello', MODEL, run=True, adapter=adapter)
    assert transport.calls == []


def test_llama_is_detected_but_not_run():
    result = ol.OllamaLocal(transport=FakeTransport(), finder=lambda name: '/not/executed' if name == 'llama-cli' else None).status()
    assert result['status'] == 'unavailable'
    assert result['unsupported_runtimes'] == ['llama-cli']


@pytest.mark.parametrize('operation', local_offload.OPERATIONS)
def test_preview_never_sends_supplied_text_or_generates(fake, operation):
    adapter, transport = fake
    with credential_guard():
        result = local_offload.transform(operation, 'PRIVATE INPUT CANARY', MODEL, adapter=adapter)
    assert result['status'] == 'preview'
    assert '/api/generate' not in str(transport.calls)
    assert 'PRIVATE INPUT CANARY' not in str(transport.calls) + str(result)
    assert result['evidence']['digest'] == ENTRY['digest']


def test_authorized_generation_has_no_context_and_writes_nothing(fake, tmp_path):
    adapter, transport = fake
    before = list(tmp_path.rglob('*'))
    result = local_offload.transform('handoff', 'finished A, unresolved B', MODEL, run=True, adapter=adapter)
    assert result['status'] == 'completed'
    assert result['output'] == GENERATED['response']
    payload = transport.calls[-1][2]
    assert transport.calls[-1][:2] == ('POST', '/api/generate')
    assert payload['model'] == MODEL and payload['stream'] is False and payload['raw'] is True
    assert payload['keep_alive'] == 0 and payload['options']['num_predict'] == 1024
    assert not {'context', 'messages', 'tools', 'images'} & payload.keys()
    assert list(tmp_path.rglob('*')) == before
    assert not hasattr(adapter, 'history')


@pytest.mark.parametrize('text', ['', ' ', 'x' * 16001, '😀' * 4001])
def test_input_bounds_before_metadata(fake, text):
    adapter, transport = fake
    with pytest.raises(ol.LocalError, match='UTF-8'):
        local_offload.transform('summary', text, MODEL, run=True, adapter=adapter)
    assert not transport.calls


@pytest.mark.parametrize('model', ['fixture:cloud', 'fixture-cloud:latest', 'https://ollama.com/x', '', 'x\nY'])
def test_remote_names_refused_before_metadata(fake, model):
    adapter, transport = fake
    with pytest.raises(ol.LocalError):
        adapter.inspect(model)
    assert not transport.calls


@pytest.mark.parametrize('mutation', [
    lambda t: t.models.clear(),
    lambda t: t.models.append(copy.deepcopy(ENTRY)),
    lambda t: t.models[0].update(remote_host='https://ollama.com'),
    lambda t: t.models[0].update(remote_model='cloud-model'),
    lambda t: t.models[0].update(size=0),
    lambda t: t.models[0].update(size=True),
    lambda t: t.models[0].update(digest='invalid'),
    lambda t: t.models[0].update(details={'format': 'remote'}),
    lambda t: t.show.update(remote_host='https://ollama.com'),
    lambda t: t.show.update(model_info={}),
    lambda t: t.show.update(model_info={'general.architecture': 'fixture', 'general.parameter_count': True}),
    lambda t: t.show.update(capabilities=['embedding']),
    lambda t: t.show.update(details={'format': 'gguf', 'remote_host': 'remote'}),
])
def test_cloud_alias_and_ambiguous_weight_evidence_refused(fake, mutation):
    adapter, transport = fake
    mutation(transport)
    with pytest.raises(ol.LocalError):
        local_offload.transform('summary', 'text', MODEL, run=True, adapter=adapter)
    assert not any(path == '/api/generate' for _, path, _ in transport.calls)


@pytest.mark.parametrize('change', ['missing', 'digest', 'remote'])
def test_model_disappearing_or_changing_before_generation_refuses(fake, change):
    adapter, transport = fake
    evidence = adapter.inspect(MODEL)
    if change == 'missing':
        transport.models.clear()
    elif change == 'digest':
        transport.models[0]['digest'] = 'b' * 64
    else:
        transport.models[0]['remote_host'] = 'https://ollama.com'
    with pytest.raises(ol.LocalError):
        adapter.generate(evidence, 'text')
    assert not any(path == '/api/generate' for _, path, _ in transport.calls)


@pytest.mark.parametrize('patch', [
    {'response': 'x' * 16001}, {'response': ''}, {'done': False}, {'done_reason': 'length'},
    {'model': 'other'}, {'remote_host': 'https://ollama.com'},
])
def test_generation_bad_or_incomplete_response_refuses(fake, patch):
    adapter, transport = fake
    transport.generated.update(patch)
    with pytest.raises(ol.LocalError):
        local_offload.transform('summary', 'text', MODEL, run=True, adapter=adapter)
    assert sum(path == '/api/generate' for _, path, _ in transport.calls) == 1


@pytest.mark.parametrize('endpoint', ['http://localhost:11434', 'http://192.0.2.1:80', 'https://127.0.0.1:1',
    'http://user@127.0.0.1:1', 'http://127.0.0.1:1/a', 'http://127.0.0.1:1?x', 'http://127.1:1',
    'http://127.0.0.1:0', 'http://[::ffff:127.0.0.1]:1', 'http://[::1%lo]:1'])
def test_non_numeric_or_non_loopback_endpoints_refused(endpoint):
    with pytest.raises(ol.LocalError):
        ol.LoopbackTransport(endpoint)


def test_numeric_ipv4_and_ipv6():
    assert ol.endpoint_address('http://127.0.0.1:11434') == ('127.0.0.1', 11434, socket.AF_INET)
    assert ol.endpoint_address('http://[::1]:11434') == ('::1', 11434, socket.AF_INET6)


@pytest.mark.parametrize('path', ['/api/pull', '/api/create', '/api/delete', '/api/chat', '/api/show?x'])
def test_endpoint_allowlist_before_socket(path):
    with pytest.raises(ol.LocalError, match='not allowed'):
        ol.LoopbackTransport().request('POST', path, {})


@pytest.fixture
def server(monkeypatch):
    def connect(sock, address):
        if not ipaddress.ip_address(address[0]).is_loopback:
            raise NetworkBlockedError('Only numeric loopback allowed in local transport test.')
        return _REAL_CONNECT(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)
    state = {'mode': 'normal', 'requests': []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.do_GET()

        def do_GET(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            state['requests'].append((self.command, self.path, body))
            mode = state['mode']
            try:
                if mode in {'header-drip', 'body-drip'}:
                    wire = b'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n'
                    if mode == 'body-drip':
                        self.wfile.write(wire)
                        self.wfile.flush()
                        wire = b'{"version":"0.13.0"}'
                    for byte in wire:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.02)
                    return
                self.send_response(302 if mode == 'redirect' else 200)
                self.send_header('Content-Type', 'application/json')
                if mode == 'redirect':
                    self.send_header('Location', 'http://192.0.2.1:80/api/version')
                self.end_headers()
                data = {
                    'normal': json.dumps({'/api/version': {'version': '0.13.0'}, '/api/tags': {'models': [ENTRY]},
                                          '/api/show': SHOW, '/api/generate': GENERATED}[self.path]).encode(),
                    'malformed': b'{oops', 'duplicate': b'{"version":"1","version":"2"}',
                    'oversize': b'x' * (ol.MAX_RESPONSE + 1), 'nan': b'{"value":NaN}',
                    'array': b'[]', 'nested': b'{"x":' + b'[' * 40 + b'0' + b']' * 40 + b'}', 'error': b'{"error":"DO NOT ECHO PRIVATE SERVER DETAILS"}',
                    'redirect': b'{}',
                }[mode]
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
    try:
        httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    except OSError:
        pytest.skip('Loopback unavailable in this network namespace.')
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{httpd.server_port}', state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_real_loopback_ignores_proxy_and_ollama_host_without_dns(server, monkeypatch):
    endpoint, state = server
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'OLLAMA_HOST'):
        monkeypatch.setenv(key, 'http://192.0.2.1:9999')
    assert ol.LoopbackTransport(endpoint).request('GET', '/api/version') == {'version': '0.13.0'}
    assert len(state['requests']) == 1
    with pytest.raises(NetworkBlockedError):
        socket.socket().connect(('192.0.2.1', 80))
    with pytest.raises(NetworkBlockedError):
        socket.getaddrinfo('localhost', 80)


@pytest.mark.parametrize('mode', ['redirect', 'malformed', 'duplicate', 'oversize', 'nan', 'array', 'nested', 'error'])
def test_real_http_rejections(server, mode):
    endpoint, state = server
    state['mode'] = mode
    with pytest.raises(ol.LocalError) as exc:
        ol.LoopbackTransport(endpoint).request('GET', '/api/version')
    assert 'PRIVATE SERVER' not in str(exc.value)
    assert len(state['requests']) == 1


@pytest.mark.parametrize('mode', ['header-drip', 'body-drip'])
def test_total_deadline_stops_drip_headers_and_body(server, mode):
    endpoint, state = server
    state['mode'] = mode
    started = time.monotonic()
    with pytest.raises(ol.LocalError, match='deadline'):
        ol.LoopbackTransport(endpoint, timeout=0.12).request('GET', '/api/version')
    assert time.monotonic() - started < 0.8
    assert len(state['requests']) == 1


def test_cli_preview_stdin_and_json(fake, monkeypatch, capsys):
    adapter, _ = fake
    monkeypatch.setattr(local_offload, 'OllamaLocal', lambda *_: adapter)
    parser = argparse.ArgumentParser()
    local_cli.add_parser(parser.add_subparsers(dest='cmd'))
    args = parser.parse_args(['local', 'summary', '--model', MODEL, '--stdin', '--json'])
    monkeypatch.setattr('sys.stdin', io.StringIO('PRIVATE INPUT'))
    assert local_cli.run(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'preview' and 'PRIVATE INPUT' not in str(result)


def test_cli_refusal_missing_runtime(monkeypatch, capsys):
    monkeypatch.setattr(ol.shutil, 'which', lambda _: None)
    parser = argparse.ArgumentParser()
    local_cli.add_parser(parser.add_subparsers(dest='cmd'))
    assert local_cli.run(parser.parse_args(['local', 'status', '--json'])) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'unavailable'


def test_full_synthetic_ollama_protocol_over_loopback(server, tmp_path):
    endpoint, state = server
    binary = tmp_path / 'ollama'
    binary.write_text('#!/bin/sh\nexit 99\n')
    binary.chmod(0o700)
    adapter = ol.OllamaLocal(endpoint, finder=lambda name: str(binary) if name == 'ollama' else None)
    result = local_offload.transform('commit-message', 'Supplied synthetic change', MODEL, run=True, adapter=adapter)
    assert result['output'] == GENERATED['response']
    assert all((method, path) in ol.ALLOWLIST for method, path, _ in state['requests'])
    generation = [(method, path, json.loads(body)) for method, path, body in state['requests'] if path == '/api/generate']
    assert len(generation) == 1
    assert generation[0][2]['model'] == MODEL and 'Supplied synthetic change' in generation[0][2]['prompt']


@pytest.mark.parametrize('timeout', [0, -1, 301, float('inf'), float('nan'), True, '60'])
def test_timeout_configuration_is_bounded(timeout):
    with pytest.raises(ol.LocalError):
        ol.LoopbackTransport(timeout=timeout)
