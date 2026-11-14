"""Historical patterns use real local manifests and fake, explicitly trusted gates."""
import hashlib
import json
from pathlib import Path
import sys
import uuid

import pytest

from tsmax import conductor as c, patterns as p, verify
from tsmax.adapters.base import ConductorCapabilities
from tsmax.cli import main
from tsmax.context import Context
from tsmax.installer import apply_changes, undo_manifest


class Gate:
    name = 'fixture-offline'
    def prepare(self, root, home): return True
    def run(self, check, root, home, timeout):
        return {'status':'passed','exit_code':0,'duration_s':.01,'output':'1 passed','reason':None}


@pytest.fixture
def completed(tmp_path):
    root, home = tmp_path/'project', tmp_path/'home'
    root.mkdir();home.mkdir();(root/'app.py').write_text('answer = 42\n')
    source = verify.snapshot(root, home)['digest']
    task={'id':'T1','title':'Normalize URL filters','spec':'Share URL query parsing across project and customer lists.',
          'depends_on':[],'files':['app.py'],'acceptance':'URL filters survive reload and required tests pass.',
          'checks':[{'kind':'test','argv':[sys.executable,'-m','pytest','-q']}]}
    plan={'schema':1,'goal':'URL list controls','tool':'arbitrary-ai','source_fingerprint':source,'tasks':[task],
          'usage':None,'created_at':'fixture','models':{'mid':'any-medium','top':'any-large'},'adapter_version':None,
          'requested_model':None,'effective_model':None}
    plan['digest']=c._digest(plan)
    source_manifest=apply_changes(Context(root=root,home=home),[{'path':str(root/'app.py'),'kind':'modify',
        'content':'answer = 43\n','secure':True,'private':True,'preimage':hashlib.sha256((root/'app.py').read_bytes()).hexdigest()}],
        stamp='conductor-'+uuid.uuid4().hex,profile='conductor')
    gate=verify.verify(root,home,{'checks':c._checks(task),'unresolved':[],'notes':[]},authorized=True,runner=Gate())
    assert gate['status']=='passed' and verify.is_current(root,home,gate), (gate.get('reason'), gate.get('scope'), gate.get('results'))
    state={'schema':1,'status':'complete','plan_digest':plan['digest'],'tasks':{'T1':'done'},
           'attempts':[{'task':'T1','id':'attempt-fixture','status':'verified','provider_status':'completed',
                        'solution_note':'Normalize query values in one pure helper, then restore list controls from the URL.'}],
           'applied':[{'task':'T1','manifest_path':source_manifest['manifest_path'],'original_gate':c._gate_reference(gate)}]}
    (root/c.PLAN_JSON).write_text(json.dumps(plan));(root/c.STATE_JSON).write_text(json.dumps(state))
    return root,home,plan,state,gate


def update_state(world, change):
    root,home,plan,state,gate=world;change(state);(root/c.STATE_JSON).write_text(json.dumps(state))


def apply_preview(world, **kwargs):
    root,home,*_=world;preview=p.save(root,home,'T1',**kwargs)
    return p.save(root,home,'T1',apply=True,review=preview['review'],**kwargs)


def test_preview_is_readonly_and_apply_undo_roundtrip(completed,monkeypatch):
    root,home,*_=completed
    before={str(path):path.read_bytes() for base in (root,home) for path in base.rglob('*') if path.is_file()}
    preview=p.save(root,home,'T1')
    after={str(path):path.read_bytes() for base in (root,home) for path in base.rglob('*') if path.is_file()}
    assert before==after and preview['status']=='preview' and not (root/p.LIBRARY).exists()
    assert preview['candidate']['evidence']['source_matches_verified_snapshot'] is True
    saved=p.save(root,home,'T1',apply=True,review=preview['review'])
    destination=Path(saved['destination'])
    assert saved['status']=='saved' and (destination/'SKILL.md').exists()
    assert (destination/'pattern.json').stat().st_mode & 0o777==0o600
    record=json.loads((destination/'pattern.json').read_text())
    assert record['evidence']['source_manifest'] and record['evidence']['verified_at']
    assert p.suggest(root,'URL query')[0]['slug']==record['slug']
    undo_manifest(Path(saved['manifest_path']))
    assert not (destination/'pattern.json').exists() and not (destination/'SKILL.md').exists()
    assert p.suggest(root,'URL query')==[]
    assert (root/'app.py').read_text()=='answer = 43\n'


