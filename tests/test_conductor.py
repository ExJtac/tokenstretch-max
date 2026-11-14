"""Provider-neutral Conductor state, authorization, retry and review invariants."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
from types import ModuleType

import pytest

from tsmax import conductor as c
from tsmax import verify
from tsmax.adapters.base import Adapter, ConductorCapabilities, ConductorResponse
from tsmax.context import Context
from tsmax.installer import apply_changes, undo_manifest


def task(identifier='T1', files=None, dependencies=None):
    return {'id': identifier, 'title': 'Change the answer', 'spec': 'Change the answer to the accepted value.',
            'depends_on': dependencies or [], 'files': files or ['app.py'], 'acceptance': 'Required tests pass.',
            'checks': [{'kind': 'test', 'argv': [sys.executable, '-m', 'pytest', '-q']}]}


class AnyProvider(Adapter):
    id = 'arbitrary-local-cli'

    def __init__(self, tasks=None, effects=None):
        self.tasks = tasks or [task()]
        self.effects = list(effects or [])
        self.requests = []
        self.capability = ConductorCapabilities(provider=self.id, available=True, supported=True,
            operations=('plan', 'task'), tiers={'light': 'small-v1', 'mid': 'balanced-v1', 'top': 'strong-v1'},
            native_login=True, fresh_session=True, read_only_plan=True, workspace_task=True)

    def conductor_capabilities(self, ctx):
        return self.capability

    def run_conductor(self, ctx, request, runner=None):
        self.requests.append(request)
        assert request.authorized
        # Dispatch has already been durably recorded before provider code runs.
        original = self.original
        state = json.loads((original / c.STATE_JSON).read_text())
        assert state['status'] in {'planning-dispatched', 'dispatched'}
        if request.operation == 'plan':
            return ConductorResponse('completed', json.dumps({'tasks': self.tasks}), usage=None)
        effect = self.effects.pop(0) if self.effects else lambda path: (path / 'app.py').write_text('answer = 43\n')
        response = effect(request.cwd)
        return response if isinstance(response, ConductorResponse) else ConductorResponse('completed', 'Done. Provider claims are not gate evidence.', usage=None)


class Gate:
    name = 'fixture-offline'
    error = 'unavailable'

    def __init__(self, statuses=None):
        self.statuses = list(statuses or ['passed'])
        self.calls = 0

    def prepare(self, root, home):
        return True

    def run(self, check, root, home, timeout):
        self.calls += 1
        status = self.statuses.pop(0) if self.statuses else 'passed'
        return {'status': status, 'exit_code': 0 if status == 'passed' else 1, 'duration_s': .01,
                'output': '1 passed' if status == 'passed' else '1 failed: expected 43',
                'reason': None if status != 'incomplete' else 'missing required check'}


@pytest.fixture
def world(tmp_path, monkeypatch):
    root, home = tmp_path / 'original', tmp_path / 'home'
    root.mkdir()
    home.mkdir()
    (root / 'app.py').write_text('answer = 42\n')
    (root / 'unrelated.py').write_text('keep = True\n')
    fake = ModuleType('tsmax.conductor_git')

    def snapshot(stage, home):
        return verify.snapshot(stage, home)['hashes']

    def prepare(root, home):
        stage = tmp_path / 'worktree'
        stage.mkdir()
        for name in snapshot(root, home):
            destination = stage / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((root / name).read_bytes())
        return {'path': str(stage), 'commit': 'a' * 40, 'baseline': snapshot(stage, home)}

    def check_original(root, home, state, files):
        now = snapshot(root, home)
        for name in files:
            if now.get(name) != state['baseline'].get(name):
                raise ValueError('Original task input drifted.')

    def inspect(stage, home, allowed, baseline):
        current = snapshot(stage, home)
        changed = [name for name in current.keys() | baseline.keys() if current.get(name) != baseline.get(name)]
        if set(changed) - set(allowed):
            raise ValueError('out of scope')
        changes = [{'path': name, 'before': baseline.get(name), 'after': current.get(name),
                    'content': (stage / name).read_text()} for name in sorted(changed)]
        return {'changes': changes, 'allowed_files': allowed, 'diff': json.dumps(changes), 'digest': c._digest(changes)}

    def apply(root, home, stage, review):
        changes = [{'path': str(root / row['path']), 'content': row['content'], 'secure': True,
                    'private': True, 'preimage': row['before']} for row in review['changes']]
        return apply_changes(Context(root=root, home=home), changes, stamp=c._stamp('apply'), profile='test-conductor-apply')

    def accept(state, stage, home, files):
        result = copy.deepcopy(state)
        current = snapshot(stage, home)
        for name in files:
            if name in current:
                result['baseline'][name] = current[name]
            else:
                result['baseline'].pop(name, None)
        return result

    fake.snapshot, fake.prepare, fake.inspect = snapshot, prepare, inspect
    fake.check_original, fake.apply_review, fake.accept = check_original, apply, accept
    monkeypatch.setitem(sys.modules, 'tsmax.conductor_git', fake)
    import tsmax
    monkeypatch.setattr(tsmax, 'conductor_git', fake, raising=False)
    return root, home


def make_plan(world, provider=None):
    provider = provider or AnyProvider()
    provider.original = world[0]
    result = c.plan(*world, 'Update the answer.', provider.id, authorized=True, adapter=provider)
    assert result['status'] == 'plan-ready'
    return provider


def test_plan_preview_never_calls_provider_or_writes(world):
    provider = AnyProvider()
    result = c.plan(*world, 'Update answer', provider.id, adapter=provider)
    assert result['status'] == 'preview' and provider.requests == []
    assert not (world[0] / '.tsmax').exists()


def test_plan_persists_structured_reviewable_private_files(world):
    provider = make_plan(world)
    root, home = world
    document = c.load_plan(root, home)
    assert document['tool'] == 'arbitrary-local-cli'
    assert document['usage'] is None
    assert (root / c.PLAN_MD).stat().st_mode & 0o777 == 0o600
    assert 'Required commands' in (root / c.PLAN_MD).read_text()
    assert provider.requests[0].tier == 'top'


@pytest.mark.parametrize('mutate', [
    lambda row: row.update(files=['../outside.py']),
    lambda row: row.update(files=['.env']),
    lambda row: row.update(depends_on=['T2']),
    lambda row: row.update(checks=[]),
    lambda row: row.update(extra='unexpected'),
])
def test_untrusted_plan_output_is_validated(world, mutate):
    row = task()
    mutate(row)
    provider = AnyProvider([row])
    provider.original = world[0]
    with pytest.raises(c.ConductorError):
        c.plan(*world, 'Update answer', provider.id, authorized=True, adapter=provider)
    assert json.loads((world[0] / c.STATE_JSON).read_text())['status'] == 'needs-review'
    assert not (world[0] / c.PLAN_JSON).exists()


def test_execution_preview_does_not_dispatch(world):
    provider = make_plan(world)
    result = c.execute(*world, adapter=provider)
    assert result['status'] == 'preview' and len(provider.requests) == 1
    assert not (world[0].parent / 'worktree').exists()


def test_success_requires_gate_and_exact_review_apply_then_undo(world):
    provider = make_plan(world)
    root, home = world
    result = c.execute(root, home, authorized=True, max_calls=3, adapter=provider, gate_runner=Gate())
    assert result['status'] == 'review-ready'
    assert (root / 'app.py').read_text() == 'answer = 42\n'
    assert result['review']['gate']['status'] == 'passed'
    with pytest.raises(c.ConductorError, match='exact task'):
        c.apply_review(root, home, 'T1', 'wrong-token')
    applied = c.apply_review(root, home, 'T1', result['review']['token'], gate_runner=Gate())
    assert applied['status'] == 'complete'
    assert (root / 'app.py').read_text() == 'answer = 43\n'
    assert (root / 'unrelated.py').read_text() == 'keep = True\n'
    undo_manifest(Path(applied['manifest_path']))
    assert (root / 'app.py').read_text() == 'answer = 42\n'


def test_two_failed_cheap_attempts_escalate_once_without_file_history(world):
    provider = make_plan(world)
    result = c.execute(*world, authorized=True, max_calls=3, adapter=provider, gate_runner=Gate(['failed', 'failed', 'passed']))
    assert result['status'] == 'review-ready'
    requests = provider.requests[1:]
    assert [r.tier for r in requests] == ['mid', 'mid', 'top']
    assert all(r.operation == 'task' and r.cwd != world[0] for r in requests)
    assert 'FILES:\n' in requests[0].prompt
    assert 'FILES:\n' not in requests[2].prompt and 'FAILURES:\n' in requests[2].prompt
    assert 'Provider claims' not in requests[2].prompt
    assert all(len(r.prompt) <= c.MAX_PROMPT for r in requests)


def test_escalation_exhaustion_and_incomplete_stop(world):
    provider = make_plan(world)
    result = c.execute(*world, authorized=True, max_calls=3, adapter=provider, gate_runner=Gate(['failed'] * 3))
    assert result['status'] == 'needs-review' and len(provider.requests) == 4
    with pytest.raises(c.ConductorError, match='uncertain or blocked'):
        c.execute(*world, authorized=True, max_calls=3, adapter=provider, gate_runner=Gate())


@pytest.mark.parametrize('status', ['auth-required', 'quota-exhausted', 'timeout', 'interrupted', 'unsupported', 'failed'])
def test_provider_errors_stop_without_replay(world, status):
    provider = make_plan(world, AnyProvider(effects=[lambda path: ConductorResponse(status, reason='not available')]))
    result = c.execute(*world, authorized=True, max_calls=3, adapter=provider, gate_runner=Gate())
    assert result['status'] == 'needs-review' and len(provider.requests) == 2
    with pytest.raises(c.ConductorError):
        c.execute(*world, authorized=True, max_calls=3, adapter=provider)


def test_uncertain_dispatch_persists_before_exception_and_is_not_replayed(world):
    def interrupted(path):
        raise KeyboardInterrupt()
    provider = make_plan(world, AnyProvider(effects=[interrupted]))
    with pytest.raises(KeyboardInterrupt):
        c.execute(*world, authorized=True, max_calls=3, adapter=provider)
    state = json.loads((world[0] / c.STATE_JSON).read_text())
    assert state['status'] == 'dispatched' and state['calls'] == 1
    assert state['attempts'][0]['status'] == 'dispatched'
    with pytest.raises(c.ConductorError, match='uncertain'):
        c.execute(*world, authorized=True, max_calls=3, adapter=provider)


def test_scope_violation_stops_before_gate(world):
    provider = make_plan(world, AnyProvider(effects=[lambda path: (path / 'unrelated.py').write_text('oops')]))
    gate = Gate()
    result = c.execute(*world, authorized=True, max_calls=3, adapter=provider, gate_runner=gate)
    assert result['status'] == 'needs-review' and gate.calls == 0
    assert (world[0] / 'unrelated.py').read_text() == 'keep = True\n'


def test_changed_staging_content_or_original_preimage_refuses_apply(world):
    provider = make_plan(world)
    result = c.execute(*world, authorized=True, max_calls=3, adapter=provider, gate_runner=Gate())
    (Path(result['stage']) / 'app.py').write_text('changed-after-review')
    with pytest.raises(c.ConductorError, match='changed after review'):
        c.apply_review(*world, 'T1', result['review']['token'])


def test_call_limit_persists_and_cannot_reset(world):
    provider = make_plan(world)
    result = c.execute(*world, authorized=True, max_calls=1, adapter=provider, gate_runner=Gate(['failed']))
    assert result['status'] == 'call-limit' and len(provider.requests) == 2
    with pytest.raises(c.ConductorError, match='cannot be reset'):
        c.execute(*world, authorized=True, max_calls=3, adapter=provider)
    again = c.execute(*world, authorized=True, max_calls=1, adapter=provider)
    assert again['status'] == 'call-limit' and len(provider.requests) == 2


def test_dependencies_run_only_after_explicit_application(world):
    second = task('T2', ['unrelated.py'], ['T1'])
    provider = make_plan(world, AnyProvider([task(), second], effects=[
        lambda path: (path / 'app.py').write_text('answer=43\n'),
        lambda path: (path / 'unrelated.py').write_text('keep=False\n'),
    ]))
    first = c.execute(*world, authorized=True, max_calls=6, adapter=provider, gate_runner=Gate())
    again = c.execute(*world, authorized=True, max_calls=6, adapter=provider, gate_runner=Gate())
    assert first['review']['token'] == again['review']['token'] and len(provider.requests) == 2
    assert c.apply_review(*world, 'T1', first['review']['token'], gate_runner=Gate())['status'] == 'ready'
    second_result = c.execute(*world, authorized=True, max_calls=6, adapter=provider, gate_runner=Gate())
    assert second_result['review']['task'] == 'T2'
    assert c.apply_review(*world, 'T2', second_result['review']['token'], gate_runner=Gate())['status'] == 'complete'


def test_lock_blocks_concurrent_or_crashed_state_writer(world):
    root, home = world
    (root / '.tsmax').mkdir()
    (root / '.tsmax/conductor.lock').write_text('{"pid":1}')
    with pytest.raises(c.ConductorError, match='holds the local lock'):
        c.plan(root, home, 'goal', AnyProvider.id, authorized=True, adapter=AnyProvider())


def test_original_gate_failure_preserves_applied_work_and_blocks_dependents(world):
    provider = make_plan(world, AnyProvider([task(), task('T2', ['unrelated.py'], ['T1'])]))
    result = c.execute(*world, authorized=True, max_calls=6, adapter=provider, gate_runner=Gate())
    applied = c.apply_review(*world, 'T1', result['review']['token'], gate_runner=Gate(['failed']))
    assert applied['status'] == 'needs-review'
    assert (world[0] / 'app.py').read_text() == 'answer = 43\n'
    state = json.loads((world[0] / c.STATE_JSON).read_text())
    assert state['tasks']['T1'] == 'applied-needs-review'
    assert state['applied'][0]['manifest_path'] == applied['manifest_path']
    with pytest.raises(c.ConductorError, match='uncertain or blocked'):
        c.execute(*world, authorized=True, max_calls=6, adapter=provider)


def test_planning_and_worker_maps_are_really_built(world):
    provider = make_plan(world)
    assert (world[0] / '.tsmax/map.sqlite3').is_file()
    result = c.execute(*world, authorized=True, max_calls=3, adapter=provider, gate_runner=Gate())
    assert (Path(result['stage']) / '.tsmax/map.sqlite3').is_file()
    assert 'No current project map' not in provider.requests[1].prompt


def test_required_detected_commands_are_frozen_in_reviewed_plan(world):
    root, home = world
    (root / 'pyproject.toml').write_text('[tool.ruff]\nline-length=100\n')
    provider = make_plan(world)
    document = c.load_plan(root, home)
    assert {row['kind'] for row in document['tasks'][0]['checks']} == {'test', 'lint'}
    assert 'ruff' in (root / c.PLAN_MD).read_text()


def test_required_check_script_cannot_be_a_worker_target(world):
    row = task()
    row['checks'][0]['argv'] = [sys.executable, 'app.py']
    provider = AnyProvider([row])
    provider.original = world[0]
    with pytest.raises(c.ConductorError):
        c.plan(*world, 'Update answer', provider.id, authorized=True, adapter=provider)


def test_changed_model_tiers_require_fresh_review(world):
    from dataclasses import replace
    provider = make_plan(world)
    provider.capability = replace(provider.capability, tiers={'mid': 'different', 'top': 'strong-v1'})
    with pytest.raises(c.ConductorError, match='model tiers changed'):
        c.execute(*world, authorized=True, max_calls=3, adapter=provider)


def test_real_git_worktree_plan_execute_review_apply_preserves_dirty_work(tmp_path, monkeypatch):
    from tsmax import conductor_git as real_git
    from test_conductor_git import git as fixture_git
    root, home, runtime = tmp_path / 'source', tmp_path / 'home', tmp_path / 'runtime'
    root.mkdir(); home.mkdir(); runtime.mkdir()
    monkeypatch.setattr(real_git.tempfile, 'gettempdir', lambda: str(runtime))
    (root / 'app.py').write_text('answer = 42\n')
    (root / 'unrelated.py').write_text('original = True\n')
    fixture_git(root, 'init', '--template=')
    fixture_git(root, 'add', '.')
    fixture_git(root, 'commit', '-m', 'synthetic source')
    (root / 'unrelated.py').write_text('user_dirty_work = True\n')
    provider = make_plan((root, home))
    result = c.execute(root, home, authorized=True, max_calls=3, adapter=provider, gate_runner=Gate())
    assert result['status'] == 'review-ready'
    assert (root / 'app.py').read_text() == 'answer = 42\n'
    assert (Path(result['stage']) / 'app.py').read_text() == 'answer = 43\n'
    applied = c.apply_review(root, home, 'T1', result['review']['token'], gate_runner=Gate())
    assert applied['status'] == 'complete'
    assert applied['original_gate']['status'] == 'passed'
    assert (root / 'app.py').read_text() == 'answer = 43\n'
    assert (root / 'unrelated.py').read_text() == 'user_dirty_work = True\n'
    undo_manifest(Path(applied['manifest_path']))
    assert (root / 'app.py').read_text() == 'answer = 42\n'
    assert (root / 'unrelated.py').read_text() == 'user_dirty_work = True\n'


@pytest.mark.parametrize('path', ['assets/logo.png', 'assets/font.woff2', 'assets/PHOTO.JPEG'])
def test_binary_assets_cannot_be_worker_edit_targets(world, path):
    root, home = world
    with pytest.raises(c.ConductorError, match='Binary assets are read-only'):
        c.validate_plan({'tasks': [task(files=[path])]}, root, home)
    with pytest.raises(c.ConductorError, match='Binary asset contents'):
        c._excerpts(root, home, [path])


def test_svg_asset_is_text_editable_but_secret_names_remain_excluded(world):
    root, home = world
    (root / 'logo.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>')
    plan = c.validate_plan({'tasks': [task(files=['logo.svg'])]}, root, home)
    assert plan['tasks'][0]['files'] == ['logo.svg']
    assert c._excerpts(root, home, ['logo.svg'])['logo.svg'].startswith('<svg')
    with pytest.raises(c.ConductorError, match='never secrets'):
        c.validate_plan({'tasks': [task(files=['secret.svg'])]}, root, home)


def test_prepare_creates_retained_stage_without_worker_authorization(world):
    provider = make_plan(world)
    prepared = c.prepare(*world)
    assert prepared['status'] == 'prepared'
    assert prepared['paid_call_authorized'] is False
    assert prepared['calls'] == 0 and prepared['call_limit'] is None
    assert len(provider.requests) == 1
    again = c.prepare(*world)
    assert again['stage'] == prepared['stage'] and again['calls'] == 0
    result = c.execute(*world, authorized=True, max_calls=3, adapter=provider, gate_runner=Gate())
    assert result['status'] == 'review-ready' and result['stage'] == prepared['stage']
    assert len(provider.requests) == 2


def test_missing_node_dependencies_refuse_before_spending_worker_calls(world):
    root, home = world
    (root / 'package.json').write_text(json.dumps({'dependencies': {'fixture-lib': '1.0.0'}}))
    provider = make_plan(world)
    result = c.execute(root, home, authorized=True, max_calls=3, adapter=provider, gate_runner=Gate())
    assert result['status'] == 'needs-preparation' and result['calls'] == 0
    assert len(provider.requests) == 1
    assert 'never installs' in result['reason']
    state = json.loads((root / c.STATE_JSON).read_text())
    assert state['call_limit'] == 3 and state['attempts'] == []
    stage = Path(result['stage'])
    (stage / 'node_modules').mkdir()  # synthetic explicit manual preparation, no install/copy
    prepared = c.prepare(root, home)
    assert prepared['status'] == 'prepared' and prepared['call_limit'] == 3
    with pytest.raises(c.ConductorError, match='cannot be reset'):
        c.execute(root, home, authorized=True, max_calls=4, adapter=provider)
    resumed = c.execute(root, home, authorized=True, max_calls=3, adapter=provider, gate_runner=Gate())
    assert resumed['status'] == 'review-ready' and len(provider.requests) == 2
    state = json.loads((root / c.STATE_JSON).read_text())
    assert state['calls'] == 1 and len(state['attempts']) == 1


def test_prepared_node_modules_cannot_be_shared_by_symlink(world):
    root, home = world
    (root / 'package.json').write_text(json.dumps({'devDependencies': {'fixture-tool': '1.0.0'}}))
    provider = make_plan(world)
    prepared = c.prepare(root, home)
    assert prepared['status'] == 'needs-preparation'
    external = home / 'external-modules'
    external.mkdir()
    (Path(prepared['stage']) / 'node_modules').symlink_to(external, target_is_directory=True)
    result = c.execute(root, home, authorized=True, max_calls=3, adapter=provider)
    assert result['status'] == 'needs-preparation' and len(provider.requests) == 1


def test_preparation_cannot_change_bound_source_or_original_inputs(world):
    provider = make_plan(world)
    prepared = c.prepare(*world)
    (Path(prepared['stage']) / 'app.py').write_text('unreviewed source edit')
    result = c.execute(*world, authorized=True, max_calls=3, adapter=provider)
    assert result['status'] == 'needs-preparation' and len(provider.requests) == 1
    assert 'source or metadata changed' in result['reason']


def test_preparation_never_replays_uncertain_dispatch(world):
    provider = make_plan(world, AnyProvider(effects=[lambda path: (_ for _ in ()).throw(KeyboardInterrupt())]))
    with pytest.raises(KeyboardInterrupt):
        c.execute(*world, authorized=True, max_calls=3, adapter=provider)
    with pytest.raises(c.ConductorError, match='cannot replay or reset'):
        c.prepare(*world)
    assert len(provider.requests) == 2


def test_solution_note_is_bounded_redacted_and_never_a_gate(world):
    def completed(path):
        (path / 'app.py').write_text('answer = 43\n')
        return ConductorResponse('completed', 'password=hidden sk-testabcdefghijkl ' + 'approach ' * 1000)
    provider = make_plan(world, AnyProvider(effects=[completed]))
    c.execute(*world, authorized=True, max_calls=3, adapter=provider, gate_runner=Gate())
    state = json.loads((world[0] / c.STATE_JSON).read_text())
    note = state['attempts'][0]['solution_note']
    assert len(note) <= 4000 and 'hidden' not in note and 'sk-testabcdefghijkl' not in note
    assert state['attempts'][0]['gate']['status'] == 'passed'


def test_real_prepared_npm_bin_link_resumes_without_extra_provider_calls(tmp_path, monkeypatch):
    from tsmax import conductor_git as real_git
    from test_conductor_git import git as fixture_git
    root, home, runtime = tmp_path / 'source', tmp_path / 'home', tmp_path / 'runtime'
    root.mkdir(); home.mkdir(); runtime.mkdir()
    monkeypatch.setattr(real_git.tempfile, 'gettempdir', lambda: str(runtime))
    (root / 'app.py').write_text('answer = 42\n')
    (root / 'package.json').write_text(json.dumps({'dependencies': {'fixture-tool': '1.0.0'}}))
    fixture_git(root, 'init', '--template=')
    fixture_git(root, 'add', '.')
    fixture_git(root, 'commit', '-m', 'synthetic dependency baseline')
    provider = make_plan((root, home), AnyProvider([task(), task('T2', ['other.py'], ['T1'])]))
    prepared = c.prepare(root, home)
    assert prepared['status'] == 'needs-preparation' and len(provider.requests) == 1
    stage = Path(prepared['stage'])
    module = stage / 'node_modules' / 'fixture-tool'
    module.mkdir(parents=True)
    (module / 'cli.js').write_text('console.log("synthetic installed tool");\n')
    bins = stage / 'node_modules' / '.bin'
    bins.mkdir()
    (bins / 'fixture-tool').symlink_to('../fixture-tool/cli.js')
    checked = c.prepare(root, home)
    assert checked['status'] == 'prepared' and len(provider.requests) == 1
    assert checked['calls'] == 0 and checked['call_limit'] is None
    result = c.execute(root, home, authorized=True, max_calls=6, adapter=provider, gate_runner=Gate())
    assert result['status'] == 'review-ready' and len(provider.requests) == 2
    assert not (root / 'node_modules').exists()
    assert (root / 'app.py').read_text() == 'answer = 42\n'
    assert c.apply_review(root, home, 'T1', result['review']['token'], gate_runner=Gate())['status'] == 'ready'
    (module / 'cli.js').write_text('console.log("unreviewed dependency change");\n')
    blocked = c.execute(root, home, authorized=True, max_calls=6, adapter=provider, gate_runner=Gate())
    assert blocked['status'] == 'needs-preparation' and len(provider.requests) == 2
    assert 'previous accepted task' in blocked['reason']


def test_prepare_respects_existing_writer_lock_without_state_mutation(world):
    provider = make_plan(world)
    root, home = world
    before = (root / c.STATE_JSON).read_bytes()
    manifests = sorted((home / '.tsmax/manifests').iterdir())
    (root / '.tsmax/conductor.lock').write_text('{"pid":1}')
    with pytest.raises(c.ConductorError, match='holds the local lock'):
        c.prepare(root, home)
    assert (root / c.STATE_JSON).read_bytes() == before
    assert sorted((home / '.tsmax/manifests').iterdir()) == manifests
    assert not (root.parent / 'worktree').exists()
    assert len(provider.requests) == 1


def test_completed_tasks_keep_evidence_references_not_snapshot_or_log_duplicates(world):
    provider = make_plan(world, AnyProvider([task(), task('T2', ['unrelated.py'], ['T1'])]))
    result = c.execute(*world, authorized=True, max_calls=6, adapter=provider, gate_runner=Gate(['failed', 'passed']))
    pending = json.loads((world[0] / c.STATE_JSON).read_text())
    assert 'T1' in pending['task_baselines']
    assert 'baseline' in pending['review'] and pending['review']['gate']['results']
    applied = c.apply_review(*world, 'T1', result['review']['token'], gate_runner=Gate())
    assert applied['status'] == 'ready'
    complete = json.loads((world[0] / c.STATE_JSON).read_text())
    assert 'T1' not in complete['task_baselines'] and 'review' not in complete
    for attempt in complete['attempts']:
        assert 'failure' not in attempt
        assert 'results' not in attempt['gate']
        assert Path(attempt['gate']['result_path']).is_file()
        assert 'outcomes' in attempt['gate']
    assert 'solution_note' not in complete['attempts'][0]
    assert complete['attempts'][1]['solution_note']
    original = complete['applied'][0]['original_gate']
    assert 'results' not in original and Path(original['result_path']).is_file()
    assert original['accepted_fingerprint']


def test_next_task_does_not_bless_manual_staging_edits(world):
    provider = make_plan(world, AnyProvider([task(), task('T2', ['unrelated.py'], ['T1'])]))
    first = c.execute(*world, authorized=True, max_calls=6, adapter=provider, gate_runner=Gate())
    c.apply_review(*world, 'T1', first['review']['token'], gate_runner=Gate())
    (Path(first['stage']) / 'unrelated.py').write_text('manual_unreviewed_change = True\n')
    result = c.execute(*world, authorized=True, max_calls=6, adapter=provider, gate_runner=Gate())
    assert result['status'] == 'needs-preparation'
    assert 'previous accepted task' in result['reason']
    assert len(provider.requests) == 2
