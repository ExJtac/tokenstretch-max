"""Synthetic local repositories; no provider, network, auth, or real user project access."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

import pytest

from tsmax import conductor_git as cg
from tsmax.installer import undo_manifest


def git(root, *args):
    env = {'PATH': os.defpath, 'HOME': str(root.parent/'empty-home'),
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_CONFIG_SYSTEM': os.devnull, 'LC_ALL': 'C'}
    return subprocess.run(['/usr/bin/git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                           '-C', str(root), *args], env=env, check=True, capture_output=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root, home = tmp_path/'source', tmp_path/'home'
    root.mkdir(); home.mkdir()
    runtime = tmp_path/'runtime'
    runtime.mkdir()
    monkeypatch.setattr(cg.tempfile, 'gettempdir', lambda: str(runtime))
    git(root, 'init', '--template=')
    for name, text in {'a.py': 'value = 1\n', 'b.py': 'other = 2\n',
                       'pyproject.toml': '[tool.pytest.ini_options]\n',
                       'package-lock.json': '{"lockfileVersion":3}\n'}.items():
        (root/name).write_text(text)
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'synthetic baseline')
    return root, home


def review(stage, home, allowed, baseline, monkeypatch):
    value = cg.inspect(stage, home, allowed, baseline)
    value.update(gate={'status': 'passed'}, baseline=baseline, task='T1', plan_digest='fixture')
    value['token'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    monkeypatch.setattr(cg.verify, 'is_current', lambda *args: True)
    return value


def test_prepare_never_uses_source_configuration_hooks_or_filters(repo, monkeypatch):
    root, home = repo
    (root/'a.py').write_text('dirty source is preserved\n')
    hook = root/'.git/hooks/post-checkout'
    hook.parent.mkdir(exist_ok=True)
    hook.write_text('#!/bin/sh\ntouch '+str(root/'HOOK_RAN')+'\n'); hook.chmod(0o755)
    canary = root.parent/'credential-canary'
    canary.write_text('invalid git config; opening this breaks source Git\n')
    with (root/'.git/config').open('a') as f:
        f.write('\n[include]\n path = '+str(canary)+'\n[filter "fixture"]\n smudge = touch '+str(root/'FILTER_RAN')+'\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(canary))
    read = cg.sf.read_bytes
    forbidden = {root/'.git/config', canary, hook}
    def guarded(path, *args, **kwargs):
        assert Path(path) not in forbidden
        return read(path, *args, **kwargs)
    monkeypatch.setattr(cg.sf, 'read_bytes', guarded)
    state = cg.prepare(root, home)
    stage = Path(state['path'])
    assert (stage/'a.py').read_text() == 'value = 1\n'
    assert (root/'a.py').read_text() == 'dirty source is preserved\n'
    assert not (root/'HOOK_RAN').exists() and not (root/'FILTER_RAN').exists()
    assert stage.is_relative_to(root.parent/'runtime')
    assert (stage/'package-lock.json').exists()
    assert stat.S_IMODE(stage.stat().st_mode) == 0o700


def test_prepare_excludes_secrets_symlinks_ignored_lfs_and_binary(repo):
    root, home = repo
    (root/'.env').write_text('SYNTHETIC_SECRET=fixture\n')
    (root/'auth.json').write_text('{"synthetic":true}\n')
    (root/'alias.py').symlink_to('a.py')
    (root/'image.txt').write_bytes(b'\0binary')
    (root/'large.txt').write_text('version https://git-lfs.github.com/spec/v1\noid sha256:fixture\n')
    (root/'ignored.py').write_text('must not materialize\n')
    (root/'.gitignore').write_text('ignored.py\n')
    git(root, 'add', '-f', '.')
    git(root, 'commit', '-m', 'synthetic excluded paths')
    stage = Path(cg.prepare(root, home)['path'])
    for name in ['.env', 'auth.json', 'alias.py', 'image.txt', 'large.txt', 'ignored.py']:
        assert not (stage/name).exists()


@pytest.mark.parametrize('bad', ['alternates', 'symlink', 'hardlink'])
def test_object_store_links_and_alternates_are_refused_before_git_reads(repo, bad):
    root, home = repo
    objects = root/'.git/objects'
    if bad == 'alternates':
        (objects/'info/alternates').write_text('/never/read/this\n')
    elif bad == 'symlink':
        (objects/'unsafe').symlink_to(root/'a.py')
    else:
        os.link(root/'a.py', objects/'unsafe')
    with pytest.raises(cg.GitStageError, match='object|Alternate'):
        cg.prepare(root, home)


def test_dirty_unrelated_source_is_preserved_but_scoped_drift_blocks(repo):
    root, home = repo
    state = cg.prepare(root, home)
    (root/'b.py').write_text('unrelated dirty work\n')
    cg.check_original(root, home, state, ['a.py'])
    (root/'a.py').write_text('scoped drift\n')
    with pytest.raises(cg.GitStageError, match='changed'):
        cg.check_original(root, home, state, ['a.py'])
    assert (root/'b.py').read_text() == 'unrelated dirty work\n'


def test_inspect_complete_diff_create_modify_delete_mode_apply_and_undo(repo, monkeypatch):
    root, home = repo
    state = cg.prepare(root, home); stage = Path(state['path'])
    (stage/'a.py').write_text('value = 3\n'); (stage/'a.py').chmod(0o755)
    (stage/'b.py').unlink()
    (stage/'new.py').write_text('created = True\n'); (stage/'new.py').chmod(0o644)
    value = review(stage, home, ['a.py', 'b.py', 'new.py'], state['baseline'], monkeypatch)
    assert {row['kind'] for row in value['changes']} == {'modify', 'create', 'delete'}
    assert '-value = 1' in value['diff'] and '+value = 3' in value['diff']
    assert 'old mode' in value['diff']
    applied = cg.apply_review(root, home, stage, value)
    assert (root/'a.py').read_text() == 'value = 3\n'
    assert stat.S_IMODE((root/'a.py').stat().st_mode) == 0o755
    assert not (root/'b.py').exists() and (root/'new.py').exists()
    undo_manifest(Path(applied['manifest_path']))
    assert (root/'a.py').read_text() == 'value = 1\n'
    assert stat.S_IMODE((root/'a.py').stat().st_mode) == 0o644
    assert (root/'b.py').read_text() == 'other = 2\n'
    assert not (root/'new.py').exists()


@pytest.mark.parametrize('change', ['outscope', 'secret', 'symlink', 'gate', 'git'])
def test_inspect_refuses_outscope_sensitive_links_and_gate_changes(repo, change):
    root, home = repo
    state = cg.prepare(root, home); stage = Path(state['path'])
    name = {'outscope': 'b.py', 'secret': 'auth.json', 'symlink': 'a.py',
            'gate': 'pyproject.toml', 'git': '.git'}[change]
    if change == 'symlink':
        (stage/name).unlink(); (stage/name).symlink_to(root/'a.py')
    else:
        (stage/name).write_text('changed\n')
    with pytest.raises((cg.GitStageError, OSError)):
        cg.inspect(stage, home, ['a.py'], state['baseline'])


def test_gate_config_cannot_be_allowed_as_task_source(repo):
    root, home = repo
    state = cg.prepare(root, home)
    with pytest.raises(cg.GitStageError, match='definitions'):
        cg.inspect(Path(state['path']), home, ['pyproject.toml'], state['baseline'])


def test_apply_requires_exact_token_current_gate_and_source_preimage(repo, monkeypatch):
    root, home = repo
    state = cg.prepare(root, home); stage = Path(state['path'])
    (stage/'a.py').write_text('value = 3\n')
    value = review(stage, home, ['a.py'], state['baseline'], monkeypatch)
    with pytest.raises(cg.GitStageError, match='Exact'):
        cg.apply_review(root, home, stage, {**value, 'token': 'wrong'})
    monkeypatch.setattr(cg.verify, 'is_current', lambda *args: False)
    with pytest.raises(cg.GitStageError, match='Exact'):
        cg.apply_review(root, home, stage, value)
    monkeypatch.setattr(cg.verify, 'is_current', lambda *args: True)
    (root/'a.py').write_text('intervening source edit\n')
    with pytest.raises(cg.GitStageError, match='changed'):
        cg.apply_review(root, home, stage, value)
    assert (root/'a.py').read_text() == 'intervening source edit\n'


def test_apply_refuses_post_review_worker_change(repo, monkeypatch):
    root, home = repo
    state = cg.prepare(root, home); stage = Path(state['path'])
    (stage/'a.py').write_text('value = 3\n')
    value = review(stage, home, ['a.py'], state['baseline'], monkeypatch)
    (stage/'a.py').write_text('value = 4\n')
    with pytest.raises(cg.GitStageError, match='reviewed'):
        cg.apply_review(root, home, stage, value)
    assert (root/'a.py').read_text() == 'value = 1\n'


def test_runtime_evidence_and_pytest_cache_do_not_hide_other_paths(repo):
    root, home = repo
    state = cg.prepare(root, home); stage = Path(state['path'])
    (stage/'.tsmax/verify').mkdir(parents=True)
    (stage/'.tsmax/verify/synthetic.json').write_text('{}')
    (stage/'.pytest_cache').mkdir(); (stage/'.pytest_cache/result').write_text('fixture')
    assert cg.snapshot(stage, home) == state['baseline']
    (stage/'a.py').write_text('value = 3\n')
    assert len(cg.inspect(stage, home, ['a.py'], state['baseline'])['changes']) == 1
    (stage/'.tsmax/unexpected.json').write_text('{}')
    with pytest.raises(cg.GitStageError, match='out-of-scope'):
        cg.inspect(stage, home, ['a.py'], state['baseline'])


def test_no_edit_task_requires_explicit_resolution(repo):
    root, home = repo
    state = cg.prepare(root, home)
    with pytest.raises(cg.GitStageError, match='No source changes'):
        cg.inspect(Path(state['path']), home, ['a.py'], state['baseline'])


def test_unsafe_file_mode_is_refused_before_review(repo):
    root, home = repo
    state = cg.prepare(root, home); stage = Path(state['path'])
    (stage/'a.py').chmod(0o666)
    with pytest.raises(cg.GitStageError, match='permissions'):
        cg.inspect(stage, home, ['a.py'], state['baseline'])


def test_complete_diff_marks_missing_newline_and_empty_file_creation(repo):
    root, home = repo
    state = cg.prepare(root, home); stage = Path(state['path'])
    (stage/'a.py').write_text('without final newline')
    (stage/'empty.py').write_text('')
    value = cg.inspect(stage, home, ['a.py', 'empty.py'], state['baseline'])
    assert '+without final newline\n\\ No newline at end of file\n' in value['diff']
    assert 'diff --tsmax a/empty.py b/empty.py\ncreate mode' in value['diff']


def test_baseline_blob_path_cannot_escape_private_digest_store(repo, monkeypatch):
    root, home = repo
    state = cg.prepare(root, home); stage = Path(state['path'])
    (stage/'a.py').write_text('changed\n')
    baseline = {**state['baseline'], 'a.py': {**state['baseline']['a.py'], 'digest': '../../credential-canary'}}
    with pytest.raises(cg.GitStageError, match='digest'):
        cg.inspect(stage, home, ['a.py'], baseline)


def test_recorded_head_drift_blocks_application_even_if_scoped_bytes_match(repo):
    root, home = repo
    state = cg.prepare(root, home)
    git(root, 'commit', '--allow-empty', '-m', 'new recorded head')
    with pytest.raises(cg.GitStageError, match='HEAD changed'):
        cg.check_original(root, home, state, ['a.py'])


def test_packed_objects_and_packed_refs_are_supported_without_source_config(repo):
    root, home = repo
    git(root, 'gc', '--prune=now')
    git(root, 'pack-refs', '--all')
    state = cg.prepare(root, home)
    assert (Path(state['path'])/'a.py').read_text() == 'value = 1\n'


def test_ignored_root_gate_config_is_preserved(repo):
    root, home = repo
    (root/'.gitignore').write_text('package-lock.json\npyproject.toml\n')
    git(root, 'add', '.gitignore'); git(root, 'commit', '-m', 'ignore tracked gate files')
    state = cg.prepare(root, home); stage = Path(state['path'])
    assert (stage/'package-lock.json').exists()
    assert state['baseline']['pyproject.toml']['kind'] == 'file'
    cg.check_original(root, home, state, ['a.py'])


def test_linked_source_worktree_uses_safe_common_metadata(repo):
    root, home = repo
    linked = root.parent/'linked-source'
    git(root, 'worktree', 'add', '--detach', str(linked))
    state = cg.prepare(linked, home)
    assert (Path(state['path'])/'a.py').read_text() == 'value = 1\n'


def test_private_worktree_has_recorded_baseline_index_without_checkout(repo):
    root, home = repo
    state = cg.prepare(root, home); stage = Path(state['path'])
    assert git(stage, 'status', '--porcelain') == b''
    (stage/'a.py').write_text('value = 3\n')
    assert b' M a.py' in git(stage, 'status', '--porcelain')
    assert git(root, 'status', '--porcelain') == b''


def add_static_assets(root):
    assets = root/'assets'
    assets.mkdir()
    payloads = {'logo.png': b'\x89PNG\r\n\x1a\n\0\xffsynthetic-image',
                'logo.svg': b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>\n',
                'font.woff2': b'wOF2\0\xffsynthetic-font'}
    for name, payload in payloads.items():
        (assets/name).write_bytes(payload)
    (root/'main.ts').write_text("import logo from './assets/logo.png';\nimport vector from './assets/logo.svg';\n")
    (root/'style.css').write_text("@font-face {font-family: Fixture; src: url('./assets/font.woff2');}\n")
    git(root, 'add', '.'); git(root, 'commit', '-m', 'synthetic required static assets')
    return payloads


def test_required_static_assets_survive_staging_and_have_clean_index(repo):
    root, home = repo
    payloads = add_static_assets(root)
    state = cg.prepare(root, home); stage = Path(state['path'])
    for name, payload in payloads.items():
        assert (stage/'assets'/name).read_bytes() == payload
        row = state['baseline']['assets/'+name]
        assert row['digest'] == hashlib.sha256(payload).hexdigest()
        assert row['kind'] == ('file' if name.endswith('.svg') else 'asset')
        assert (stage.parent/'baselines'/row['digest']).read_bytes() == payload
    assert git(stage, 'status', '--porcelain') == b''
    cg.check_original(root, home, state, ['main.ts'])


@pytest.mark.parametrize('suffix', ['png', 'woff2'])
def test_binary_assets_are_readonly_and_changes_invalidate_review(repo, suffix):
    root, home = repo
    add_static_assets(root)
    state = cg.prepare(root, home); stage = Path(state['path'])
    rel = 'assets/'+('logo.png' if suffix == 'png' else 'font.woff2')
    with pytest.raises(cg.GitStageError, match='read-only'):
        cg.inspect(stage, home, [rel], state['baseline'])
    before = cg.verify.snapshot(stage, home)
    (stage/'a.py').write_text('value = 3\n')
    cg.inspect(stage, home, ['a.py'], state['baseline'])
    (stage/rel).write_bytes(b'changed binary\0\xff')
    after = cg.verify.snapshot(stage, home)
    assert before['digest'] != after['digest']
    assert before['hashes'][rel] != after['hashes'][rel]
    with pytest.raises(cg.GitStageError, match='out-of-scope'):
        cg.inspect(stage, home, ['a.py'], state['baseline'])


def test_original_binary_asset_drift_blocks_scoped_text_application(repo, monkeypatch):
    root, home = repo
    add_static_assets(root)
    state = cg.prepare(root, home); stage = Path(state['path'])
    (stage/'a.py').write_text('value = 3\n')
    value = review(stage, home, ['a.py'], state['baseline'], monkeypatch)
    (root/'assets/logo.png').write_bytes(b'changed original asset\0')
    with pytest.raises(cg.GitStageError, match='changed'):
        cg.apply_review(root, home, stage, value)
    assert (root/'a.py').read_text() == 'value = 1\n'


def test_svg_is_editable_text_with_exact_apply_and_undo(repo, monkeypatch):
    root, home = repo
    original = add_static_assets(root)['logo.svg']
    state = cg.prepare(root, home); stage = Path(state['path'])
    updated = original.replace(b'M0 0', b'M1 1')
    (stage/'assets/logo.svg').write_bytes(updated)
    value = review(stage, home, ['assets/logo.svg'], state['baseline'], monkeypatch)
    assert 'M1 1' in value['diff']
    applied = cg.apply_review(root, home, stage, value)
    assert (root/'assets/logo.svg').read_bytes() == updated
    undo_manifest(Path(applied['manifest_path']))
    assert (root/'assets/logo.svg').read_bytes() == original


def test_credential_named_asset_is_not_read_or_materialized(repo, monkeypatch):
    root, home = repo
    add_static_assets(root)
    fake_secret = root/'assets/credentials.png'
    fake_secret.write_bytes(b'synthetic-credential-canary\0')
    git(root, 'add', '.'); git(root, 'commit', '-m', 'synthetic excluded credential name')
    oid = git(root, 'rev-parse', 'HEAD:assets/credentials.png').decode().strip()
    real_blob = cg._blob
    def guarded_blob(control, object_id, *args, **kwargs):
        assert object_id != oid, 'credential-named asset content must not be read'
        return real_blob(control, object_id, *args, **kwargs)
    monkeypatch.setattr(cg, '_blob', guarded_blob)
    state = cg.prepare(root, home); stage = Path(state['path'])
    assert not (stage/'assets/credentials.png').exists()
    # A worker cannot smuggle a credential-named binary asset into an otherwise safe review.
    (stage/'a.py').write_text('value = 3\n')
    (stage/'assets/credentials.png').write_bytes(b'never read this synthetic content')
    read = cg.sf._read_at
    def guarded_read(fd, name, bound):
        assert name != 'credentials.png'
        return read(fd, name, bound)
    monkeypatch.setattr(cg.sf, '_read_at', guarded_read)
    with pytest.raises(cg.GitStageError, match='out-of-scope'):
        cg.inspect(stage, home, ['a.py'], state['baseline'])


def test_asset_larger_than_source_limit_uses_bounded_asset_limit(repo):
    root, home = repo
    payload = b'\x89PNG\0'+b'\xff'*1_100_000
    (root/'large.png').write_bytes(payload)
    git(root, 'add', '.'); git(root, 'commit', '-m', 'asset over one MB but within eight MB')
    state = cg.prepare(root, home); stage = Path(state['path'])
    assert (stage/'large.png').read_bytes() == payload
    assert cg.snapshot(stage, home)['large.png']['digest'] == hashlib.sha256(payload).hexdigest()
    cg.check_original(root, home, state, ['a.py'])


def test_known_build_outputs_do_not_change_review_or_apply_scope(repo):
    root, home = repo
    state = cg.prepare(root, home); stage = Path(state['path'])
    (stage/'a.py').write_text('value = 3\n')
    before = cg.inspect(stage, home, ['a.py'], state['baseline'])
    for directory in ['dist', 'build', 'coverage', '.next', 'target', '.ruff_cache', '.mypy_cache']:
        (stage/directory).mkdir()
        (stage/directory/'generated.bin').write_bytes(b'generated binary\0\xff')
    (stage/'.eslintcache').write_text('generated lint cache')
    (stage/'project.tsbuildinfo').write_text('generated incremental compiler cache')
    after = cg.inspect(stage, home, ['a.py'], state['baseline'])
    assert after == before
    (stage/'arbitrary-output.json').write_text('{}')
    with pytest.raises(cg.GitStageError, match='out-of-scope'):
        cg.inspect(stage, home, ['a.py'], state['baseline'])


@pytest.mark.parametrize('dependency', ['node_modules', '.venv', 'venv'])
def test_build_names_inside_dependencies_are_not_exempt_from_scope(repo, dependency):
    root, home = repo
    state = cg.prepare(root, home); stage = Path(state['path'])
    nested = stage/dependency/'package'/'dist'
    nested.mkdir(parents=True)
    source = nested/'runtime.js'
    source.write_text('approved dependency input')
    baseline = cg.snapshot(stage, home)  # Explicit dependency preparation precedes task scope.
    (stage/'a.py').write_text('value = 3\n')
    cg.inspect(stage, home, ['a.py'], baseline)
    source.write_text('unexpected changed dependency input')
    with pytest.raises(cg.GitStageError, match='out-of-scope'):
        cg.inspect(stage, home, ['a.py'], baseline)


def test_known_build_output_exemption_does_not_allow_symlinks(repo):
    root, home = repo
    state = cg.prepare(root, home); stage = Path(state['path'])
    (stage/'a.py').write_text('value = 3\n')
    (stage/'dist').mkdir()
    (stage/'dist/escape').symlink_to(root/'a.py')
    with pytest.raises(cg.GitStageError, match='links'):
        cg.inspect(stage, home, ['a.py'], state['baseline'])


def add_npm_shim(stage):
    deps = stage/'node_modules'
    (deps/'.bin').mkdir(parents=True)
    (deps/'tool').mkdir()
    target = deps/'tool/bin.js'
    target.write_text('#!/bin/sh\nprintf synthetic-tool\\n\n')
    target.chmod(0o755)
    link = deps/'.bin/tool'
    link.symlink_to('../tool/bin.js')
    return deps, link, target


def test_npm_executable_shim_is_accepted_without_dependency_content_reads(repo, monkeypatch):
    root, home = repo
    state = cg.prepare(root, home); stage = Path(state['path'])
    deps, link, target = add_npm_shim(stage)
    read = cg.sf._read_at
    def guarded(fd, name, bound):
        assert name != 'bin.js', 'dependency content must not enter source/map/context reads'
        return read(fd, name, bound)
    monkeypatch.setattr(cg.sf, '_read_at', guarded)
    baseline = cg.snapshot(stage, home)
    assert baseline['node_modules/']['kind'] == 'dependency'
    assert not any(path.startswith('node_modules/') and path != 'node_modules/' for path in baseline)
    (stage/'a.py').write_text('value = 3\n')
    assert len(cg.inspect(stage, home, ['a.py'], baseline)['changes']) == 1
    # A trusted installed executable remains usable by ordinary local check processes.
    result = subprocess.run([str(link)], cwd=stage, check=True, capture_output=True)
    assert b'synthetic-tool' in result.stdout


def test_npm_shim_target_mutation_changes_compact_dependency_digest(repo):
    root, home = repo
    state = cg.prepare(root, home); stage = Path(state['path'])
    deps, link, target = add_npm_shim(stage)
    alternate = deps/'tool/other.js'
    alternate.write_text('#!/bin/sh\nexit 0\n'); alternate.chmod(0o755)
    baseline = cg.snapshot(stage, home)
    link.unlink(); link.symlink_to('../tool/other.js')
    assert cg.snapshot(stage, home)['node_modules/']['digest'] != baseline['node_modules/']['digest']
    (stage/'a.py').write_text('value = 3\n')
    with pytest.raises(cg.GitStageError, match='dependency tree'):
        cg.inspect(stage, home, ['a.py'], baseline)


@pytest.mark.parametrize('bad', ['absolute', 'escape', 'cycle', 'secret', 'state', 'directory', 'hardlink', 'special', 'dangling'])
def test_npm_shims_reject_unsafe_targets(repo, bad, monkeypatch):
    root, home = repo
    state = cg.prepare(root, home); stage = Path(state['path'])
    deps, link, target = add_npm_shim(stage)
    link.unlink()
    if bad == 'absolute':
        link.symlink_to(target)
    elif bad == 'escape':
        link.symlink_to('../../a.py')
    elif bad == 'cycle':
        link.symlink_to('peer'); (deps/'.bin/peer').symlink_to('tool')
    elif bad == 'secret':
        (deps/'tool/auth.json').write_text('synthetic do-not-read canary')
        link.symlink_to('../tool/auth.json')
    elif bad == 'state':
        (deps/'tool/.tsmax').mkdir(); (deps/'tool/.tsmax/bin.js').write_text('synthetic state')
        link.symlink_to('../tool/.tsmax/bin.js')
    elif bad == 'directory':
        link.symlink_to('../tool')
    elif bad == 'hardlink':
        os.link(target, deps/'tool/alias.js'); link.symlink_to('../tool/bin.js')
    elif bad == 'special':
        os.mkfifo(deps/'tool/pipe'); link.symlink_to('../tool/pipe')
    else:
        link.symlink_to('../tool/missing.js')
    read = cg.sf._read_at
    def guarded(fd, name, bound):
        assert name not in {'bin.js', 'auth.json', 'pipe', 'missing.js'}
        return read(fd, name, bound)
    monkeypatch.setattr(cg.sf, '_read_at', guarded)
    with pytest.raises(cg.GitStageError, match='[Ll]ink'):
        cg.snapshot(stage, home)


def test_entire_node_modules_directory_cannot_be_a_link(repo):
    root, home = repo
    state = cg.prepare(root, home); stage = Path(state['path'])
    outside = root.parent/'external-dependencies'; outside.mkdir()
    (stage/'node_modules').symlink_to(outside, target_is_directory=True)
    with pytest.raises(cg.GitStageError, match='actual node_modules'):
        cg.snapshot(stage, home)


def test_dependency_link_chain_is_bounded(repo):
    root, home = repo
    state = cg.prepare(root, home); stage = Path(state['path'])
    deps, link, target = add_npm_shim(stage)
    link.unlink()
    for i in range(cg.MAX_DEPENDENCY_LINK_HOPS+1):
        (deps/'.bin'/('shim'+str(i))).symlink_to('shim'+str(i+1))
    (deps/'.bin'/('shim'+str(cg.MAX_DEPENDENCY_LINK_HOPS+1))).symlink_to('../tool/bin.js')
    with pytest.raises(cg.GitStageError, match='overlong'):
        cg.snapshot(stage, home)


def test_large_dependency_tree_has_one_small_integrity_record(repo, monkeypatch):
    root, home = repo
    state = cg.prepare(root, home); stage = Path(state['path'])
    deps = stage/'node_modules'; deps.mkdir()
    for index in range(10_001):
        (deps/f'module-{index:05}.js').write_text('synthetic dependency')
    read = cg.sf._read_at
    def guarded(fd, name, bound):
        assert not name.startswith('module-')
        return read(fd, name, bound)
    monkeypatch.setattr(cg.sf, '_read_at', guarded)
    baseline = cg.snapshot(stage, home)
    assert baseline['node_modules/']['entries'] == 10_002
    assert len(json.dumps(baseline)) < 10_000
    assert len(baseline) == len(state['baseline'])+1
    (stage/'a.py').write_text('value = 3\n')
    cg.inspect(stage, home, ['a.py'], baseline)
    (deps/'module-00000.js').write_text('changed dependency')
    with pytest.raises(cg.GitStageError, match='dependency tree'):
        cg.inspect(stage, home, ['a.py'], baseline)
    monkeypatch.setattr(cg.pm, 'MAX_FILES', 20)
    with pytest.raises(cg.GitStageError, match='entry bound'):
        cg.snapshot(stage, home)