def test_historical_save_after_source_change_requires_explicit_label(completed):
    root,home,*_=completed;(root/'unrelated.py').write_text('new_value = 1\n')
    preview=p.save(root,home,'T1')
    assert preview['candidate']['evidence']['source_matches_verified_snapshot'] is False
    assert 'Verified at recorded snapshot; current source differs' in preview['skill_markdown']
    saved=p.save(root,home,'T1',apply=True,review=preview['review'])
    assert saved['status']=='saved'
    assert p.suggest(root,'URL')[0]['historical'] is True


@pytest.mark.parametrize('changed',['source','state','destination','override'])
def test_review_token_binds_observation_evidence_destination_and_candidate(completed,changed):
    root,home,*_=completed;preview=p.save(root,home,'T1')
    kwargs={}
    if changed=='source':(root/'app.py').write_text('answer = 99\n')
    elif changed=='state':update_state(completed,lambda state:state['attempts'][-1].update(solution_note='Different reviewed approach.'))
    elif changed=='destination':
        destination=Path(preview['destination']);destination.mkdir(parents=True);(destination/'SKILL.md').write_text('intervening text')
    else:kwargs={'approach':'Another solution.'}
    with pytest.raises(p.PatternError,match='token'):
        p.save(root,home,'T1',apply=True,review=preview['review'],**kwargs)


@pytest.mark.parametrize('state_change',[
    lambda state:state['tasks'].update(T1='review-ready'),
    lambda state:state.update(plan_digest='0'*64),
    lambda state:state['attempts'][-1].update(status='failed'),
    lambda state:state['attempts'][-1].update(provider_status='failed'),
    lambda state:state['applied'][0]['original_gate'].update(status='failed'),
    lambda state:state['applied'][0]['original_gate'].update(accepted_fingerprint='0'*64),
    lambda state:state['applied'].append(dict(state['applied'][0])),
])
def test_unverified_or_inconsistent_task_cannot_save(completed,state_change):
    update_state(completed,state_change)
    with pytest.raises(ValueError):p.save(completed[0],completed[1],'T1')
    assert not (completed[0]/p.LIBRARY).exists()


def test_forged_gate_receipt_fails_even_with_matching_state(completed):
    root,home,plan,state,gate=completed
    path=Path(gate['result_path']);stored=json.loads(path.read_text());stored['source_fingerprint']='0'*64
    stored['accepted_fingerprint']=hashlib.sha256(json.dumps({'source':stored['source_fingerprint'],'checks':stored['checks'],
        'tools':[row.get('tool') for row in stored['results']]},sort_keys=True).encode()).hexdigest()
    path.write_text(json.dumps(stored))
    update_state(completed,lambda state:state['applied'][0]['original_gate'].update(source_fingerprint=stored['source_fingerprint'],accepted_fingerprint=stored['accepted_fingerprint']))
    with pytest.raises(p.PatternError,match='secure manifest'):p.save(root,home,'T1')


def test_gate_and_manifest_paths_cannot_read_credentials(completed,tmp_path,monkeypatch):
    root,home,*_=completed;secret=tmp_path/'.env';secret.write_text('CANARY_NEVER_READ')
    original=p.sf.read_bytes
    def guarded(path,*args,**kwargs):
        assert Path(path)!=secret,'secret file opened'
        return original(path,*args,**kwargs)
    monkeypatch.setattr(p.sf,'read_bytes',guarded)
    update_state(completed,lambda state:state['applied'][0]['original_gate'].update(result_path=str(secret)))
    with pytest.raises(p.PatternError,match='reference'):p.save(root,home,'T1')


