"""Opt-in numeric-loopback Ollama transport, never CLI inference or model downloads.

Schema: https://docs.ollama.com/api/tags and /api/generate; show/remote fields:
https://github.com/ollama/ollama/blob/main/api/types.go . Locality relies on the
installed runtime's API metadata, not daemon identity or OS network containment.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import threading

DEFAULT_ENDPOINT = 'http://127.0.0.1:11434'
MAX_RESPONSE = 512 * 1024
MAX_OUTPUT = 16000
ALLOWLIST = {('GET', '/api/version'), ('GET', '/api/tags'),
             ('POST', '/api/show'), ('POST', '/api/generate')}


class LocalError(ValueError):
    pass


def endpoint_address(endpoint):
    match = re.fullmatch(r'http://(\[[0-9A-Fa-f:]+\]|[0-9.]+):([0-9]{1,5})', endpoint)
    if not match:
        raise LocalError('Endpoint must be http://NUMERIC_LOOPBACK:PORT; no hostname, path, credentials or query.')
    host, port = match.group(1).strip('[]'), int(match.group(2))
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise LocalError('Endpoint must use a numeric loopback address.') from None
    if not address.is_loopback or not 1 <= port <= 65535:
        raise LocalError('Only numeric loopback and a valid port are allowed.')
    return str(address), port, socket.AF_INET6 if address.version == 6 else socket.AF_INET


class _Connection(http.client.HTTPConnection):
    def __init__(self, host, port, family, timeout):
        super().__init__(host, port, timeout=timeout)
        self.family = family
        self.wire = None

    def connect(self):
        # socket.create_connection resolves names even for numeric inputs. Never use it.
        self.wire = self.sock = socket.socket(self.family, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.host, self.port))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LocalError('Local runtime returned duplicate JSON fields.')
        result[key] = value
    return result


def _constant(_):
    raise LocalError('Local runtime returned invalid JSON numbers.')


class LoopbackTransport:
    def __init__(self, endpoint=DEFAULT_ENDPOINT, timeout=60):
        self.address = endpoint_address(endpoint)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
            raise LocalError('Local request timeout must be greater than zero and at most 300 seconds.')
        self.timeout = timeout

    def request(self, method, path, payload=None):
        if (method, path) not in ALLOWLIST:
            raise LocalError('Local runtime endpoint is not allowed.')
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode('utf-8')
        if body and len(body) > 128 * 1024:
            raise LocalError('Local request exceeds the input limit.')
        conn = _Connection(*self.address, self.timeout)
        expired = threading.Event()

        def expire():
            expired.set()
            if conn.wire is not None:
                try:
                    conn.wire.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.wire.close()

        timer = threading.Timer(self.timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            conn.request(method, path, body=body, headers={'Content-Type': 'application/json',
                                                         'Accept': 'application/json', 'Connection': 'close'})
            response = conn.getresponse()
            if response.status != 200:
                raise LocalError(f'Local runtime returned HTTP {response.status}; redirects and retries are disabled.')
            if response.getheader('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                raise LocalError('Local runtime did not return JSON.')
            data = response.read(MAX_RESPONSE + 1)
            if expired.is_set():
                raise LocalError('Local request deadline exceeded; generation may still be running in the runtime. No retry was made.')
            if len(data) > MAX_RESPONSE:
                raise LocalError('Local runtime response exceeds the size limit.')
            result = json.loads(data.decode('utf-8'), object_pairs_hook=_object, parse_constant=_constant)
            pending = [(result, 0)]
            while pending:
                value, depth = pending.pop()
                if depth > 32:
                    raise LocalError('Local runtime JSON nesting exceeds the limit.')
                if isinstance(value, dict):
                    pending.extend((item, depth + 1) for item in value.values())
                elif isinstance(value, list):
                    pending.extend((item, depth + 1) for item in value)
            if not isinstance(result, dict):
                raise LocalError('Local runtime returned an invalid response object.')
            if result.get('error'):
                raise LocalError('Local runtime reported an error; no retry or download was attempted.')
            return result
        except LocalError:
            raise
        except (OSError, http.client.HTTPException, UnicodeError, ValueError, RecursionError):
            if expired.is_set():
                raise LocalError('Local request deadline exceeded; runtime completion is uncertain. No retry was made.') from None
            raise LocalError('Local runtime unavailable or returned malformed data; no retry was made.') from None
        finally:
            timer.cancel()
            conn.close()
            if conn.wire is not None:
                conn.wire.close()


def _remote(value):
    if isinstance(value, dict):
        return any((('remote' in key.lower() or 'cloud' in key.lower()) and item not in (None, '', False))
                   or _remote(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_remote(item) for item in value)
    return False


def _model_name(model):
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}', model):
        raise LocalError('Select an exact model name listed by the local runtime.')
    if 'cloud' in model.lower() or '://' in model or model.startswith(('/', '.')):
        raise LocalError('Cloud or remote model names are not allowed.')


class OllamaLocal:
    def __init__(self, endpoint=DEFAULT_ENDPOINT, timeout=60, *, transport=None, finder=None):
        self.transport = transport or LoopbackTransport(endpoint, timeout)
        self.finder = finder or shutil.which

    def status(self):
        executable = self.finder('ollama')
        if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
            return {'status': 'unavailable', 'reason': 'Ollama is not installed on PATH. Nothing was started or downloaded.',
                    'unsupported_runtimes': [name for name in ('llama-cli', 'llama-server', 'llama-cpp') if self.finder(name)]}
        version = self.transport.request('GET', '/api/version').get('version')
        if not isinstance(version, str) or not re.fullmatch(r'[0-9][A-Za-z0-9.+_-]{0,79}', version):
            raise LocalError('Local Ollama version metadata is invalid.')
        models = self._models()
        return {'status': 'available', 'runtime': 'ollama', 'version': version,
                'models': [m['name'] for m in models if isinstance(m.get('name'), str) and len(m['name']) <= 200],
                'reason': 'Installed runtime detected; listed models still require local-weight validation.'}

    def _models(self):
        models = self.transport.request('GET', '/api/tags').get('models')
        if not isinstance(models, list) or len(models) > 1000 or any(not isinstance(m, dict) for m in models):
            raise LocalError('Local model list is invalid or exceeds the model limit.')
        return models

    def _entry(self, model):
        matches = [m for m in self._models() if m.get('name') == model]
        if len(matches) != 1:
            raise LocalError('Exact model is not uniquely present locally; no download or fallback is allowed.')
        entry = matches[0]
        if _remote(entry) or entry.get('model', model) != model:
            raise LocalError('Remote or ambiguous model metadata is not allowed.')
        size, digest, details = entry.get('size'), entry.get('digest'), entry.get('details')
        if (type(size) is not int or size <= 0 or not isinstance(digest, str)
                or not re.fullmatch(r'[0-9a-f]{64}', digest) or not isinstance(details, dict)
                or details.get('format') != 'gguf'):
            raise LocalError('Model metadata does not establish local GGUF weights.')
        return entry

    def inspect(self, model):
        _model_name(model)
        state = self.status()
        if state['status'] != 'available':
            raise LocalError(state['reason'])
        entry = self._entry(model)
        show = self.transport.request('POST', '/api/show', {'model': model, 'verbose': False})
        info, details, caps = show.get('model_info'), show.get('details'), show.get('capabilities')
        if _remote(show):
            raise LocalError('Model show metadata identifies remote or cloud execution.')
        if (not isinstance(info, dict) or not isinstance(info.get('general.architecture'), str)
                or not info['general.architecture'] or type(info.get('general.parameter_count')) is not int
                or info['general.parameter_count'] <= 0 or not isinstance(details, dict)
                or details.get('format') != 'gguf' or not isinstance(caps, list) or 'completion' not in caps):
            raise LocalError('Local weight architecture, parameter count and completion capability are not established.')
        return {'model': model, 'digest': entry['digest'], 'size': entry['size'], 'runtime_version': state['version']}

    def generate(self, evidence, prompt):
        model = evidence['model']
        _model_name(model)
        current = self._entry(model)
        if current['digest'] != evidence['digest'] or current['size'] != evidence['size']:
            raise LocalError('Model changed since validation; generation refused.')
        result = self.transport.request('POST', '/api/generate', {
            'model': model, 'prompt': prompt, 'stream': False, 'raw': True, 'keep_alive': 0,
            'options': {'num_predict': 1024, 'temperature': 0},
        })
        output = result.get('response')
        if _remote(result) or result.get('model') != model or result.get('done') is not True:
            raise LocalError('Generation did not report completed local execution for the selected model.')
        if result.get('done_reason') != 'stop':
            raise LocalError('Generation stopped without a complete response (possibly output limit).')
        if not isinstance(output, str) or not output.strip() or len(output.encode('utf-8')) > MAX_OUTPUT:
            raise LocalError('Generated text is empty or exceeds the output limit.')
        return output
