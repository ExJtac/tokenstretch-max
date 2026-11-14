"""Provider-neutral, explicit-authorization argv runner; never uses a shell.

Adapters own command construction, model tier choices and output semantics.
This module neither chooses a provider nor substitutes a model on failure.
"""
from __future__ import annotations

import os
from pathlib import Path
import selectors
import re
import signal
import subprocess
import sys
import time

from .adapters.base import ConductorExecution, ConductorResponse, HeadlessRefused
from .mask import mask_text

MAX_OUTPUT = 2 * 1024 * 1024
MAX_TIMEOUT = 1800


def _stop(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def _run_argv(argv, cwd, env, timeout, max_output=MAX_OUTPUT):
    """Bounded process primitive, also used for offline help and synthetic tests."""
    if os.name != "posix":
        return ConductorExecution(None, stop_reason="unsupported process-group platform")
    proc = None
    chunks = {"stdout": bytearray(), "stderr": bytearray()}
    reason = None
    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        with selectors.DefaultSelector() as selector:
            for pipe, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map():
                if time.monotonic() - started >= timeout:
                    reason = "timeout"
                    break
                for key, _ in selector.select(.1):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = max_output - sum(map(len, chunks.values()))
                    chunks[key.data].extend(data[:max(0, remaining)])
                    if len(data) > remaining:
                        reason = "output-limit"
                        break
                if reason:
                    break
            if not reason:
                proc.wait(timeout=max(.01, timeout - (time.monotonic() - started)))
    except subprocess.TimeoutExpired:
        reason = "timeout"
    except KeyboardInterrupt:
        reason = "interrupted"
    except OSError as exc:
        reason = mask_text(str(exc))[:2000]
    finally:
        if proc is not None:
            _stop(proc)
            proc.stdout.close()
            proc.stderr.close()
    return ConductorExecution(proc.returncode if proc is not None else None,
        chunks["stdout"].decode("utf-8", "replace"), chunks["stderr"].decode("utf-8", "replace"), reason)


def native_environment(extra=None, path_names=()):
    # Select approved environment names before accessing values. Never enumerate
    # or copy credentials. Provider path relocations retain the existing login.
    names = ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", *path_names)
    env = {key: os.environ[key] for key in names if key in os.environ}
    forbidden = re.compile(r"HOME|PATH|AUTH|TOKEN|SECRET|PASSWORD|CREDENTIAL|PROXY|ENDPOINT|BASE_URL|API_KEY|CONFIG|SANDBOX|PERMISSION|OPTIONS|^LD_|^DYLD_", re.I)
    for key, value in (extra or {}).items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key) or forbidden.search(key) or value not in {"0", "1", "true", "false"}:
            raise HeadlessRefused("unsupported conductor environment override")
        env[key] = value
    return env


class ConductorRunner:
    def run(self, command, *, timeout_s, path_names=()):
        if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
            raise HeadlessRefused("real provider execution disabled under tests; use FakeConductorRunner")
        return _run_argv(command.argv, command.cwd, native_environment(command.env, path_names), timeout_s)


class FakeConductorRunner:
    """Finite queued responses. Exhaustion refuses rather than falling back live."""
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def run(self, command, *, timeout_s, path_names=()):
        self.calls.append({"command": command, "timeout_s": timeout_s, "path_names": path_names})
        if not self.responses:
            raise HeadlessRefused("fake conductor response queue exhausted")
        result = self.responses.pop(0)
        if not isinstance(result, ConductorExecution):
            raise TypeError("fake responses must be ConductorExecution instances")
        return result


def dispatch(adapter, ctx, request, *, runner=None):
    if request.authorized is not True:
        raise HeadlessRefused("conductor invocation requires explicit authorization")
    if request.operation not in {"plan", "task"} or request.tier not in {"top", "mid", "light"}:
        raise ValueError("unsupported conductor operation or tier")
    if (not isinstance(request.prompt, str) or not request.prompt.strip() or len(request.prompt.encode()) > 256_000
            or not isinstance(request.timeout_s, int) or isinstance(request.timeout_s, bool)
            or not 1 <= request.timeout_s <= MAX_TIMEOUT):
        raise ValueError("conductor requires a bounded prompt and timeout")
    from .adapters.map_support import safe_path
    cwd = Path(request.cwd)
    if not cwd.is_absolute() or safe_path(cwd) != cwd or not cwd.is_dir() or cwd != Path(ctx.root) or cwd == Path(ctx.home):
        raise HeadlessRefused("conductor requires a safe project root matching its context")
    caps = adapter.conductor_capabilities(ctx)
    mode_ok = caps.read_only_plan if request.operation == "plan" else caps.workspace_task
    if not (caps.available and caps.supported and caps.native_login and caps.fresh_session
            and request.operation in caps.operations and mode_ok):
        return ConductorResponse("unsupported", reason=caps.reason or "required conductor capability unavailable")
    model = request.model_override or caps.tiers.get(request.tier)
    if not isinstance(model, str) or not model or model.startswith('-') or len(model) > 128 or any(ord(c) < 33 for c in model):
        return ConductorResponse("unsupported", reason="selected model tier or override is unavailable")
    command = adapter.conductor_command(ctx, request, model)
    if command.cwd != cwd or not command.argv or not Path(command.argv[0]).is_absolute():
        raise HeadlessRefused("adapter command must use the selected root and absolute official executable")
    execution = (runner or ConductorRunner()).run(command, timeout_s=request.timeout_s, path_names=adapter.config_dir_env)
    if execution.stop_reason:
        status = execution.stop_reason if execution.stop_reason in {"timeout", "interrupted"} else "failed"
        return ConductorResponse(status, requested_model=model, exit_code=execution.exit_code,
                                 reason=mask_text(execution.stop_reason)[:2000])
    response = adapter.parse_conductor_output(execution, model)
    if response.effective_model is not None and not adapter.conductor_model_matches(model, response.effective_model):
        response.status = "failed"
        response.reason = "Official CLI reported a different effective model; no substitution was authorized."
    response.text = mask_text(response.text)
    response.reason = mask_text(response.reason)[:2000] if response.reason else None
    return response


def local_cli_evidence(binary: Path, *, help_args=("--help",), probe_env=None) -> tuple[str | None, str, str | None]:
    """Inspect only local help/version, with OS-level network denial on macOS.

    Other hosts need a separately verified probe profile; do not speculate about
    a newer binary's flags or permission semantics.
    """
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        return None, "", "CLI capability probes are replaced with fixtures under tests."
    if sys.platform != "darwin" or not Path('/usr/bin/sandbox-exec').is_file():
        return None, "", "No verified offline CLI capability probe on this platform."
    prefix = ['/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)(deny network*)']
    env = native_environment(probe_env)
    version = _run_argv([*prefix, str(binary), '--version'], Path.cwd(), env, 10, 64000)
    help_result = _run_argv([*prefix, str(binary), *help_args], Path.cwd(), env, 10, 64000)
    if version.exit_code or help_result.exit_code or version.stop_reason or help_result.stop_reason:
        return None, "", "Offline CLI help/version probe failed."
    return version.stdout.strip(), help_result.stdout, None