def test_gate_symlink_and_hardlink_refused(completed,tmp_path):
    root,home,_,_,gate=completed;path=Path(gate['result_path']);data=path.read_bytes();path.unlink()
    other=tmp_path/'other.json';other.write_bytes(data);path.symlink_to(other)
    with pytest.raises(OSError):p.save(root,home,'T1')
    path.unlink();path.hardlink_to(other)
    with pytest.raises(OSError):p.save(root,home,'T1')


def test_no_note_requires_concise_override_and_redacts_source_text(completed):
    root,home,*_=completed;update_state(completed,lambda state:state['attempts'][-1].pop('solution_note'))
    with pytest.raises(p.PatternError,match='Approach'):p.save(root,home,'T1')
    preview=p.save(root,home,'T1',title='Reusable query parsing',approach='Normalize tokens. password=secretvalue CANARY_PRIVATE sk-abcdefghijk')
    text=json.dumps(preview)
    assert 'secretvalue' not in text and 'CANARY_PRIVATE' not in text and 'sk-abcdefghijk' not in text
    assert '--title' in preview['apply_command'] and '--approach' in preview['apply_command']


@pytest.mark.parametrize('bad',['duplicate','schema','oversized','symlink'])
def test_library_rejects_invalid_records_before_suggestion(completed,bad,tmp_path):
    saved=apply_preview(completed);root=completed[0];path=Path(saved['destination'])/'pattern.json'
    if bad=='duplicate':path.write_text('{"schema":1,"schema":1}')
    elif bad=='schema':path.write_text('{"schema":99}')
    elif bad=='oversized':path.write_bytes(b' '* (p.MAX_RECORD+1))
    else:
        path.unlink();secret=tmp_path/'.env';secret.write_text('CANARY_PRIVATE');path.symlink_to(secret)
    with pytest.raises((ValueError,OSError)):p.suggest(root,'query')


def test_library_symlink_ancestor_refused(completed,tmp_path):
    root,home,*_=completed;other=tmp_path/'elsewhere';other.mkdir();(root/p.LIBRARY).symlink_to(other,target_is_directory=True)
    with pytest.raises(OSError):p.suggest(root,'query')
    with pytest.raises(OSError):p.save(root,home,'T1')


def test_suggestions_are_bounded_deterministic_summaries_only(completed):
    root,home,*_=completed
    for title in ['URL query gamma','URL query alpha','URL query delta','URL query beta']:
        apply_preview(completed,title=title)
    first=p.suggest(root,'URL query');second=p.suggest(root,'URL query')
    assert first==second and len(first)==3 and [r['slug'] for r in first]==sorted(r['slug'] for r in first)
    assert all(set(row)=={'slug','title','summary','historical','limitation'} for row in first)
    assert not any('approach' in row or 'evidence' in row for row in first)
    with pytest.raises(ValueError):p.suggest(root,'x'*513)
    with pytest.raises(ValueError):p.suggest(root,'query',limit=4)


def test_library_scan_bound_and_empty_readonly(tmp_path,monkeypatch):
    root=tmp_path/'project';root.mkdir()
    assert p.suggest(root,'query')==[] and list(root.iterdir())==[]
    library=root/p.LIBRARY;library.mkdir(parents=True)
    for i in range(3):(library/f'pattern-{i}').mkdir()
    monkeypatch.setattr(p,'MAX_DIRECTORIES',2)
    with pytest.raises(p.PatternError,match='bound'):p.suggest(root,'query')


def test_duplicate_evidence_json_and_oversized_state_refused(completed):
    root,home,*_=completed;path=root/c.STATE_JSON;raw=path.read_text();path.write_text(raw[:-1]+',"schema":1}')
    with pytest.raises(p.PatternError):p.save(root,home,'T1')
    path.write_bytes(b' '*(p.MAX_STATE+1))
    with pytest.raises(OSError):p.save(root,home,'T1')


