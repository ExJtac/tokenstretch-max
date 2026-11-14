"""Project-local historical procedures, saved only after exact human review.

No chat logs, model calls or provider-specific skill locations. Verification
supports the recorded procedure at one point in time, never a future task.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import os
import math
from pathlib import Path
import re
import shlex
import uuid

from . import project_map as pm, secure_files as sf, verify
from .context import Context
from .installer import apply_changes

LIBRARY = '.tsmax/skills'
MAX_RECORD = 32_000
MAX_STATE = 1_000_000
MAX_PATTERNS = 128
MAX_DIRECTORIES = 512
SLUG = re.compile(r'[a-z0-9][a-z0-9-]{0,79}')
HEX = re.compile(r'[a-f0-9]{64}')
HISTORY = 'Historical verified procedure; applicability must be reviewed and the new task must pass its own checks.'


class PatternError(ValueError):
    pass


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, indent=2) + '\n').encode()


def _digest(value):
    return _hash(_encoded(value))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PatternError('Duplicate JSON keys are not accepted in pattern evidence.')
        result[key] = value
    return result


def _decode(raw):
    try:
        value = json.loads(raw, object_pairs_hook=_unique,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite number')))
    except (ValueError, UnicodeError, RecursionError):
        raise PatternError('Pattern records and evidence must be bounded valid JSON with unique keys.') from None
    if not isinstance(value, dict):
        raise PatternError('Pattern records and evidence must be JSON objects.')
    return value


def _read(path, limit=MAX_STATE):
    raw = sf.read_bytes(path, limit)
    if raw is None:
        raise PatternError('Required pattern or verification evidence is missing.')
    return _decode(raw), _hash(raw)


def _text(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise PatternError(f'{name} must be nonempty text of at most {limit} characters.')
    return verify._diagnostic(value).strip()


def _one_line(value, limit=400):
    return ' '.join(verify._diagnostic(value).split())[:limit]


def _reference(value, directory, *, prefix=None):
    if not isinstance(value, str) or len(value) > 4096:
        raise PatternError('Invalid evidence reference.')
    path = Path(value)
    # References are exact direct-child paths, never arbitrary external files.
    if not path.is_absolute() or path.parent != directory or not re.fullmatch(r'[A-Za-z0-9_.:+-]{1,180}\.json', path.name):
        raise PatternError('Evidence reference escapes its permitted local directory.')
    if prefix and not path.name.startswith(prefix):
        raise PatternError('Evidence reference is not a supported source manifest.')
    if prefix == 'conductor-' and not re.fullmatch(r'conductor-[a-f0-9]{32}\.json', path.name):
        raise PatternError('Source manifest must use the generated Conductor manifest name.')
    if directory.name == 'verify' and not re.fullmatch(r'\d{8}T\d{6}-verify-[a-f0-9]{32}\.json', path.name):
        raise PatternError('Gate reference must use the generated verification record name.')
    return path


def _fingerprint(value):
    return isinstance(value, str) and HEX.fullmatch(value) is not None


def _recorded_tasks(tasks):
    """Validate historical plan structure without re-planning against today's tree."""
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 24:
        raise PatternError('Historical plan needs 1–24 tasks.')
    known = set()
    for row in tasks:
        fields = {'id', 'title', 'spec', 'depends_on', 'files', 'acceptance', 'checks'}
        if not isinstance(row, dict) or set(row) != fields:
            raise PatternError('Unsupported historical task schema.')
        identifier = row['id']
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,39}', identifier) or identifier in known:
            raise PatternError('Malformed historical task id.')
        for name, limit in (('title', 160), ('spec', 6000), ('acceptance', 3000)):
            _text(row[name], name, limit)
        deps, files, checks = row['depends_on'], row['files'], row['checks']
        if not isinstance(deps, list) or not all(isinstance(dep, str) and dep in known for dep in deps) or len(set(deps)) != len(deps):
            raise PatternError('Malformed historical dependencies.')
        if not isinstance(files, list) or not 1 <= len(files) <= 24 or not all(isinstance(path, str) and len(path) <= 4096 and pm._rel_ok(path) for path in files) or len(set(files)) != len(files):
            raise PatternError('Malformed historical task paths.')
        if not isinstance(checks, list) or not 1 <= len(checks) <= verify.MAX_CHECKS:
            raise PatternError('Malformed historical checks.')
        for check in checks:
            if not isinstance(check, dict) or set(check) != {'kind', 'argv'} or not isinstance(check['kind'], str) or not isinstance(check['argv'], list):
                raise PatternError('Malformed historical check schema.')
            verify.Check(check['kind'], tuple(check['argv']))
        known.add(identifier)
    return tasks


