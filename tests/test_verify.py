"""Gate results require real check evidence, stable inputs and OS isolation."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import time
from contextlib import redirect_stdout

import pytest

from tsmax.cli import main
from tsmax.installer import undo_manifest
from tsmax import verify as v


@pytest.fixture
def project(tmp_path):
    root, home = tmp_path / 'project', tmp_path / 'home'
    root.mkdir()
    home.mkdir()
    (root / 'app.py').write_text('answer = 42\n')
    return root, home


class FakeRunner:
    name = 'test-os-isolation'
    error = 'isolation unavailable'

    def __init__(self, outputs=None, mutate=None, available=True):
        self.outputs = list(outputs or [])
        self.mutate = mutate
        self.available = available
        self.calls = []

    def prepare(self, root, home):
        return self.available

    def run(self, check, root, home, timeout):
        self.calls.append(check)
        if self.mutate:
            self.mutate(root)
        return self.outputs.pop(0) if self.outputs else passed()


def passed(output='1 passed in 0.1s', **overrides):
    return {'status': 'passed', 'reason': None, 'exit_code': 0, 'output': output, 'duration_s': .1, **overrides}


def selection(*checks):
    return {'checks': list(checks or [v.Check('test', ('python3', '-m', 'pytest'))]), 'unresolved': [], 'notes': []}


def test_preview_never_launches_or_writes(project):
    root, home = project
    runner = FakeRunner()
    result = v.verify(root, home, selection(), runner=runner)
    assert result['status'] == 'preview'
    assert runner.calls == []
    assert not (root / '.tsmax').exists()
    assert not (home / '.tsmax').exists()


def test_detection_python_and_node_preserves_configured_checks(project):
    root, home = project
    (root / 'package.json').write_text(json.dumps({'scripts': {'test': 'vitest run', 'lint': 'eslint .', 'typecheck': 'tsc --noEmit'}}))
    result = v.detect(root, home)
    assert [c.kind for c in result['checks']] == ['test', 'lint', 'typecheck']
    assert result['checks'][0].argv == ('npm', '--offline', 'run', 'test')
    assert result['unresolved'] == []
    (root / 'package.json').unlink()
    (root / 'pyproject.toml').write_text('[tool.pytest.ini_options]\ntestpaths=["tests"]\n[tool.ruff]\nline-length=100\n[tool.mypy]\nstrict=true\n')
    result = v.detect(root, home)
    assert [c.kind for c in result['checks']] == ['test', 'lint', 'typecheck']


def test_detection_ambiguous_or_missing_tools_needs_selection(project):
    root, home = project
    (root / 'package.json').write_text(json.dumps({'scripts': {'test': 'a', 'test:unit': 'b'}, 'devDependencies': {'typescript': '1', 'eslint': '1'}}))
    result = v.detect(root, home)
    assert {s.split(':')[0] for s in result['unresolved']} == {'test', 'lint', 'typecheck'}
    explicit = [v.Check('test', ('node', 'tests.js'))]
    selected = v.select(result, explicit)
    assert {s.split(':')[0] for s in selected['unresolved']} == {'lint', 'typecheck'}
    assert selected['checks'] == explicit


def test_configuration_no_follow_and_credentials_not_read(project, monkeypatch):
    root, home = project
    (root / 'auth.json').write_text('CANARY_CREDENTIAL_DO_NOT_READ')
    (root / '.env').write_text('CANARY_CREDENTIAL_DO_NOT_READ')
    (root / 'package.json').symlink_to(root / 'auth.json')
    real_read = os.read
    opened = []
    real_open = os.open
    def tracked(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(os, 'open', tracked)
    with pytest.raises(ValueError, match='safely read'):
        v.detect(root, home)
    assert not any(Path(p).name in {'auth.json', '.env'} for p in opened)
    (root / 'package.json').unlink()
    snap = v.snapshot(root, home)
    assert 'auth.json' not in snap['hashes'] and '.env' not in snap['hashes']


def test_lock_and_source_changes_change_fingerprint(project):
    root, home = project
    (root / 'package-lock.json').write_text('{"v":1}')
    before = v.snapshot(root, home)
    assert 'package-lock.json' in before['hashes']
    (root / 'package-lock.json').write_text('{"v":2}')
    assert v.snapshot(root, home)['digest'] != before['digest']


def test_success_is_private_and_undoable(project):
    root, home = project
    result = v.verify(root, home, selection(), authorized=True, runner=FakeRunner())
    assert result['status'] == 'passed' and result['accepted_fingerprint']
    target = Path(result['result_path'])
    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(target.read_text())['accepted_fingerprint'] == result['accepted_fingerprint']
    undo_manifest(Path(result['manifest_path']))
    assert not target.exists()


@pytest.mark.parametrize('output,exit_code,reason', [
    ('no tests ran in 0.1s', 5, 'no tests collected'),
    ('1 passed, 1 skipped', 0, 'required tests were skipped'),
    ('Ran 0 tests\nOK', 0, 'no tests collected'),
    ('Ran 2 tests\nOK (skipped=1)', 0, 'required tests were skipped'),
    ('All good!', 0, 'no supported evidence'),
    ('No module named pytest', 1, 'required tool or dependency'),
])
def test_incomplete_checks_cannot_pass(project, output, exit_code, reason):
    result = v.verify(*project, selection(), authorized=True, runner=FakeRunner([passed(output, exit_code=exit_code)]), persist=False)
    assert result['status'] == 'incomplete'
    assert reason in result['results'][0]['reason']
    assert result['accepted_fingerprint'] is None


def test_failing_check_is_failure_not_incomplete(project):
    result = v.verify(*project, selection(), authorized=True,
                      runner=FakeRunner([passed('1 failed', status='failed', exit_code=1)]), persist=False)
    assert result['status'] == 'failed'


def test_input_drift_stops_checks_and_invalidates_result(project):
    checks = selection(v.Check('test', ('python', '-m', 'pytest')), v.Check('lint', ('ruff', 'check', '.')))
    runner = FakeRunner(mutate=lambda root: (root / 'app.py').write_text('changed=True'))
    result = v.verify(*project, checks, authorized=True, runner=runner, persist=False)
    assert len(runner.calls) == 1
    assert result['status'] == 'incomplete' and result['accepted_fingerprint'] is None
    assert 'changed during' in result['reason']


def test_isolation_unavailable_never_runs(project):
    runner = FakeRunner(available=False)
    result = v.verify(*project, selection(), authorized=True, runner=runner, persist=False)
    assert result['status'] == 'incomplete' and runner.calls == []


def test_incomplete_snapshot_refuses_execution(project):
    root, home = project
    (root / '.gitignore').write_text('unsupported\\ pattern\n')
    runner = FakeRunner()
    result = v.verify(root, home, selection(), authorized=True, runner=runner, persist=False)
    assert result['status'] == 'incomplete' and runner.calls == []


def test_diagnostics_redact_secret_values(project):
    result = v.verify(*project, selection(), authorized=True,
                      runner=FakeRunner([passed('1 passed\npassword=abc123\nsk-testabcdefghijkl\nCANARY_CREDENTIAL_DO_NOT_READ')]), persist=False)
    output = json.dumps(result)
    assert 'abc123' not in output and 'sk-testabcdefghijkl' not in output and 'CANARY_CREDENTIAL' not in output


def test_cli_requires_explicit_run_and_valid_argv(project):
    root, home = project
    out = io.StringIO()
    with redirect_stdout(out):
        status = main(['verify', '--root', str(root), '--home', str(home), '--check', 'test', '["python3","test.py"]', '--json'])
    assert status == 0 and json.loads(out.getvalue())['status'] == 'preview'
    with pytest.raises(ValueError):
        v.Check('test', ('python\nBAD',))


def real_runner(project):
    runner = v.OfflineRunner()
    if not runner.prepare(*project):
        runner.close()
        pytest.skip('OS network isolation unavailable; real subprocess verification unverified')
    return runner


def test_real_offline_subprocess_and_child_block(project):
    runner = real_runner(project)
    try:
        # All connection attempts occur under mandatory OS isolation, including children.
        script = 'import socket,subprocess,sys; p=subprocess.run([sys.executable,"-c","import socket; socket.create_connection((\'192.0.2.1\',443),timeout=.2)"],capture_output=True); assert p.returncode != 0; print("1 passed")'
        result = runner.run(v.Check('test', (sys.executable, '-I', '-S', '-c', script)), *project, 5)
        assert result['status'] == 'passed' and '1 passed' in result['output']
    finally:
        runner.close()


def test_real_timeout_output_bounds_and_environment(project, monkeypatch):
    monkeypatch.setenv('SECRET_TOKEN', 'CANARY_SECRET_TOKEN')
    runner = real_runner(project)
    try:
        result = runner.run(v.Check('test', (sys.executable, '-I', '-S', '-c', 'import time;time.sleep(10)')), *project, .2)
        assert result['status'] == 'incomplete' and result['reason'] == 'timeout'
        result = runner.run(v.Check('test', (sys.executable, '-I', '-S', '-c', 'print("X"*100000)')), *project, 5)
        assert result['status'] == 'incomplete' and len(result['output']) <= v.MAX_OUTPUT
        result = runner.run(v.Check('test', (sys.executable, '-I', '-S', '-c', 'import os;assert "SECRET_TOKEN" not in os.environ;print("1 passed")')), *project, 5)
        assert result['status'] == 'passed'
    finally:
        runner.close()


def test_ignored_check_configuration_and_lock_still_bound(project):
    root, home = project
    (root / '.gitignore').write_text('package-lock.json\npytest.ini\n')
    (root / 'package-lock.json').write_text('{}')
    (root / 'pytest.ini').write_text('[pytest]\n')
    before = v.snapshot(root, home)
    assert {'package-lock.json', 'pytest.ini'} <= before['hashes'].keys()
    (root / 'pytest.ini').write_text('[pytest]\naddopts = -x\n')
    assert v.snapshot(root, home)['digest'] != before['digest']


def test_oversized_inputs_and_linked_result_path_refused(project):
    root, home = project
    (root / 'large.py').write_bytes(b'x' * (v.pm.MAX_FILE_BYTES + 1))
    assert not v.snapshot(root, home)['complete']
    (root / 'large.py').unlink()
    (root / '.tsmax').mkdir()
    (root / '.tsmax/verify').symlink_to(home, target_is_directory=True)
    with pytest.raises(OSError):
        v.verify(root, home, selection(), authorized=True, runner=FakeRunner())
    assert not list(home.glob('*verify*.json'))


def test_explicit_acceptance_uses_exit_status(project):
    result = v.verify(*project, selection(v.Check('acceptance', ('custom-check',))),
                      authorized=True, runner=FakeRunner([passed('done')]), persist=False)
    assert result['status'] == 'passed'


def test_external_direct_script_rejected_without_read(project):
    root, home = project
    script = home / 'check.py'
    script.write_text('raise SystemExit(0)')
    runner = FakeRunner()
    result = v.verify(root, home, selection(v.Check('test', (sys.executable, str(script)))),
                      authorized=True, runner=runner, persist=False)
    assert result['status'] == 'incomplete' and not runner.calls
    assert 'outside the project' in result['reason']


def test_ignored_direct_script_is_bound(project):
    root, home = project
    (root / '.gitignore').write_text('check.py\n')
    (root / 'check.py').write_text('print("1 passed")')
    runner = FakeRunner(mutate=lambda root: (root / 'check.py').write_text('print("2 passed")'))
    result = v.verify(root, home, selection(v.Check('test', (sys.executable, 'check.py'))),
                      authorized=True, runner=runner, persist=False)
    assert result['status'] == 'incomplete' and result['accepted_fingerprint'] is None


def test_timeout_terminates_child_process_group(project):
    root, home = project
    marker = home / 'child-survived.txt'
    child = f'import time;from pathlib import Path;time.sleep(.8);Path({str(marker)!r}).write_text("alive")'
    parent = f'import subprocess,sys,time;subprocess.Popen([sys.executable,"-I","-S","-c",{child!r}]);time.sleep(10)'
    runner = real_runner(project)
    try:
        result = runner.run(v.Check('test', (sys.executable, '-I', '-S', '-c', parent)), root, home, .2)
        assert result['status'] == 'incomplete' and result['reason'] == 'timeout'
        time.sleep(.9)
        assert not marker.exists()
    finally:
        runner.close()


@pytest.mark.parametrize('content', [
    'project = 1\n', '[project]\ndependencies = 7\n',
    '[project]\ndependencies = "pytest"\n', '[project]\noptional-dependencies = 1\n',
    '[project.optional-dependencies]\ndev = "pytest"\n', '[project]\ndependencies = [1]\n',
])
def test_malformed_python_metadata_refuses_without_traceback(project, content):
    root, home = project
    (root / 'pyproject.toml').write_text(content)
    with pytest.raises(ValueError, match='Python'):
        v.detect(root, home)
    out = io.StringIO()
    with redirect_stdout(out):
        status = main(['verify', '--root', str(root), '--home', str(home), '--json'])
    assert status == 2 and json.loads(out.getvalue())['status'] == 'incomplete'


def test_freshness_rechecks_selected_commands_and_late_source_drift(project):
    root, home = project
    chosen = selection()
    result = v.verify(root, home, chosen, authorized=True, runner=FakeRunner(), persist=False)
    assert v.is_current(root, home, result, chosen['checks'])
    assert not v.is_current(root, home, result, [v.Check('test', ('different-command',))])
    (root / 'app.py').write_text('late_change = True\n')
    assert not v.is_current(root, home, result)


def test_detached_child_is_unsupported_and_late_write_invalidates_evidence(project):
    """Synchronize a real detached write, rather than assuming a quiet sleep proves cleanup."""
    import signal
    root, home = project
    pid_file, release, done = home / 'pid.txt', home / 'release.txt', home / 'done.txt'
    child = (
        'import os,time;from pathlib import Path;'
        f'Path({str(pid_file)!r}).write_text(str(os.getpid()));'
        f'go=Path({str(release)!r});deadline=time.monotonic()+10\n'
        'while not go.exists():\n'
        ' if time.monotonic()>deadline: raise SystemExit(2)\n'
        ' time.sleep(.01)\n'
        f'Path({str(root / "app.py")!r}).write_text("late=True\\n");'
        f'Path({str(done)!r}).write_text("done")'
    )
    parent = (
        'import subprocess,sys,time;from pathlib import Path;'
        f'subprocess.Popen([sys.executable,"-I","-S","-c",{child!r}],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);'
        f'ready=Path({str(pid_file)!r});deadline=time.monotonic()+5\n'
        'while not ready.exists():\n'
        ' if time.monotonic()>deadline: raise SystemExit(2)\n'
        ' time.sleep(.01)\n'
        'print("1 passed")'
    )
    probe = real_runner(project)
    probe.close()
    pid = None
    try:
        result = v.verify(root, home, selection(v.Check('test', (sys.executable, '-I', '-S', '-c', f'exec({parent!r})'))),
                          authorized=True, persist=False)
        assert result['status'] == 'passed'
        pid = int(pid_file.read_text())
        assert result['cleanup_scope'] == 'process-group'
        assert result['descendant_quiescence_verified'] is False
        assert v.is_current(root, home, result)
        release.write_text('go')
        deadline = time.monotonic() + 5
        while not done.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert done.exists(), 'detached fixture did not perform its coordinated write'
        assert not v.is_current(root, home, result)
    finally:
        if pid is None and pid_file.exists():
            pid = int(pid_file.read_text())
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize('output,expected', [
    ('# tests 1\n# pass 1\n# fail 0\n# skipped 0\n# todo 0', 'passed'),
    ('# tests 2\n# pass 1\n# fail 0\n# skipped 1\n# todo 0', 'incomplete'),
    ('# tests 2\n# pass 1\n# fail 0\n# skipped 0\n# todo 1', 'incomplete'),
    ('ok 1 - platform case # SKIP reason\n# pass 1', 'incomplete'),
    ('not ok 1 - unfinished # TODO reason\n# pass 1', 'incomplete'),
])
def test_tap_zero_skip_summaries_and_real_skip_directives(project, output, expected):
    result = v.verify(*project, selection(), authorized=True, runner=FakeRunner([passed(output)]), persist=False)
    assert result['status'] == expected


def test_observed_background_group_is_incomplete(project):
    root, home = project
    runner = real_runner(project)
    parent = 'import subprocess,sys;subprocess.Popen([sys.executable,"-I","-S","-c","import time;time.sleep(10)"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);print("1 passed")'
    try:
        result = runner.run(v.Check('test', (sys.executable, '-I', '-S', '-c', parent)), root, home, 5)
        assert result['status'] == 'incomplete'
        assert 'background processes' in result['reason']
    finally:
        runner.close()