def test_second_identical_save_is_noop_and_changed_save_undo(completed):
    root,home,*_=completed;saved=apply_preview(completed)
    prior=(Path(saved['destination'])/'pattern.json').read_bytes()
    repeat=p.save(root,home,'T1');count=len(list((home/'.tsmax/manifests').iterdir()))
    assert p.save(root,home,'T1',apply=True,review=repeat['review'])['status']=='unchanged'
    assert len(list((home/'.tsmax/manifests').iterdir()))==count
    changed=apply_preview(completed,approach='Use a deterministic query parser and explicit history updates.')
    assert (Path(changed['destination'])/'pattern.json').read_bytes()!=prior
    undo_manifest(Path(changed['manifest_path']))
    assert (Path(changed['destination'])/'pattern.json').read_bytes()==prior


def test_cli_preview_apply_and_no_automatic_save(completed,capsys):
    root,home,*_=completed
    args=['skills','save','T1','--root',str(root),'--home',str(home),'--json']
    assert main(args)==0;preview=json.loads(capsys.readouterr().out)
    assert preview['status']=='preview' and not (root/p.LIBRARY).exists()
    assert main(args+['--apply'])==2;capsys.readouterr()
    assert main(args+['--apply','--review',preview['review']])==0;capsys.readouterr()
    assert main(['skills','suggest','URL','--root',str(root),'--json'])==0
    assert len(json.loads(capsys.readouterr().out))==1


def test_plan_includes_only_historical_summaries_without_mutation(completed):
    root,home,*_=completed;saved=apply_preview(completed)
    class NoProviderCall:
        id='arbitrary-ai'
        def conductor_capabilities(self,ctx):
            return ConductorCapabilities(self.id,available=True,supported=True,operations=('plan',),tiers={'top':'top-model'},
                native_login=True,fresh_session=True,read_only_plan=True)
        def run_headless_task(self,*args,**kwargs):pytest.fail('preview invoked provider')
    before={str(path):path.read_bytes() for path in root.rglob('*') if path.is_file()}
    result=c.plan(root,home,'Improve URL query handling','arbitrary-ai',adapter=NoProviderCall())
    assert result['pattern_suggestions'] and 'HISTORICAL PATTERN SUMMARIES' in result['prompt']
    assert 'not evidence that this new task is verified' in result['prompt'] and 'source_manifest' not in result['prompt']
    assert before=={str(path):path.read_bytes() for path in root.rglob('*') if path.is_file()}
    undo_manifest(Path(saved['manifest_path']))
    empty=c.plan(root,home,'Improve URL query handling','arbitrary-ai',adapter=NoProviderCall())
    assert empty['pattern_suggestions']==[]


def test_empty_undo_directory_is_ignored_but_nonempty_malformed_entry_refuses(completed):
    root,home,*_=completed;saved=apply_preview(completed);destination=Path(saved['destination'])
    undo_manifest(Path(saved['manifest_path']))
    destination.mkdir(parents=True,exist_ok=True)
    assert p.suggest(root,'URL')==[]
    (destination/'unreviewed.txt').write_text('not a saved record')
    with pytest.raises(p.PatternError,match='Nonempty'):p.suggest(root,'URL')


def test_skill_markdown_link_or_edit_cannot_be_used_as_verified_pattern(completed,tmp_path):
    root=completed[0];saved=apply_preview(completed);markdown=Path(saved['destination'])/'SKILL.md'
    markdown.write_text('unreviewed instructions')
    with pytest.raises(p.PatternError,match='differs'):p.suggest(root,'URL')
    markdown.unlink();secret=tmp_path/'.env';secret.write_text('CANARY_SECRET');markdown.symlink_to(secret)
    with pytest.raises(OSError):p.suggest(root,'URL')