def _evidence(root, home, task_id):
    from . import conductor as c
    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,39}', task_id):
        raise PatternError('Task must be a saved Conductor task id, not a path.')
    plan, plan_hash = _read(root / c.PLAN_JSON)
    state, state_hash = _read(root / c.STATE_JSON)
    digest = plan.get('digest')
    plan_fields = {'schema', 'goal', 'tool', 'source_fingerprint', 'tasks', 'usage', 'created_at', 'models',
                   'adapter_version', 'requested_model', 'effective_model', 'digest'}
    if (set(plan) != plan_fields or type(plan.get('schema')) is not int or plan['schema'] != 1
            or type(state.get('schema')) is not int or state['schema'] != 1 or not _fingerprint(digest)
            or digest != c._digest({key: value for key, value in plan.items() if key != 'digest'})
            or state.get('plan_digest') != digest):
        raise PatternError('Task state does not match the reviewed plan.')
    task = next((row for row in _recorded_tasks(plan.get('tasks')) if row['id'] == task_id), None)
    if task is None or not isinstance(state.get('tasks'), dict) or state['tasks'].get(task_id) != 'done':
        raise PatternError('Only an applied task marked done can become a saved pattern.')
    attempts, applied = state.get('attempts'), state.get('applied')
    if (not isinstance(attempts, list) or len(attempts) > c.MAX_CALLS or not all(isinstance(row, dict) for row in attempts)
            or not isinstance(applied, list) or len(applied) > c.MAX_TASKS or not all(isinstance(row, dict) for row in applied)):
        raise PatternError('Invalid or oversized task evidence.')
    finished = [row for row in attempts if row.get('task') == task_id]
    records = [row for row in applied if row.get('task') == task_id]
    if not finished or len(records) != 1 or finished[-1].get('status') != 'verified' or finished[-1].get('provider_status') != 'completed':
        raise PatternError('Task lacks a final verified attempt and unique applied record.')
    attempt, record = finished[-1], records[0]
    reference = record.get('original_gate')
    if not isinstance(reference, dict) or reference.get('status') != 'passed' or not _fingerprint(reference.get('accepted_fingerprint')):
        raise PatternError('Task lacks passed original-project verification evidence.')
    gate_path = _reference(reference.get('result_path'), root / '.tsmax/verify')
    gate, gate_hash = _read(gate_path)
    for key in ('status', 'source_fingerprint', 'accepted_fingerprint', 'network_isolation'):
        if gate.get(key) != reference.get(key):
            raise PatternError('Original verification reference changed.')
    checks, outcomes = gate.get('checks'), gate.get('results')
    if (not isinstance(checks, list) or not 1 <= len(checks) <= verify.MAX_CHECKS
            or not all(isinstance(row, dict) for row in checks)
            or [{'kind': row.get('kind'), 'argv': row.get('argv')} for row in checks] != task['checks']
            or not isinstance(outcomes, list) or len(outcomes) != len(checks)
            or not all(isinstance(row, dict) and row.get('status') == 'passed' and row.get('exit_code') == 0 for row in outcomes)):
        raise PatternError('Original verification does not contain all passed task checks.')
    if c._gate_reference(gate)['outcomes'] != reference.get('outcomes'):
        raise PatternError('Original verification outcomes changed.')
    accepted = {'source': gate.get('source_fingerprint'), 'checks': checks,
                'tools': [row.get('tool') for row in outcomes]}
    if (not _fingerprint(gate.get('source_fingerprint')) or
            _hash(json.dumps(accepted, sort_keys=True).encode()) != gate.get('accepted_fingerprint')):
        raise PatternError('Original verification fingerprint does not match its recorded evidence.')
    gate_manifest_path = home / '.tsmax/manifests' / gate_path.name
    if reference.get('manifest_path') != str(gate_manifest_path):
        raise PatternError('Original verification manifest reference changed.')
    gate_manifest, gate_manifest_hash = _read(gate_manifest_path)
    gate_files = gate_manifest.get('files')
    if (gate_manifest.get('profile') != 'verify' or gate_manifest.get('secure') is not True
            or not isinstance(gate_files, list) or len(gate_files) != 1 or not isinstance(gate_files[0], dict)
            or gate_files[0].get('path') != str(gate_path) or gate_files[0].get('sha256_after') != gate_hash):
        raise PatternError('Original verification file does not match its secure manifest.')
    source_matches = verify.is_current(root, home, gate)
    manifest_path = _reference(record.get('manifest_path'), home / '.tsmax/manifests', prefix='conductor-')
    manifest, manifest_hash = _read(manifest_path)
    files = manifest.get('files')
    if (manifest.get('profile') != 'conductor' or manifest.get('secure') is not True
            or not isinstance(files, list) or not 1 <= len(files) <= 24):
        raise PatternError('Applied source manifest is not a supported secure Conductor record.')
    for row in files:
        if not isinstance(row, dict) or row.get('secure') is not True or not isinstance(row.get('path'), str):
            raise PatternError('Applied source manifest is malformed.')
        path = Path(row['path'])
        if not path.is_absolute() or not path.is_relative_to(root) or path.relative_to(root).as_posix() not in task['files']:
            raise PatternError('Applied source manifest exceeds the reviewed task scope.')
    note = attempt.get('solution_note')
    verified_at = datetime.strptime(gate_path.name[:15], '%Y%m%dT%H%M%S').replace(tzinfo=timezone.utc).isoformat()
    evidence = {'task_id': task_id, 'plan_digest': digest, 'attempt_id': _text(attempt.get('id'), 'Attempt id', 180),
                'source_manifest': str(manifest_path), 'source_manifest_hash': manifest_hash,
                'gate_path': str(gate_path), 'gate_hash': gate_hash, 'gate_manifest_hash': gate_manifest_hash,
                'verified_at': verified_at, 'source_matches_verified_snapshot': source_matches,
                'source_fingerprint': gate['source_fingerprint'],
                'accepted_fingerprint': gate['accepted_fingerprint'], 'outcomes': reference['outcomes']}
    binding = {'plan': plan_hash, 'state': state_hash, 'gate': gate_hash, 'gate_manifest': gate_manifest_hash,
               'manifest': manifest_hash, 'source_matches_verified_snapshot': source_matches}
    return task, note, evidence, binding


