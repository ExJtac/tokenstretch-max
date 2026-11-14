"""Private Git staging and exact, reversible application of reviewed source changes.

Git never opens the source repository configuration. Only bounded, no-follow
metadata/object copies enter a clean control repository; checkout filters and
hooks are never used. This is not containment for malicious project code.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import stat
import subprocess
import tempfile
import time
import unicodedata
import uuid

from . import project_map as pm, secure_files as sf, verify
from .adapters.map_support import current_exclusions
from .context import Context
from .installer import apply_changes

MAX_OBJECT_BYTES = 64_000_000
MAX_OBJECT_TOTAL = 256_000_000
MAX_DIFF_BYTES = 4_000_000
MAX_METADATA = 16_000_000
RUNTIME_OUTPUT_DIRS = frozenset({'__pycache__', '.pytest_cache', '.ruff_cache', '.mypy_cache',
                               'dist', 'build', 'coverage', '.next', 'target'})
DEPENDENCY_DIRS = frozenset({'node_modules', '.venv', 'venv'})
RUNTIME_OUTPUT_NAMES = frozenset({'.eslintcache'})
MAX_DEPENDENCY_LINK_HOPS = 32
PROTECTED = frozenset(verify.CONFIG_NAMES | pm.GENERATED_NAMES | {
    '.gitignore', '.gitattributes', 'makefile', 'justfile', 'conftest.py'})
_HEX = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')


class GitStageError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _hash(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _read(path, bound=pm.MAX_FILE_BYTES):
    try:
        return sf.read_bytes(path, bound)
    except OSError as exc:
        raise GitStageError('Refusing an unsafe, changing, or oversized file.') from exc


def _metadata(path):
    raw = _read(path, MAX_METADATA)
    if raw is None:
        raise GitStageError('Private staging metadata is missing; retained work needs inspection.')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise GitStageError('Invalid private staging metadata.') from exc
    if not isinstance(value, dict):
        raise GitStageError('Invalid private staging metadata.')
    return value


def _stage_info(stage):
    stage = pm._absolute(stage)
    info = _metadata(stage.parent / 'stage.json')
    if info.get('path') != str(stage) or stage.name != 'worktree':
        raise GitStageError('Stage identity does not match its private record.')
    exclusions = info.get('exclusions')
    if (not isinstance(exclusions, list) or len(exclusions) > pm.MAX_FILES
            or any(not isinstance(rel, str) or not pm._rel_ok(rel) for rel in exclusions)):
        raise GitStageError('Invalid staging exclusion policy.')
    return info


def _git(control, args, *, limit=MAX_METADATA, index=None):
    """Run only local Git plumbing against a clean private control repository."""
    env = {'PATH': os.defpath, 'HOME': str(control.parent / 'git-home'),
           'XDG_CONFIG_HOME': str(control.parent / 'git-home'), 'LC_ALL': 'C',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': os.devnull,
           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_NO_REPLACE_OBJECTS': '1',
           'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0',
           'GIT_NO_LAZY_FETCH': '1'}
    if index is not None:
        env['GIT_INDEX_FILE'] = str(index)
    argv = ['/usr/bin/git', '-c', 'core.hooksPath='+os.devnull, '-c', 'core.attributesFile='+os.devnull,
            '-c', 'core.fsmonitor=false', '-c', 'protocol.allow=never', '--git-dir='+str(control), *args]
    process = subprocess.Popen(argv, cwd=control.parent, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    output = bytearray()
    deadline = time.monotonic()+30
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
    selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
    error_bytes = 0
    try:
        while selector.get_map():
            if time.monotonic() > deadline:
                raise GitStageError('Local Git plumbing exceeded its timeout.')
            for key, _ in selector.select(0.1):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                elif key.data == 'stdout':
                    output.extend(chunk)
                    if len(output) > limit:
                        raise GitStageError('Local Git output exceeds the complete-output bound.')
                else:
                    error_bytes += len(chunk)
                    if error_bytes > 64_000:
                        raise GitStageError('Local Git diagnostics exceed the safety bound.')
        if process.wait(timeout=1):
            raise GitStageError('Local Git plumbing failed; private staging work was retained.')
        return bytes(output)
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()


def _git_dirs(root):
    gitdir = root / '.git'
    with sf._parent(gitdir) as (fd, name):
        try:
            mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
        except FileNotFoundError as exc:
            raise GitStageError('Staging requires an existing local Git commit.') from exc
    if stat.S_ISDIR(mode):
        return gitdir, gitdir
    if not stat.S_ISREG(mode):
        raise GitStageError('Source Git metadata must not be a link.')
    raw = _read(gitdir, 4096)
    try:
        line = raw.decode('utf-8').strip()
    except UnicodeError as exc:
        raise GitStageError('Unsupported Git directory pointer.') from exc
    if not line.startswith('gitdir: '):
        raise GitStageError('Unsupported Git directory pointer.')
    target = pm._absolute(root / line[8:])
    # Accept conventional linked worktrees, not arbitrary credential/config paths.
    if target.parent.name != 'worktrees' or target.parent.parent.name != '.git':
        raise GitStageError('Only standard local Git worktree metadata is supported.')
    common = target.parent.parent
    if (_read(target / 'commondir', 4096) or b'').strip() != b'../..':
        raise GitStageError('Unsupported Git common-directory pointer.')
    return target, common


def _head(root):
    gitdir, common = _git_dirs(root)
    raw = _read(gitdir / 'HEAD', 4096)
    try:
        value = (raw or b'').decode('ascii').strip()
    except UnicodeError as exc:
        raise GitStageError('Invalid source Git HEAD.') from exc
    if value.startswith('ref: '):
        ref = value[5:]
        if not ref.startswith('refs/heads/') or not pm._rel_ok(ref) or any(c in ref for c in ' ~^:?*[\\'):
            raise GitStageError('Unsafe source Git reference.')
        raw = _read(common / ref, 4096)
        if raw is not None:
            value = raw.decode('ascii').strip()
        else:
            packed = _read(common / 'packed-refs', MAX_METADATA) or b''
            value = ''
            for line in packed.decode('ascii').splitlines():
                fields = line.split(' ')
                if len(fields) == 2 and fields[1] == ref:
                    value = fields[0]
                    break
    if not _HEX.fullmatch(value):
        raise GitStageError('Source HEAD does not resolve to a recorded commit.')
    return value, common


def _copy_objects(source, destination):
    """Audit the whole object directory before copying recognized object files."""
    files = []
    total = 0
    count = 0
    with sf._parent(source / '.probe') as (rootfd, _):
        def walk(fd, prefix=''):
            nonlocal total, count
            if len(PurePosixPath(prefix).parts) > 4:
                raise GitStageError('Unsupported object-store directory layout.')
            for name in sorted(os.listdir(fd)):
                count += 1
                if count > 100_000:
                    raise GitStageError('Object-store entry bound exceeded.')
                rel = prefix+'/'+name if prefix else name
                mode = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if rel in {'info/alternates', 'info/http-alternates'} or name.endswith('.promisor'):
                    raise GitStageError('Alternate or partial object stores are unsupported.')
                if stat.S_ISDIR(mode.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        walk(child, rel)
                    finally:
                        os.close(child)
                elif not stat.S_ISREG(mode.st_mode) or mode.st_nlink != 1:
                    raise GitStageError('Git object stores must not contain links or special files.')
                elif re.fullmatch(r'[0-9a-f]{2}/(?:[0-9a-f]{38}|[0-9a-f]{62})|pack/pack-(?:[0-9a-f]{40}|[0-9a-f]{64})\.(?:pack|idx)', rel):
                    total += mode.st_size
                    if mode.st_size > MAX_OBJECT_BYTES or total > MAX_OBJECT_TOTAL:
                        raise GitStageError('Object-store copy exceeds the staging bound.')
                    files.append(rel)
        walk(rootfd)
    for rel in files:
        raw = _read(source / rel, MAX_OBJECT_BYTES)
        if raw is None:
            raise GitStageError('Source objects changed during staging.')
        sf.write_bytes(destination / rel, raw)


def _blob(control, oid, limit=pm.MAX_FILE_BYTES):
    size = int(_git(control, ['cat-file', '-s', oid], limit=100).strip())
    if size > limit:
        return None
    return _git(control, ['cat-file', 'blob', oid], limit=limit)


def _tree_files(control, commit, policy):
    raw = _git(control, ['ls-tree', '-r', '-t', '-z', '--full-tree', commit])
    rows = {}
    portable_names = {}
    directories = {''}
    for entry in raw.split(b'\0'):
        if not entry:
            continue
        try:
            header, path = entry.split(b'\t', 1)
            mode, kind, oid = header.decode('ascii').split(' ')
            name = path.decode('utf-8')
        except (ValueError, UnicodeError) as exc:
            raise GitStageError('Unsupported Git tree entry.') from exc
        if not pm._rel_ok(name) or not _HEX.fullmatch(oid):
            raise GitStageError('Unsafe Git tree entry.')
        portable = unicodedata.normalize('NFC', name).casefold()
        if portable in portable_names and portable_names[portable] != name:
            raise GitStageError('Git tree has paths that collide on common local filesystems.')
        portable_names[portable] = name
        if len(rows) >= pm.MAX_FILES * 5:
            raise GitStageError('Git tree exceeds the complete staging entry bound.')
        rows[name] = (mode, kind, oid)
        if kind == 'tree':
            directories.add(name)
    rules = {}
    excluded = set()
    for directory in sorted(directories, key=lambda p: (p.count('/'), p)):
        parent = directory.rsplit('/', 1)[0] if '/' in directory else ''
        inherited = list(rules.get(parent, [])) if directory else []
        if directory and (parent in excluded or policy.reason(directory, directory=True)
                          or pm._ignored(directory, True, inherited)):
            excluded.add(directory)
            continue
        ignore = (directory+'/' if directory else '')+'.gitignore'
        if ignore in rows:
            mode, kind, oid = rows[ignore]
            if mode not in {'100644', '100755'} or kind != 'blob':
                raise GitStageError('Unsafe committed ignore rules.')
            data = _blob(control, oid, 65_536)
            if data is None:
                raise GitStageError('Committed ignore rules exceed the bound.')
            try:
                for line in data.decode('utf-8').splitlines():
                    if not line or line.startswith('#'):
                        continue
                    pattern = line.removeprefix('!').strip('/')
                    if (line != line.strip() or not pattern or '\\' in line or len(pattern) > 4096
                            or len(pattern.split('/')) > 65 or '\x00' in line
                            or '[' in re.sub(r'\[!?[-A-Za-z0-9_.]+\]', '', line)
                            or ']' in re.sub(r'\[!?[-A-Za-z0-9_.]+\]', '', line)):
                        raise GitStageError('Unsupported committed ignore syntax.')
                    inherited.append((directory, line))
            except UnicodeError as exc:
                raise GitStageError('Committed ignore rules must be UTF-8.') from exc
        rules[directory] = inherited
    result = {}
    total = 0
    for name, (mode, kind, oid) in sorted(rows.items()):
        parent = name.rsplit('/', 1)[0] if '/' in name else ''
        gate_input = not parent and Path(name).name.lower() in PROTECTED
        if (mode not in {'100644', '100755'} or kind != 'blob' or parent in excluded
                or policy.reason(name) or (not gate_input and pm._ignored(name, False, rules.get(parent, [])))):
            continue
        data = _blob(control, oid, verify.input_limit(name))
        binary_asset = Path(name).suffix.lower() in verify.BINARY_ASSET_SUFFIXES
        if (data is None or (not binary_asset and pm._text(data) is None)
                or data.startswith(b'version https://git-lfs.github.com/spec/v1\n')):
            continue
        total += len(data)
        if len(result) >= pm.MAX_FILES or total > pm.MAX_TOTAL_BYTES:
            raise GitStageError('Eligible committed source exceeds staging bounds.')
        result[name] = (data, 0o755 if mode == '100755' else 0o644)
    return result


def prepare(root: Path, home: Path) -> dict:
    root, home = pm._absolute(root), pm._absolute(home)
    with pm._root_fd(root):
        pass
    exclusions = pm._exclusions(root, current_exclusions(root, home))
    policy = verify.VerifyPolicy(exclusions)
    commit, common = _head(root)
    runtime = Path(tempfile.mkdtemp(prefix='tsmax-stage-', dir=Path(tempfile.gettempdir()).resolve()))
    control, stage = runtime/'control.git', runtime/'worktree'
    sf.mkdir_parents(runtime/'git-home')
    _git(control, ['init', '--bare', '--template=', '--object-format='+('sha256' if len(commit) == 64 else 'sha1'), str(control)])
    _copy_objects(common/'objects', control/'objects')
    if _git(control, ['cat-file', '-t', commit], limit=100).strip() != b'commit':
        raise GitStageError('Recorded source object is not a commit.')
    files = _tree_files(control, commit, policy)
    if _head(root)[0] != commit:
        raise GitStageError('Source HEAD changed while preparing the retained staging area.')
    _git(control, ['worktree', 'add', '--detach', '--no-checkout', str(stage), commit])
    os.chmod(stage, 0o700)
    pointer = (_read(stage/'.git', 4096) or b'').decode('utf-8').strip()
    if not pointer.startswith('gitdir: '):
        raise GitStageError('Private worktree metadata was not created as expected.')
    admin = pm._absolute(Path(pointer[8:]))
    if not admin.is_relative_to(control/'worktrees'):
        raise GitStageError('Private worktree metadata escaped its control directory.')
    # Populate only the private index: no checkout, attributes, or filters run.
    _git(control, ['--work-tree='+str(stage), 'read-tree', commit], index=admin/'index')
    for rel, (raw, mode) in files.items():
        sf.write_bytes(stage/rel, raw, mode=mode, expected_hash=None)
    info = {'path': str(stage), 'root': str(root), 'home': str(home), 'commit': commit,
            'exclusions': exclusions, 'control': str(control)}
    sf.write_bytes(runtime/'stage.json', json.dumps(info, sort_keys=True).encode())
    baseline = snapshot(stage, home)
    _stash(stage, baseline)
    return {**info, 'baseline': baseline}


def _runtime_artifact(rel):
    parts = PurePosixPath(rel).parts
    # Dependency trees remain frozen even when their package layout contains
    # directory names such as dist/build or interpreter caches.
    if any(part in DEPENDENCY_DIRS for part in parts):
        return False
    return (any(part in RUNTIME_OUTPUT_DIRS for part in parts)
            or parts[-1] in RUNTIME_OUTPUT_NAMES or parts[-1].endswith('.tsbuildinfo')
            or rel == '.tsmax/map.sqlite3' or rel == '.tsmax/verify'
            or rel.startswith('.tsmax/verify/'))


def _dependency_metadata(entry, kind):
    """Metadata only: installed dependency contents are never opened here."""
    return {'kind': kind, 'mode': stat.S_IMODE(entry.st_mode), 'size': entry.st_size,
            'mtime': entry.st_mtime_ns, 'ctime': entry.st_ctime_ns,
            'device': entry.st_dev, 'inode': entry.st_ino, 'links': entry.st_nlink}


def _dependency_link(rootfd, rel, policy, budget):
    """Validate relative npm executable shims without opening target contents."""
    parts = list(PurePosixPath(rel).parts)
    if 'node_modules' not in parts or parts.index('node_modules') == len(parts)-1:
        raise GitStageError('Only regular-file links inside an actual node_modules tree are supported.')
    boundary = tuple(parts[:parts.index('node_modules')+1])
    state_names = {'.git', '.hg', '.svn', '.tsmax', '.codex', '.claude', '.gemini', '.cursor', '.opencode', '.cache'}
    secret_policy = pm.Policy()

    def check_path(candidate):
        path = '/'.join(candidate)
        if (tuple(candidate[:len(boundary)]) != boundary or len(candidate) <= len(boundary)
                or len(candidate) > 64 or not pm._rel_ok(path)):
            raise GitStageError('Dependency links must remain inside the same node_modules tree.')
        if any(path == excluded or path.startswith(excluded+'/') for excluded in policy.exclusions):
            raise GitStageError('Dependency links must not target sensitive adapter paths.')
        for part in candidate:
            reason = secret_policy.reason(part, directory=True)
            if part.lower() in state_names or reason in {'secret directory', 'secret filename', 'map state', 'unsafe path'}:
                raise GitStageError('Dependency links must not target credential or state paths.')

    def expand(parent, target):
        if not target or len(target) > 4096 or target.startswith('/') or '\\' in target:
            raise GitStageError('Dependency links require bounded relative targets.')
        result = list(parent)
        for part in target.split('/'):
            if part in {'', '.'}:
                continue
            if part == '..':
                if len(result) <= len(boundary):
                    raise GitStageError('Dependency link escapes its node_modules tree.')
                result.pop()
            else:
                result.append(part)
        return result

    check_path(parts)
    seen, chain = set(), []
    while True:
        fd = os.dup(rootfd)
        restart = False
        try:
            for index, name in enumerate(parts):
                budget[0] -= 1
                if budget[0] < 0:
                    raise GitStageError('Dependency-link metadata traversal bound exceeded.')
                entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(entry.st_mode):
                    path = '/'.join(parts[:index+1])
                    if index < len(boundary) or path in seen or len(chain) >= MAX_DEPENDENCY_LINK_HOPS or entry.st_nlink != 1:
                        raise GitStageError('Dependency links are cyclic, overlong, or do not have an actual dependency root.')
                    seen.add(path)
                    target = os.readlink(name, dir_fd=fd)
                    if _dependency_metadata(entry, 'link') != _dependency_metadata(
                            os.stat(name, dir_fd=fd, follow_symlinks=False), 'link'):
                        raise GitStageError('Dependency link changed while inspecting it.')
                    chain.append({'path': path, 'target': target, 'metadata': _dependency_metadata(entry, 'link')})
                    parts = expand(parts[:index], target)+parts[index+1:]
                    check_path(parts)
                    restart = True
                    break
                if index < len(parts)-1:
                    if not stat.S_ISDIR(entry.st_mode) or stat.S_IMODE(entry.st_mode) & 0o7000:
                        raise GitStageError('Dependency link traverses an unsafe directory.')
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    if _dependency_metadata(entry, 'directory') != _dependency_metadata(os.fstat(child), 'directory'):
                        os.close(child)
                        raise GitStageError('Dependency directory changed while inspecting a link.')
                    os.close(fd)
                    fd = child
                else:
                    if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                        raise GitStageError('Dependency links must end at regular, non-hardlinked files; directory links are unsupported.')
                    sf.regular_mode(stat.S_IMODE(entry.st_mode))
                    if not chain:
                        raise GitStageError('Dependency link changed while inspecting it.')
                    evidence = {'chain': chain, 'resolved': '/'.join(parts),
                                'target': _dependency_metadata(entry, 'file')}
                    return {'kind': 'dependency-link', 'digest': _digest(evidence),
                            'mode': chain[0]['metadata']['mode'], 'target': chain[0]['target'],
                            'resolved': evidence['resolved'], 'target_mode': evidence['target']['mode']}
        except OSError as exc:
            raise GitStageError('Dependency links must resolve through existing no-follow metadata.') from exc
        finally:
            os.close(fd)
        if not restart:
            raise GitStageError('Dependency link did not resolve to a regular file.')


def snapshot(stage: Path, home: Path) -> dict:
    stage, home = pm._absolute(stage), pm._absolute(home)
    info = _stage_info(stage)
    policy = verify.VerifyPolicy([*info['exclusions'], *pm._exclusions(stage, current_exclusions(stage, home))])
    files = {}
    count = 0
    total = 0
    link_budget = [pm.MAX_FILES*5]
    with pm._root_fd(stage) as rootfd:
        eligible, skipped, limited = pm._walk(rootfd, policy)
        if limited or any(skipped[reason] for reason in ('unreadable ignore rules', 'unsupported ignore rules',
                'unreadable directory', 'unreadable/symlink ancestor', 'directory depth cap', 'unsafe path')):
            raise GitStageError('Stage enumeration is incomplete; no partial review is allowed.')
        eligible = set(eligible)
        def remember(rel, row, dependency):
            if dependency is None:
                files[rel] = row
            else:
                dependency['entries'] += 1
                value = {'path': rel[len(dependency['root'])+1:], 'metadata': row}
                dependency['hash'].update(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()+b'\n')

        def walk(fd, prefix='', dependency=None):
            nonlocal count, total
            if len(PurePosixPath(prefix).parts) > 64:
                raise GitStageError('Stage depth bound exceeded.')
            for name in sorted(os.listdir(fd)):
                count += 1
                if count > pm.MAX_FILES*5:
                    raise GitStageError('Stage entry bound exceeded; no partial review is allowed.')
                rel = prefix+'/'+name if prefix else name
                if not pm._rel_ok(rel):
                    raise GitStageError('Unsafe stage path.')
                entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
                mode = stat.S_IMODE(entry.st_mode)
                if stat.S_ISLNK(entry.st_mode):
                    remember(rel, _dependency_link(rootfd, rel, policy, link_budget), dependency)
                    continue
                if stat.S_ISDIR(entry.st_mode):
                    if mode & 0o7000:
                        raise GitStageError('Special directory modes are unsupported.')
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    dep = dependency
                    if dep is None and name in DEPENDENCY_DIRS:
                        dep = {'root': rel, 'hash': hashlib.sha256(), 'entries': 0}
                        remember(rel+'/', _dependency_metadata(entry, 'directory'), dep)
                    try:
                        walk(child, rel, dep)
                        if dep is not None and _dependency_metadata(entry, 'directory') != _dependency_metadata(os.fstat(child), 'directory'):
                            raise GitStageError('Dependency directory changed during its metadata snapshot.')
                    finally:
                        os.close(child)
                    if dep is not dependency:
                        files[rel+'/'] = {'kind': 'dependency', 'mode': mode,
                                         'digest': dep['hash'].hexdigest(), 'entries': dep['entries']}
                    elif dependency is not None:
                        remember(rel+'/', _dependency_metadata(entry, 'directory'), dependency)
                    elif not _runtime_artifact(rel) and rel != '.tsmax':
                        remember(rel+'/', {'kind': 'directory', 'mode': mode, 'digest': None}, None)
                    continue
                if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1 or mode & 0o7000:
                    raise GitStageError('Stage contains links, special files, or special permissions.')
                if dependency is not None:
                    remember(rel, _dependency_metadata(entry, 'file'), dependency)
                    continue
                if _runtime_artifact(rel):
                    continue
                gate_input = '/' not in rel and Path(rel).name.lower() in PROTECTED
                reason = policy.reason(rel) or (None if rel in eligible or gate_input else 'ignored')
                if reason and rel != '.git':
                    remember(rel, {'kind': 'excluded', 'mode': mode, 'digest': None,
                                   'size': entry.st_size, 'mtime': entry.st_mtime_ns, 'ctime': entry.st_ctime_ns}, None)
                    continue
                binary_asset = Path(rel).suffix.lower() in verify.BINARY_ASSET_SUFFIXES
                raw = sf._read_at(fd, name, verify.input_limit(rel))
                if raw is None or (rel != '.git' and not binary_asset and pm._text(raw) is None):
                    raise GitStageError('Stage source must remain bounded text or an allowed read-only static asset.')
                total += len(raw)
                if total > pm.MAX_TOTAL_BYTES or len(files) >= pm.MAX_FILES:
                    raise GitStageError('Stage source exceeds complete-review bounds.')
                try:
                    sf.regular_mode(mode)
                except ValueError as exc:
                    raise GitStageError('Stage source permissions are not safe for reviewed application.') from exc
                kind = 'metadata' if rel == '.git' else 'asset' if binary_asset else 'file'
                remember(rel, {'kind': kind, 'digest': _hash(raw), 'mode': mode}, None)
        walk(rootfd)
    return files


def _validate_baseline(baseline):
    if not isinstance(baseline, dict) or len(baseline) > pm.MAX_FILES*5:
        raise GitStageError('Invalid bounded source baseline.')
    for rel, row in baseline.items():
        if (not isinstance(rel, str) or not pm._rel_ok(rel.rstrip('/'))
                or not isinstance(row, dict) or row.get('kind') not in {'file', 'asset', 'metadata', 'directory', 'excluded', 'dependency'}):
            raise GitStageError('Invalid source baseline path or record.')
        if row['kind'] in {'file', 'asset', 'metadata'}:
            if not isinstance(row.get('digest'), str) or not re.fullmatch('[0-9a-f]{64}', row['digest']):
                raise GitStageError('Invalid source baseline digest.')
            sf.regular_mode(row.get('mode'))
        if row['kind'] == 'metadata' and rel != '.git':
            raise GitStageError('Invalid source baseline metadata path.')
        if row['kind'] == 'dependency':
            if (not rel.endswith('/') or not isinstance(row.get('digest'), str)
                    or not re.fullmatch('[0-9a-f]{64}', row['digest'])
                    or type(row.get('entries')) is not int or not 1 <= row['entries'] <= pm.MAX_FILES*5+1
                    or type(row.get('mode')) is not int or row['mode'] < 0 or row['mode'] > 0o777):
                raise GitStageError('Invalid compact dependency baseline.')


def _allowed(allowed_files, policy):
    if not isinstance(allowed_files, (list, tuple)) or not allowed_files or len(allowed_files) > pm.MAX_FILES:
        raise GitStageError('Task needs an exact bounded file allowlist.')
    result = set()
    for rel in allowed_files:
        if not isinstance(rel, str) or not pm._rel_ok(rel) or policy.reason(rel):
            raise GitStageError('Task contains an unsafe or excluded file path.')
        if Path(rel).name.lower() in PROTECTED or rel == '.git':
            raise GitStageError('Task must not change verification definitions or Git/ignore policy.')
        if Path(rel).suffix.lower() in verify.BINARY_ASSET_SUFFIXES:
            raise GitStageError('Binary static assets are read-only build inputs, not editable task files.')
        result.add(rel)
    return result


def inspect(stage: Path, home: Path, allowed_files, baseline: dict) -> dict:
    stage = pm._absolute(stage)
    info = _stage_info(stage)
    policy = verify.VerifyPolicy([*info['exclusions'], *pm._exclusions(stage, current_exclusions(stage, home))])
    allowed = _allowed(allowed_files, policy)
    _validate_baseline(baseline)
    current = snapshot(stage, home)
    changes = []
    changed_directories = []
    diff = []
    for rel in sorted(set(current)|set(baseline)):
        before, after = baseline.get(rel), current.get(rel)
        if before == after:
            continue
        if any(row and row['kind'] == 'dependency' for row in (before, after)):
            raise GitStageError('Worker changed an out-of-scope dependency tree.')
        if rel.endswith('/'):
            if before is not None and after is not None:
                raise GitStageError('Worker changed directory permissions outside the file review.')
            if any(path.startswith(rel) for path in allowed):
                changed_directories.append(rel)
                continue
            raise GitStageError('Worker changed an out-of-scope directory.')
        if rel not in allowed or any(row and row['kind'] != 'file' for row in (before, after)):
            raise GitStageError('Worker changed an out-of-scope or excluded path.')
        old = _baseline_bytes(stage, rel, before)
        new = _read(stage/rel, verify.input_limit(rel)) if after is not None else None
        if _hash(new) != (after['digest'] if after else None):
            raise GitStageError('Stage changed during complete diff generation.')
        change = {'path': rel, 'kind': 'create' if before is None else 'delete' if after is None else 'modify',
                  'before': before, 'after': after}
        changes.append(change)
        diff.append(f'diff --tsmax a/{rel} b/{rel}\n')
        if not before:
            diff.append(f"create mode {after['mode']:06o}\n")
        elif not after:
            diff.append(f"delete mode {before['mode']:06o}\n")
        if before and after and before['mode'] != after['mode']:
            diff.append(f"old mode {before['mode']:06o}\nnew mode {after['mode']:06o}\n")
        text_diff = difflib.unified_diff((old or b'').decode('utf-8').splitlines(keepends=True),
                    (new or b'').decode('utf-8').splitlines(keepends=True),
                    fromfile='a/'+rel if before else '/dev/null', tofile='b/'+rel if after else '/dev/null')
        diff.extend(line if line.endswith('\n') else line+'\n\\ No newline at end of file\n' for line in text_diff)
        if sum(len(part.encode()) for part in diff) > MAX_DIFF_BYTES:
            raise GitStageError('Diff exceeds complete review bound; split the task.')
    if snapshot(stage, home) != current:
        raise GitStageError('Stage changed while reviewing it.')
    if not changes:
        raise GitStageError('No source changes to review; a no-edit task needs explicit manual resolution.')
    if any(not any(row['path'].startswith(directory) for row in changes) for directory in changed_directories):
        raise GitStageError('Worker changed an empty directory outside reviewed source changes.')
    value = {'changes': changes, 'diff': ''.join(diff), 'allowed_files': sorted(allowed),
             'stage_fingerprint': _digest(current)}
    value['digest'] = _digest(value)
    return value


def _baseline_bytes(stage, rel, expected):
    if expected is None:
        return None
    # Baseline content snapshots live outside worker scope and are keyed by digest.
    raw = _read(stage.parent/'baselines'/expected['digest'], verify.input_limit(rel))
    if raw is None or _hash(raw) != expected['digest']:
        raise GitStageError('Reviewed baseline content is missing or changed.')
    return raw


def _stash(stage, files):
    for rel, row in files.items():
        if row['kind'] not in {'file', 'asset'}:
            continue
        raw = _read(stage/rel, verify.input_limit(rel))
        if _hash(raw) != row['digest']:
            raise GitStageError('Stage changed while saving its baseline.')
        sf.write_bytes(stage.parent/'baselines'/row['digest'], raw)


def check_original(root: Path, home: Path, stage_record: dict, allowed_files) -> None:
    root, home = pm._absolute(root), pm._absolute(home)
    if stage_record.get('root') != str(root) or stage_record.get('home') != str(home):
        raise GitStageError('Source identity differs from recorded staging source.')
    policy = verify.VerifyPolicy(pm._exclusions(root, current_exclusions(root, home)))
    allowed = _allowed(allowed_files, policy)
    baseline = stage_record['baseline']
    _validate_baseline(baseline)
    if _head(root)[0] != stage_record['commit']:
        raise GitStageError('Source HEAD changed from the recorded staging commit.')
    protected = {name for name in baseline if not name.endswith('/') and Path(name).name.lower() in PROTECTED}
    protected |= {name for name, row in baseline.items() if row['kind'] == 'asset'}
    protected |= {name for name in PROTECTED if '/' not in name}
    for rel in allowed | protected:
        expected = baseline.get(rel)
        if policy.reason(rel):
            if rel in allowed or expected and expected['kind'] == 'asset':
                raise GitStageError('A required task input became sensitive or excluded.')
            continue
        raw = _read(root/rel, verify.input_limit(rel))
        if _hash(raw) != (expected['digest'] if expected else None):
            raise GitStageError('Original scoped source or check definitions changed; nothing was applied.')
        if raw is not None:
            with sf._parent(root/rel) as (fd, name):
                mode = stat.S_IMODE(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode)
            if mode != expected['mode']:
                raise GitStageError('Original scoped source permissions changed.')


def apply_review(root: Path, home: Path, stage: Path, review: dict) -> dict:
    root, home, stage = pm._absolute(root), pm._absolute(home), pm._absolute(stage)
    info = _stage_info(stage)
    if info['root'] != str(root) or info['home'] != str(home):
        raise GitStageError('Review source identity does not match staging.')
    # Match the conductor's public review-token encoding exactly.
    token = hashlib.sha256(json.dumps({k: v for k, v in review.items() if k != 'token'},
                                     sort_keys=True).encode()).hexdigest()
    if review.get('token') != token or not verify.is_current(stage, home, review.get('gate', {})):
        raise GitStageError('Exact reviewed diff and fresh gate evidence are required.')
    fresh = inspect(stage, home, review['allowed_files'], review['baseline'])
    if fresh['digest'] != review['digest']:
        raise GitStageError('Staged changes differ from the reviewed changes.')
    check_original(root, home, {**info, 'baseline': review['baseline']}, review['allowed_files'])
    changes = []
    for row in fresh['changes']:
        rel, before, after = row['path'], row['before'], row['after']
        raw = _read(stage/rel, verify.input_limit(rel)) if after else None
        if _hash(raw) != (after['digest'] if after else None):
            raise GitStageError('Staged bytes changed after review.')
        changes.append({'path': str(root/rel), 'kind': row['kind'], 'content': raw or b'',
                        'mode': after['mode'] if after else None,
                        'preimage_mode': before['mode'] if before else None,
                        'preimage': before['digest'] if before else None, 'private': True, 'secure': True,
                        'reason': 'Apply exact verified conductor review '+review['token']})
    return apply_changes(Context(root=root, home=home), changes, stamp='conductor-'+uuid.uuid4().hex,
                         profile='conductor', extra_manifest={'review_token': review['token']})


def accept(stage_record: dict, stage: Path, home: Path, allowed_files) -> dict:
    baseline = snapshot(stage, home)
    _stash(stage, baseline)
    return {**stage_record, 'baseline': baseline}
