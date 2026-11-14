"""Explicitly authorized, offline verification with content-bound evidence.

Discovery is read-only. Project checks are executable input, and only run after
an explicit authorization. OS network isolation includes subprocesses.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
import tempfile
import tomllib
import uuid

from . import project_map as pm
from .adapters.map_support import current_exclusions
from .context import Context
from .installer import apply_changes
from .mask import mask_text

MAX_CHECKS = 12
MAX_OUTPUT = 64_000
MAX_TIMEOUT = 1800
KINDS = frozenset({'test', 'lint', 'typecheck', 'acceptance'})
BINARY_ASSET_SUFFIXES = frozenset({'.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif', '.ico', '.bmp',
                                  '.woff', '.woff2', '.ttf', '.otf', '.eot'})
ASSET_SUFFIXES = BINARY_ASSET_SUFFIXES | {'.svg'}
MAX_ASSET_BYTES = 8_000_000


def input_limit(rel):
    return MAX_ASSET_BYTES if Path(rel).suffix.lower() in ASSET_SUFFIXES else pm.MAX_FILE_BYTES


CONFIG_NAMES = frozenset({'package.json', 'pyproject.toml', 'pytest.ini', 'setup.cfg', 'tox.ini',
    'tsconfig.json', 'ruff.toml', '.ruff.toml', '.flake8', '.pylintrc', 'mypy.ini', '.mypy.ini',
    'pyrightconfig.json', '.eslintrc', '.eslintrc.json', '.eslintrc.js', '.eslintrc.cjs',
    'eslint.config.js', 'eslint.config.mjs', 'eslint.config.cjs', 'biome.json', 'biome.jsonc'})


@dataclass(frozen=True)
class Check:
    kind: str
    argv: tuple[str, ...]
    evidence: str = 'explicit selection'

    def __post_init__(self):
        if self.kind not in KINDS or not self.argv or len(self.argv) > 128:
            raise ValueError('Check requires a supported kind and a nonempty argv (at most 128 items).')
        if any(not isinstance(s, str) or not s or len(s) > 4096 or any(ord(c) < 32 for c in s) for s in self.argv):
            raise ValueError('Check arguments must be bounded strings without control characters.')

    def json(self):
        return {'kind': self.kind, 'argv': list(self.argv), 'evidence': self.evidence}


class VerifyPolicy(pm.Policy):
    def reason(self, rel, *, directory=False):
        reason = super().reason(rel, directory=directory)
        # Dependency locks are verification inputs even though they are not map text.
        if reason == 'binary/unsupported extension' and Path(rel).name.lower() in CONFIG_NAMES:
            return None
        # Build inputs include static images/fonts even though they are not map text.
        # Path-based secret, generated and adapter exclusions still take precedence.
        if reason == 'binary/unsupported extension' and Path(rel).suffix.lower() in ASSET_SUFFIXES:
            return None
        if reason == 'generated file' and Path(rel).name.lower() in pm.GENERATED_NAMES:
            return None
        return reason


def _policy(root, home):
    return VerifyPolicy(pm._exclusions(root, current_exclusions(root, home)))


def _read_config(root, home, name):
    with pm._root_fd(root) as fd:
        raw, reason = pm._read(fd, name, _policy(root, home))
    if reason == 'missing':
        return None
    if reason:
        raise ValueError(f'Cannot safely read verification configuration: {name}.')
    return raw


def snapshot(root: Path, home: Path, extra_files=()) -> dict:
    """Hash eligible source/config/lock bytes with the shared no-follow policy."""
    policy = _policy(root, home)
    with pm._root_fd(root) as fd:
        paths, skipped, limited = pm._walk(fd, policy)
        # Root check configuration and locks remain inputs even if Git ignores them.
        for name in CONFIG_NAMES | pm.GENERATED_NAMES | set(extra_files):
            raw, reason = pm._read(fd, name, policy, max_bytes=input_limit(name))
            if reason == 'missing' and name not in extra_files:
                continue
            if reason:
                skipped[reason] += 1
                limited = True
            elif name not in paths:
                paths.append(name)
        hashes, total = {}, 0
        for name in paths:
            if len(hashes) >= policy.max_files:
                limited = True
                break
            raw, reason = pm._read(fd, name, policy, max_bytes=input_limit(name))
            if reason:
                skipped[reason] += 1
                limited = True
                continue
            total += len(raw)
            if total > policy.max_total_bytes:
                limited = True
                break
            hashes[name] = hashlib.sha256(raw).hexdigest()
        for reason in ('unreadable ignore rules', 'unsupported ignore rules', 'unreadable directory',
                       'unreadable/symlink ancestor', 'directory depth cap', 'unsafe path', 'symlink/nonregular/hardlink'):
            limited = limited or bool(skipped[reason])
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    return {'digest': digest, 'files': len(hashes), 'complete': not limited,
            'skipped': dict(sorted(skipped.items())), 'hashes': hashes}


def detect(root: Path, home: Path) -> dict:
    """Conservatively select familiar Python/Node commands; never launch a tool."""
    checks, unresolved, notes = [], [], []
    package = _read_config(root, home, 'package.json')
    pyproject = _read_config(root, home, 'pyproject.toml')
    if package is not None:
        try:
            data = json.loads(package)
            scripts = data.get('scripts', {})
            if not isinstance(scripts, dict) or not isinstance(data.get('dependencies', {}), dict) or not isinstance(data.get('devDependencies', {}), dict):
                raise ValueError()
            manager = data.get('packageManager', 'npm').split('@')[0]
        except (ValueError, AttributeError, TypeError):
            raise ValueError('package.json must contain valid project metadata.') from None
        for kind, names in (('test', ('test', 'test:unit')), ('lint', ('lint',)), ('typecheck', ('typecheck', 'type-check', 'check:types'))):
            candidates = [name for name in names if isinstance(scripts.get(name), str) and scripts[name].strip()]
            if len(candidates) == 1 and manager == 'npm':
                checks.append(Check(kind, ('npm', '--offline', 'run', candidates[0]), 'package.json scripts.' + candidates[0]))
            elif candidates:
                unresolved.append(kind + ': select an exact command (ambiguous scripts or non-npm package manager).')
        dependencies = {**(data.get('dependencies') or {}), **(data.get('devDependencies') or {})}
        for kind, configured in (('lint', any(name in dependencies for name in ('eslint', '@biomejs/biome'))),
                                 ('typecheck', 'typescript' in dependencies or _read_config(root, home, 'tsconfig.json') is not None)):
            if configured and not any(c.kind == kind for c in checks) and not any(s.startswith(kind + ':') for s in unresolved):
                unresolved.append(kind + ': tooling exists but no unique supported script; select its command.')
    py = {}
    if pyproject is not None:
        try:
            py = tomllib.loads(pyproject.decode('utf-8'))
        except (ValueError, UnicodeError):
            raise ValueError('pyproject.toml must contain valid TOML.') from None
    tool = py.get('tool', {})
    if not isinstance(tool, dict):
        raise ValueError('Python tool configuration must be a table.')
    project = py.get('project', {})
    if not isinstance(project, dict):
        raise ValueError('Python project configuration must be a table.')
    dependencies = project.get('dependencies', [])
    optional = project.get('optional-dependencies', {})
    if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
        raise ValueError('Python dependencies must be a list of strings.')
    if not isinstance(optional, dict) or any(not isinstance(group, list) or not all(isinstance(item, str) for item in group) for group in optional.values()):
        raise ValueError('Python optional dependencies must map group names to lists of strings.')
    declared = list(dependencies)
    for group in optional.values():
        declared.extend(group)
    declared_names = {re.split(r'[<>=!~ ;\[]', dep.lower())[0] for dep in declared if isinstance(dep, str)}
    pytest_ini = _read_config(root, home, 'pytest.ini')
    setup_cfg = _read_config(root, home, 'setup.cfg')
    has_pytest = 'pytest' in declared_names or 'pytest' in tool or pytest_ini is not None or bool(setup_cfg and b'[tool:pytest]' in setup_cfg)
    if has_pytest:
        checks.append(Check('test', (sys.executable, '-m', 'pytest', '-q'), 'existing pytest configuration'))
    for name, kind, args in (('ruff', 'lint', ('check', '.')), ('flake8', 'lint', ('.',)),
                             ('pylint', 'lint', ()), ('mypy', 'typecheck', ('.',)), ('pyright', 'typecheck', ())):
        present = name in tool or name in declared_names
        for config in {'ruff': ('ruff.toml', '.ruff.toml'), 'flake8': ('.flake8',), 'pylint': ('.pylintrc',),
                       'mypy': ('mypy.ini', '.mypy.ini'), 'pyright': ('pyrightconfig.json',)}[name]:
            present = _read_config(root, home, config) is not None or present
        if setup_cfg and re.search(rb'^\[' + name.encode() + rb'\]', setup_cfg, re.M):
            present = True
        if present:
            if name == 'pylint':
                unresolved.append('lint: pylint requires explicit target selection.')
            else:
                checks.append(Check(kind, (sys.executable, '-m', name, *args), 'existing ' + name + ' configuration'))
    if not any(c.kind == 'test' for c in checks) and not any(s.startswith('test:') for s in unresolved):
        unresolved.append('test: no supported test command detected; select a required test command.')
    if not package and not pyproject and not checks:
        notes.append('Unsupported project: supply explicit required commands.')
    notes.append('Only configured checks are detected; use --check for additional required lint, typecheck or acceptance checks.')
    if len(checks) > MAX_CHECKS:
        raise ValueError('Too many detected checks; select a bounded check set.')
    return {'checks': checks, 'unresolved': unresolved, 'notes': notes}


def select(detected: dict, explicit: list[Check]) -> dict:
    if len(explicit) > MAX_CHECKS:
        raise ValueError('At most 12 checks are allowed.')
    kinds = {c.kind for c in explicit}
    checks = [c for c in detected['checks'] if c.kind not in kinds] + explicit
    if len(checks) > MAX_CHECKS:
        raise ValueError('At most 12 checks are allowed.')
    unresolved = [s for s in detected['unresolved'] if s.split(':', 1)[0] not in kinds]
    return {**detected, 'checks': checks, 'unresolved': unresolved}


def _diagnostic(text):
    text = mask_text(text)
    text = re.sub(r'(?im)\b(api[_-]?key|password|passwd|secret|token|authorization)\s*[:=]\s*[^\s,;]+', r'\1=[redacted]', text)
    text = re.sub(r'CANARY_[A-Za-z0-9_-]+', '[redacted]', text)
    return ''.join(c if c in '\n\t' or c.isprintable() else '?' for c in text)[:MAX_OUTPUT]


def _terminate(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=5)


def _execute(argv, root, env, timeout, max_output=MAX_OUTPUT):
    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True)
    except OSError:
        return {'status': 'incomplete', 'reason': 'executable unavailable', 'exit_code': None, 'output': '', 'duration_s': 0.0}
    buffer = bytearray()
    reason = None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() - started >= timeout:
                    reason = 'timeout'
                    break
                for key, _ in selector.select(min(.1, timeout)):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    available = max_output - len(buffer)
                    buffer.extend(chunk[:max(0, available)])
                    if len(chunk) > available:
                        reason = 'output limit exceeded'
                        break
                if reason:
                    break
            if reason:
                _terminate(proc)
            else:
                try:
                    proc.wait(timeout=max(.01, timeout - (time.monotonic() - started)))
                except subprocess.TimeoutExpired:
                    reason = 'timeout'
                    _terminate(proc)
            # Clean up remaining members of this process group. A detached session
            # can escape the group; this does not prove descendant quiescence.
            try:
                os.killpg(proc.pid, 0)
                if reason is None:
                    reason = 'background processes remained in the check process group'
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    finally:
        if proc.poll() is None:
            _terminate(proc)
        proc.stdout.close()
    return {'status': 'incomplete' if reason else 'passed' if proc.returncode == 0 else 'failed',
            'reason': reason, 'exit_code': proc.returncode,
            'output': _diagnostic(buffer.decode('utf-8', errors='replace')), 'duration_s': round(time.monotonic() - started, 3)}


class OfflineRunner:
    """OS isolation is mandatory. There is deliberately no online fallback."""
    def __init__(self):
        self.prefix = None
        self.name = None
        self.error = None
        self.temporary_home = None

    def prepare(self, root, home):
        if sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file():
            profile = '(version 1)(allow default)(deny network*)(allow network-inbound (local ip "localhost:*"))(allow network-outbound (remote ip "localhost:*"))'
            self.prefix = ['/usr/bin/sandbox-exec', '-p', profile]
            self.name = 'macos-sandbox-exec'
        elif sys.platform.startswith('linux') and shutil.which('unshare'):
            # Fixed bootstrap, never interpolated project text. Loopback is optional.
            self.prefix = [shutil.which('unshare'), '-Urn', 'sh', '-c', 'ip link set lo up 2>/dev/null || true; exec "$@"', 'tsmax-offline']
            self.name = 'linux-network-namespace'
        else:
            self.error = 'OS network isolation is unavailable.'
            return False
        self.temporary_home = tempfile.TemporaryDirectory(prefix='tsmax-verify-')
        env = self.environment(Path(self.temporary_home.name))
        probe = _execute([*self.prefix, sys.executable, '-I', '-S', '-c', 'pass'], root, env, 5)
        if probe['status'] != 'passed':
            self.error = 'OS network isolation could not be established.'
            return False
        return True

    def close(self):
        if self.temporary_home is not None:
            self.temporary_home.cleanup()
            self.temporary_home = None

    @staticmethod
    def environment(home):
        # Do not propagate credential values, proxies, preloaders or startup hooks.
        return {'PATH': os.environ.get('PATH', os.defpath), 'HOME': str(home), 'LANG': 'C.UTF-8',
                'TERM': 'dumb', 'CI': '1', 'NO_COLOR': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                'PIP_NO_INDEX': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1',
                'npm_config_offline': 'true', 'npm_config_update_notifier': 'false', 'npm_config_audit': 'false'}

    def run(self, check, root, home, timeout):
        isolated_home = Path(self.temporary_home.name)
        resolved = shutil.which(check.argv[0], path=self.environment(isolated_home)['PATH'])
        if not resolved:
            return {'status': 'incomplete', 'reason': 'required executable is missing', 'exit_code': None, 'output': '', 'duration_s': 0.0}
        st = Path(resolved).stat()
        identity = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        result = _execute([*self.prefix, resolved, *check.argv[1:]], root, self.environment(isolated_home), timeout)
        try:
            after = Path(resolved).stat()
            if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                result.update(status='incomplete', reason='tool changed during verification')
        except OSError:
            result.update(status='incomplete', reason='tool disappeared during verification')
        result['tool'] = {'executable': resolved, 'identity_kind': 'filesystem-metadata-not-version',
                          'device': st.st_dev, 'inode': st.st_ino, 'ctime_ns': st.st_ctime_ns,
                          'size': st.st_size, 'mtime_ns': st.st_mtime_ns,
                          'python_version': sys.version.split()[0] if resolved == sys.executable else None}
        return result


def _classify(check, result):
    output = result['output']
    if result['status'] == 'incomplete':
        return result
    if re.search(r'No module named|command not found|not recognized as an internal|could not determine executable', output, re.I):
        return {**result, 'status': 'incomplete', 'reason': 'required tool or dependency is missing'}
    if check.kind == 'test':
        if result['exit_code'] == 5 or re.search(r'no tests (?:ran|collected|found)|collected 0 items|Ran 0 tests', output, re.I):
            return {**result, 'status': 'incomplete', 'reason': 'no tests collected'}
        if re.search(r'\b[1-9]\d*\s+(?:skipped|pending|todo)\b|\bskipped\s*[=:]\s*[1-9]|^\s*#\s*(?:skip(?:ped)?|todo)\s+[1-9]\d*\b|^\s*(?:not\s+)?ok\b[^\n]*#\s*(?:skip|todo)\b', output, re.I | re.M):
            return {**result, 'status': 'incomplete', 'reason': 'required tests were skipped'}
        evidence = re.search(r'\b[1-9]\d*\s+passed\b|\bRan\s+[1-9]\d*\s+tests?\b|#\s*pass\s+[1-9]\d*\b', output, re.I)
        if result['status'] == 'passed' and not evidence:
            return {**result, 'status': 'incomplete', 'reason': 'no supported evidence of executed tests (pytest, unittest, Jest/Vitest or TAP)'}
    return result


def _check_inputs(root, checks):
    """Pin direct file arguments; refuse paths outside the selected project.

    This is not a shell/code interpreter: dynamically loaded files remain a
    limitation of authorized project code, not a hermetic-build guarantee.
    """
    files = set()
    for check in checks:
        skip_code = False
        for argument in check.argv[1:]:
            if skip_code:
                skip_code = False
                continue
            if argument in {'-c', '-e', '--eval'}:
                skip_code = True
                continue
            value = argument.split('=', 1)[1] if argument.startswith('-') and '=' in argument else argument
            if value.startswith('-') or re.match(r'^[a-zA-Z]+://', value):
                continue
            path = Path(value)
            if not (path.is_absolute() or value.startswith('../') or path.suffix.lower() in pm.TEXT_SUFFIXES):
                continue
            candidate = pm._absolute(path if path.is_absolute() else root / path)
            try:
                relative = candidate.relative_to(root).as_posix()
            except ValueError:
                return (), 'A direct check script/configuration argument is outside the project; select a project-local input.'
            files.add(relative)
    return tuple(sorted(files)), None


def is_current(root: Path, home: Path, result: dict, checks: list[Check] | None = None) -> bool:
    """Revalidate point-in-time evidence immediately before downstream use.

    A true value certifies this observation only, never future stability or
    quiescence of detached processes. Callers still check apply preimages.
    """
    if result.get('status') != 'passed' or not result.get('accepted_fingerprint'):
        return False
    root, home = pm._absolute(root), pm._absolute(home)
    try:
        recorded = [Check(row['kind'], tuple(row['argv']), row['evidence']) for row in result['checks']]
        selected = checks if checks is not None else recorded
        if [check.json() for check in selected] != result['checks']:
            return False
        extra, error = _check_inputs(root, selected)
        if error:
            return False
        current = snapshot(root, home, extra)
        if not current['complete'] or current['digest'] != result['source_fingerprint']:
            return False
        tools = [row.get('tool') for row in result['results']]
        if len(tools) != len(selected):
            return False
        for tool in tools:
            if tool is None:  # injected test runners supply no executable identity
                continue
            st = Path(tool['executable']).stat()
            if any(tool[key] != value for key, value in (('device', st.st_dev), ('inode', st.st_ino),
                   ('size', st.st_size), ('mtime_ns', st.st_mtime_ns), ('ctime_ns', st.st_ctime_ns))):
                return False
        evidence = {'source': current['digest'], 'checks': result['checks'], 'tools': tools}
        return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest() == result['accepted_fingerprint']
    except (ValueError, OSError, KeyError, TypeError):
        return False


def verify(root: Path, home: Path, selection: dict, *, authorized=False, timeout=120, runner=None, persist=True) -> dict:
    root, home = pm._absolute(root), pm._absolute(home)
    if not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError('Timeout must be between 1 and 1800 seconds.')
    checks = selection['checks']
    if not checks or len(checks) > MAX_CHECKS or not all(isinstance(c, Check) for c in checks):
        if checks:
            raise ValueError('Invalid verification check selection.')
    result = {'status': 'preview', 'checks': [c.json() for c in checks], 'unresolved': selection['unresolved'],
              'notes': selection.get('notes', []), 'results': [], 'source_fingerprint': None,
              'accepted_fingerprint': None, 'manifest_path': None, 'network_isolation': None,
              'cleanup_scope': 'process-group', 'descendant_quiescence_verified': False,
              'evidence_scope': 'eligible project inputs observed at synchronous check completion',
              'limitations': ['Only trusted synchronous checks are supported; detached/background check jobs are unsupported.',
                              'Passing does not prove filesystem containment or descendant quiescence; revalidate immediately before using the result.',
                              'Executable identity uses filesystem metadata; dynamically loaded external inputs are not fully fingerprinted.']}
    if not authorized:
        return result
    result['status'] = 'incomplete'
    input_files, input_error = _check_inputs(root, checks)
    before = snapshot(root, home, input_files)
    result['source_fingerprint'] = before['digest']
    result['scope'] = {k: v for k, v in before.items() if k != 'hashes'}
    if input_error:
        result['reason'] = input_error
    elif selection['unresolved'] or not checks:
        result['reason'] = 'Required checks need explicit selection.'
    elif not before['complete']:
        result['reason'] = 'Source snapshot is incomplete; narrow the project or resolve unsafe files.'
    else:
        runner = runner or OfflineRunner()
        if not runner.prepare(root, home):
            result['reason'] = runner.error
            if hasattr(runner, 'close'):
                runner.close()
        else:
            result['network_isolation'] = runner.name
            try:
                for check in checks:
                    ran = _classify(check, runner.run(check, root, home, timeout))
                    ran['output'] = _diagnostic(ran['output'])
                    result['results'].append({**check.json(), **ran})
                    current = snapshot(root, home, input_files)
                    if not current['complete'] or current['digest'] != before['digest']:
                        break
            finally:
                if hasattr(runner, 'close'):
                    runner.close()
            after = snapshot(root, home, input_files)
            states = {r['status'] for r in result['results']}
            if not after['complete'] or after['digest'] != before['digest']:
                result['reason'] = 'Source, test, configuration or lock inputs changed during verification.'
            elif 'incomplete' in states:
                result['reason'] = 'One or more required checks are incomplete.'
            elif 'failed' in states:
                result.update(status='failed', reason='One or more required checks failed.')
            else:
                evidence = {'source': before['digest'], 'checks': result['checks'],
                            'tools': [r.get('tool') for r in result['results']]}
                result.update(status='passed', reason=None,
                              accepted_fingerprint=hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest())
    if persist:
        pm._safe_write_paths(root, home)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-verify-' + uuid.uuid4().hex
        target = root / '.tsmax' / 'verify' / (stamp + '.json')
        applied = apply_changes(Context(root=root, home=home), [{
            'path': str(target), 'kind': 'create', 'content': json.dumps(result, indent=2) + '\n',
            'private': True, 'secure': True, 'preimage': None, 'reason': 'Record local verification evidence.'
        }], stamp=stamp, profile='verify')
        result.update(manifest_path=applied['manifest_path'], result_path=str(target))
    return result