def _render(record):
    return (f"# {record['title']}\n\n{HISTORY}\n\n## Applicability\n\n{record['applicability']}\n\n"
            f"## Approach\n\n{record['approach']}\n\n## Limitations\n\n{record['limitations']}\n\n"
            f"## Recorded evidence\n\nTask: {record['evidence']['task_id']}\n\n"
            f"Plan digest: {record['evidence']['plan_digest']}\n\n"
            f"Accepted fingerprint: {record['evidence']['accepted_fingerprint']}\n\n"
            "Evidence and source manifest references are in pattern.json. This is project-local data, not a tool permission grant.\n").encode()


def _validate(record, slug):
    expected = {'schema', 'slug', 'title', 'summary', 'approach', 'applicability', 'limitations', 'evidence', 'digest'}
    if not isinstance(record, dict) or set(record) != expected or type(record['schema']) is not int or record['schema'] != 1 or record['slug'] != slug:
        raise PatternError('Unsupported pattern record schema.')
    if not SLUG.fullmatch(slug) or record['digest'] != _digest({k: v for k, v in record.items() if k != 'digest'}):
        raise PatternError('Pattern content digest or slug does not match.')
    for key, limit in (('title', 160), ('summary', 400), ('approach', 4000), ('applicability', 6000), ('limitations', 4000)):
        _text(record[key], key, limit)
    evidence = record['evidence']
    fields = {'task_id', 'plan_digest', 'attempt_id', 'source_manifest', 'source_manifest_hash', 'gate_path', 'gate_hash',
              'gate_manifest_hash', 'verified_at', 'source_matches_verified_snapshot',
              'source_fingerprint', 'accepted_fingerprint', 'outcomes'}
    if not isinstance(evidence, dict) or set(evidence) != fields:
        raise PatternError('Malformed historical pattern evidence.')
    for key in ('plan_digest', 'source_manifest_hash', 'gate_hash', 'gate_manifest_hash', 'source_fingerprint', 'accepted_fingerprint'):
        if not _fingerprint(evidence[key]):
            raise PatternError('Malformed historical evidence fingerprint.')
    for key, limit in (('task_id', 40), ('attempt_id', 180), ('source_manifest', 4096), ('gate_path', 4096)):
        _text(evidence[key], key, limit)
    if not isinstance(evidence['source_matches_verified_snapshot'], bool) or not isinstance(evidence['verified_at'], str):
        raise PatternError('Malformed verification observation metadata.')
    try:
        datetime.fromisoformat(evidence['verified_at'])
    except ValueError:
        raise PatternError('Malformed original verification timestamp.') from None
    outcomes = evidence['outcomes']
    if not isinstance(outcomes, list) or not 1 <= len(outcomes) <= verify.MAX_CHECKS or not all(
            isinstance(row, dict) and set(row) == {'kind', 'status', 'exit_code', 'duration_s'}
            and isinstance(row['kind'], str) and row['kind'] in verify.KINDS and row['status'] == 'passed'
            and type(row['exit_code']) is int and row['exit_code'] == 0
            and isinstance(row['duration_s'], (int, float)) and not isinstance(row['duration_s'], bool)
            and row['duration_s'] >= 0 and (isinstance(row['duration_s'], int) or math.isfinite(row['duration_s']))
            for row in outcomes):
        raise PatternError('Malformed historical check outcomes.')


def suggest(root: Path, query: str, limit: int = 3) -> list[dict]:
    """Rank a bounded local library lexically; return summaries, never procedures."""
    root = pm._absolute(root)
    if not isinstance(query, str) or len(query) > 512 or not query.strip() or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 3:
        raise PatternError('Suggestion query must be 1–512 characters; limit must be 1–3.')
    words = set(re.findall(r'\w+', query.casefold())[:32])
    ranked = []
    try:
        with sf._parent(root / LIBRARY / '.enumerate') as (fd, _):
            names = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    if len(names) >= MAX_DIRECTORIES:
                        raise PatternError('Pattern library exceeds its directory scan bound; remove unused empty entries.')
                    if not SLUG.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
                        raise PatternError('Pattern library entries must be plain slug directories without links.')
                    names.append(entry.name)
            active = 0
            for slug in sorted(names):
                child = os.open(slug, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    contents = set()
                    with os.scandir(child) as children:
                        for entry in children:
                            contents.add(entry.name)
                            if len(contents) > 2:
                                break
                    if not contents:
                        continue  # Undo can intentionally leave an empty slug directory.
                    if contents != {'pattern.json', 'SKILL.md'}:
                        raise PatternError('Nonempty pattern directories must contain only their JSON record and SKILL.md.')
                    active += 1
                    if active > MAX_PATTERNS:
                        raise PatternError('Pattern library exceeds the 128-record bound.')
                    raw = sf._read_at(child, 'pattern.json', MAX_RECORD)
                    markdown = sf._read_at(child, 'SKILL.md', MAX_RECORD)
                finally:
                    os.close(child)
                if raw is None:
                    raise PatternError('Pattern directory lacks its bounded JSON record.')
                record = _decode(raw)
                _validate(record, slug)
                if markdown != _render(record):
                    raise PatternError('Pattern SKILL.md differs from its reviewed JSON record.')
                tokens = [set(re.findall(r'\w+', record[key].casefold())) for key in ('title', 'summary', 'applicability')]
                score = sum(weight * len(words & terms) for weight, terms in zip((4, 2, 1), tokens))
                if score:
                    ranked.append((score, slug, {'slug': slug, 'title': _one_line(record['title'], 160),
                        'summary': _one_line(record['summary']), 'historical': True, 'limitation': HISTORY}))
    except FileNotFoundError:
        return []
    return [row for _, _, row in sorted(ranked, key=lambda item: (-item[0], item[1]))[:limit]]


def save(root: Path, home: Path, task_id: str, *, title=None, approach=None, apply=False, review=None) -> dict:
    """Preview exact generated bytes; apply only a matching fresh review token."""
    from . import conductor as c
    root, home = pm._absolute(root), pm._absolute(home)
    if not isinstance(apply, bool):
        raise PatternError('Apply must be an explicit boolean choice.')
    if review is not None and not apply:
        raise PatternError('--review is only valid with --apply.')
    with c._exclusive(root) if apply else nullcontext():
        task, note, evidence, binding = _evidence(root, home, task_id)
        title = _one_line(_text(title if title is not None else task['title'], 'Title', 160), 160)
        approach = _text(approach if approach is not None else note, 'Approach (or use --approach)', 4000)
        base = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60] or 'pattern'
        slug = base.rstrip('-') + '-' + _digest({'task': task_id, 'plan': evidence['plan_digest']})[:12]
        observation = ('Current source matched the recorded snapshot when this pattern was saved.' if evidence['source_matches_verified_snapshot'] else
                       'Verified at recorded snapshot; current source differs or its current verification cannot be established.')
        record = {'schema': 1, 'slug': slug, 'title': title, 'summary': _one_line(title + ': ' + approach.splitlines()[0]),
                  'approach': approach, 'applicability': _text(task['spec'], 'Applicability', 6000),
                  'limitations': _text(HISTORY + '\n' + observation + '\nThis source-match observation is not a continuing guarantee.\nRecorded acceptance scope: ' + task['acceptance'], 'Limitations', 4000),
                  'evidence': evidence}
        record['digest'] = _digest(record)
        _validate(record, slug)
        contents = {'pattern.json': _encoded(record), 'SKILL.md': _render(record)}
        destination = root / LIBRARY / slug
        changes, before = [], {}
        for name, content in contents.items():
            if len(content) > MAX_RECORD:
                raise PatternError('Generated pattern exceeds the local record limit.')
            path = destination / name
            prior = sf.read_bytes(path, MAX_RECORD)
            before[name] = _hash(prior) if prior is not None else None
            if prior != content:
                changes.append({'path': str(path), 'kind': 'modify' if prior is not None else 'create', 'content': content,
                                'private': True, 'secure': True, 'preimage': before[name],
                                'reason': 'Save an explicitly reviewed historical verified procedure.'})
        token = _digest({'destination': str(destination), 'content': {name: _hash(data) for name, data in contents.items()},
                         'preimages': before, 'evidence': binding})
        command = ['tsmax', 'skills', 'save', task_id, '--root', str(root), '--home', str(home),
                   '--title', title, '--approach', approach, '--apply', '--review', token]
        result = {'status': 'preview', 'destination': str(destination), 'review': token, 'candidate': record,
                  'skill_markdown': contents['SKILL.md'].decode(), 'historical': True, 'limitations': [HISTORY],
                  'apply_command': shlex.join(command)}
        if not apply:
            return result
        if not isinstance(review, str) or review != token:
            raise PatternError('Exact pattern review token is missing or stale; preview again before applying.')
        if not changes:
            return {**result, 'status': 'unchanged', 'manifest_path': None}
        pm._safe_write_paths(root, home)
        written = apply_changes(Context(root=root, home=home), changes, stamp='pattern-' + uuid.uuid4().hex, profile='patterns')
        return {**result, 'status': 'saved', 'manifest_path': written['manifest_path']}
