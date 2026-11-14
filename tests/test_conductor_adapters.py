"""Provider-neutral contract tests. No provider/model/auth requests are made."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time

import pytest

from tsmax.adapters.base import (Adapter, ConductorCapabilities, ConductorCommand, ConductorExecution,
                                ConductorRequest, ConductorResponse, HeadlessRefused)
from tsmax.adapters.claude_code import ClaudeCodeAdapter
from tsmax.adapters.codex import CodexAdapter
from tsmax.adapters.gemini_cli import GeminiCliAdapter
from tsmax.adapters.copilot_cli import CopilotCliAdapter
from tsmax.context import Context
from tsmax.conductor_runner import ConductorRunner, FakeConductorRunner, _run_argv, native_environment


class OtherProvider(Adapter):
    id = "synthetic-any-ai"

    def conductor_capabilities(self, ctx):
        return ConductorCapabilities(self.id, available=True, supported=True, executable='/synthetic/official',
            operations=('plan','task'), tiers={'top':'synthetic-big','mid':'synthetic-medium','light':'synthetic-small'},
            native_login=True, fresh_session=True, read_only_plan=True, workspace_task=True)

    def conductor_command(self, ctx, request, model):
        return ConductorCommand(('/synthetic/official', request.operation, model, request.prompt), request.cwd)

    def parse_conductor_output(self, execution, model):
        return ConductorResponse('completed' if execution.exit_code == 0 else 'failed', execution.stdout,
                                 requested_model=model, exit_code=execution.exit_code)


@pytest.fixture
def context(tmp_path):
    root=tmp_path/'worker';root.mkdir()
    home=tmp_path/'native-home';home.mkdir()
    return Context(home=home,root=root)


def request(ctx, **kwargs):
    return replace(ConductorRequest('task','Do a bounded task','mid',ctx.root,authorized=True),**kwargs)


def test_arbitrary_provider_and_explicit_override(context):
    adapter=OtherProvider(); runner=FakeConductorRunner([ConductorExecution(0,'done')])
    got=adapter.run_conductor(context,request(context,model_override='my-model'),runner=runner)
    assert got.status=='completed' and got.requested_model=='my-model'
    assert got.effective_model is None and got.usage is None
    assert runner.calls[0]['command'].argv[2]=='my-model'
    with pytest.raises(HeadlessRefused,match='queue exhausted'):
        adapter.run_conductor(context,request(context),runner=runner)


@pytest.mark.parametrize('operation',['plan','task'])
def test_authorization_before_probe_or_runner(context,operation,monkeypatch):
    adapter=OtherProvider()
    monkeypatch.setattr(adapter,'conductor_capabilities',lambda ctx:pytest.fail('probe before authorization'))
    with pytest.raises(HeadlessRefused,match='authorization'):
        adapter.run_conductor(context,request(context,operation=operation,authorized=False))


@pytest.mark.parametrize('field',['available','supported','native_login','fresh_session','workspace_task'])
def test_missing_capability_refuses_without_dispatch(context,field,monkeypatch):
    adapter=OtherProvider();caps=adapter.conductor_capabilities(context)
    monkeypatch.setattr(adapter,'conductor_capabilities',lambda ctx:replace(caps,**{field:False}))
    runner=FakeConductorRunner()
    assert adapter.run_conductor(context,request(context),runner=runner).status=='unsupported'
    assert runner.calls==[]


def test_plan_needs_read_only_and_safe_root(context,tmp_path,monkeypatch):
    adapter=OtherProvider();caps=adapter.conductor_capabilities(context)
    monkeypatch.setattr(adapter,'conductor_capabilities',lambda ctx:replace(caps,read_only_plan=False))
    assert adapter.run_conductor(context,request(context,operation='plan'),runner=FakeConductorRunner()).status=='unsupported'
    link=tmp_path/'link';link.symlink_to(context.root,target_is_directory=True)
    with pytest.raises((ValueError,HeadlessRefused)):
        adapter.run_conductor(context,request(context,cwd=link),runner=FakeConductorRunner())


@pytest.mark.parametrize('adapter',[OtherProvider(),ClaudeCodeAdapter(),CodexAdapter(),GeminiCliAdapter(),CopilotCliAdapter()])
def test_conductor_is_not_benchmark_bypass(context,adapter):
    with pytest.raises(HeadlessRefused):
        adapter.run_headless_task(context,{'prompt':'task'}, {'purpose':'conductor','bench':True,'authorized':True})
    with pytest.raises(HeadlessRefused):
        adapter.run_headless_task(context,{'prompt':'task'}, {'purpose':'conductor','home':str(context.home),'authorized':True})


def test_missing_gemini_copilot_are_unavailable_not_implicitly_supported(context,monkeypatch):
    monkeypatch.setattr('shutil.which',lambda name:None)
    for adapter in (GeminiCliAdapter(),CopilotCliAdapter()):
        caps=adapter.conductor_capabilities(context)
        assert not caps.available and not caps.supported and 'not installed' in caps.reason
        assert adapter.run_conductor(context,request(context),runner=FakeConductorRunner()).status=='unsupported'


def verified_probe(binary,**kwargs):
    if binary.name=='claude':
        return '2.1.323 (Claude Code)','--restricted --safe-mode --tools --strict-mcp-config --permission-prompts --no-session-persistence --output-format --max-turns',None
    return 'codex-cli 0.167.0-alpha.4.1','--ignore-user-config --ephemeral --json --sandbox read-only workspace-write',None


@pytest.fixture
def installed(context,monkeypatch):
    monkeypatch.setattr(context,'which',lambda name:Path('/synthetic')/name)
    monkeypatch.setattr('tsmax.conductor_runner.local_cli_evidence',verified_probe)
    return context


@pytest.mark.parametrize('adapter',[ClaudeCodeAdapter(),CodexAdapter()])
def test_commands_fresh_permission_preserving(installed,adapter):
    assert adapter.conductor_capabilities(installed).supported
    for operation in ('plan','task'):
        cmd=adapter.conductor_command(installed,request(installed,operation=operation),'explicit-model')
        assert cmd.cwd==installed.root and 'explicit-model' in cmd.argv
        assert not set(cmd.argv)&{'--bare','--dangerously-skip-permissions','--dangerously-bypass-approvals-and-sandbox','--add-dir','resume'}
        assert 'HOME' not in cmd.env
        if adapter.id=='codex':
            assert cmd.argv[cmd.argv.index('--sandbox')+1]==('read-only' if operation=='plan' else 'workspace-write')
            assert cmd.argv[cmd.argv.index('--ask-for-approval')+1]=='never'
        else:
            tools=cmd.argv[cmd.argv.index('--tools')+1].split(',')
            assert 'Bash' not in tools and 'Agent' not in tools
            assert ('Write' in tools)==(operation=='task')


def test_unverified_cli_version_refuses(installed,monkeypatch):
    monkeypatch.setattr('tsmax.conductor_runner.local_cli_evidence',lambda *a,**k:('new-version','',None))
    for adapter in (ClaudeCodeAdapter(),CodexAdapter()):
        caps=adapter.conductor_capabilities(installed)
        assert caps.available and not caps.supported


def test_claude_cumulative_usage_preserves_unknown_and_reasoning_subset():
    result={'type':'result','subtype':'success','is_error':False,'result':'done','total_cost_usd':.12,
            'modelUsage':{'resolved-model':{'inputTokens':10,'outputTokens':7,'thinkingTokens':2,'cacheReadInputTokens':20}}}
    got=ClaudeCodeAdapter().parse_conductor_output(ConductorExecution(0,json.dumps(result)),'alias')
    assert got.status=='completed' and got.effective_model=='resolved-model'
    assert got.usage['reasoning_tokens']==2 and got.usage['output_tokens']==7
    assert got.usage['cache_write_tokens'] is None and got.usage['reasoning_included_in_output']
    result.pop('modelUsage');result.pop('total_cost_usd')
    got=ClaudeCodeAdapter().parse_conductor_output(ConductorExecution(0,json.dumps(result)),'alias')
    assert got.effective_model is None and got.usage['estimated_cost_usd'] is None


def test_claude_multi_model_receipt_retains_counts_and_fails_ambiguity():
    usage={'requested':{'inputTokens':10,'outputTokens':7,'thinkingTokens':2,'cacheReadInputTokens':5},
           'other':{'inputTokens':5,'outputTokens':3,'thinkingTokens':1}}
    result={'type':'result','subtype':'success','is_error':False,'result':'done','total_cost_usd':.5,'modelUsage':usage}
    got=ClaudeCodeAdapter().parse_conductor_output(ConductorExecution(0,json.dumps(result)),'requested')
    assert got.status=='failed' and 'Ambiguous multi-model' in got.reason
    assert got.effective_model is None and got.usage['per_model']==usage
    assert got.usage['input_tokens']==15 and got.usage['output_tokens']==10 and got.usage['reasoning_tokens']==3
    assert got.usage['cache_read_tokens'] is None and got.usage['cache_write_tokens'] is None
    assert got.usage['estimated_cost_usd']==.5 and got.usage['reasoning_included_in_output']


@pytest.mark.parametrize('usage', [[], 'bad', {'model':None}, {'model':[]}, {'model':'bad'},
                                 {'model':{'inputTokens':'10'}}, {'model':{'inputTokens':True}},
                                 {'model':{'inputTokens':-1}}, {'model':{'outputTokens':1.5}}])
def test_claude_malformed_usage_returns_failed_receipt_not_traceback(usage):
    result={'type':'result','subtype':'success','is_error':False,'result':'done','modelUsage':usage}
    got=ClaudeCodeAdapter().parse_conductor_output(ConductorExecution(0,json.dumps(result)),'model')
    assert got.status=='failed' and 'Malformed' in got.reason and got.usage['per_model']==usage


def test_claude_partially_malformed_receipt_keeps_independent_known_totals():
    usage={'model':{'inputTokens':'bad','outputTokens':7,'thinkingTokens':2}}
    result={'type':'result','subtype':'success','is_error':False,'modelUsage':usage,'total_cost_usd':'bad'}
    got=ClaudeCodeAdapter().parse_conductor_output(ConductorExecution(0,json.dumps(result)),'model')
    assert got.status=='failed' and got.usage['input_tokens'] is None and got.usage['output_tokens']==7
    assert got.usage['reasoning_tokens']==2 and got.usage['estimated_cost_usd'] is None


def test_deeply_malformed_receipt_fails_without_parser_traceback():
    raw='{"type":"result","modelUsage":'+('['*2000)+'0'+(']'*2000)+'}'
    got=ClaudeCodeAdapter().parse_conductor_output(ConductorExecution(0,raw),'model')
    assert got.status=='failed'


def test_codex_final_output_usage_and_no_requested_model_invention():
    events=[{'type':'thread.started','thread_id':'fresh'},
            {'type':'item.completed','item':{'type':'agent_message','text':'done'}},
            {'type':'turn.completed','usage':{'input_tokens':100,'cached_input_tokens':60,'output_tokens':20}}]
    got=CodexAdapter().parse_conductor_output(ConductorExecution(0,'\n'.join(map(json.dumps,events))),'requested-model')
    assert got.status=='completed' and got.text=='done' and got.effective_model is None
    assert got.usage['input_includes_cache'] and got.usage['input_tokens']==100 and got.usage['cache_read_tokens']==60
    assert got.usage['estimated_cost_usd'] is None
    duplicated=ConductorExecution(0,'\n'.join(map(json.dumps,events+[events[-1]])))
    refused=CodexAdapter().parse_conductor_output(duplicated,'requested-model')
    assert refused.status=='failed' and refused.usage is None


@pytest.mark.parametrize('message,status',[('authentication required','auth-required'),('quota exceeded','quota-exhausted'),('unexpected failure','failed')])
def test_auth_and_quota_are_terminal_not_substitution(message,status):
    for adapter in (ClaudeCodeAdapter(),CodexAdapter()):
        got=adapter.parse_conductor_output(ConductorExecution(1,'',message),'explicit')
        assert got.status==status and got.requested_model=='explicit'


def test_native_home_and_pytest_execution_guard(context,monkeypatch):
    monkeypatch.setenv('HOME','/existing/login/home');monkeypatch.setenv('DO_NOT_COPY_SECRET','private')
    env=native_environment()
    assert env['HOME']=='/existing/login/home' and 'DO_NOT_COPY_SECRET' not in env
    with pytest.raises(HeadlessRefused):native_environment({'HOME':'/different'})
    with pytest.raises(HeadlessRefused,match='tests'):
        ConductorRunner().run(ConductorCommand(('/synthetic/official',),context.root),timeout_s=10)


def test_synthetic_timeout_group_and_output_bound(tmp_path):
    marker=tmp_path/'escaped'
    script='import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",'+repr('import time;from pathlib import Path;time.sleep(1);Path('+repr(str(marker))+').write_text("bad")')+']);time.sleep(10)'
    result=_run_argv([sys.executable,'-c',script],tmp_path,native_environment(),.2)
    assert result.stop_reason=='timeout';time.sleep(1.1);assert not marker.exists()
    result=_run_argv([sys.executable,'-c','print("x"*10000)'],tmp_path,native_environment(),5,max_output=500)
    assert result.stop_reason=='output-limit' and len(result.stdout)<=500


def test_diagnostics_redacted_no_automatic_retry(context):
    runner=FakeConductorRunner([ConductorExecution(1,stop_reason='failed Bearer secret-token'),ConductorExecution(0,'unused')])
    got=OtherProvider().run_conductor(context,request(context),runner=runner)
    assert got.status=='failed' and 'secret-token' not in got.reason and len(runner.calls)==1


def test_prompts_cannot_be_interpreted_as_cli_flags(installed):
    for adapter in (ClaudeCodeAdapter(),CodexAdapter()):
        cmd=adapter.conductor_command(installed,request(installed,prompt='--dangerously-skip-permissions'),'explicit')
        assert cmd.argv[-2:]==('--','--dangerously-skip-permissions')


def test_unknown_or_flag_shaped_model_never_substitutes(context):
    runner=FakeConductorRunner()
    got=OtherProvider().run_conductor(context,request(context,model_override='--bad-option'),runner=runner)
    assert got.status=='unsupported' and runner.calls==[]


def test_effective_model_mismatch_fails_without_retry(context,monkeypatch):
    adapter=OtherProvider()
    monkeypatch.setattr(adapter,'parse_conductor_output',lambda execution,model:ConductorResponse('completed','done',model,'other-model'))
    runner=FakeConductorRunner([ConductorExecution(0,'done')])
    got=adapter.run_conductor(context,request(context),runner=runner)
    assert got.status=='failed' and 'different effective model' in got.reason and len(runner.calls)==1
    claude=ClaudeCodeAdapter()
    assert claude.conductor_model_matches('sonnet','claude-sonnet-5-5')
    assert not claude.conductor_model_matches('sonnet','claude-opus-5-5')
    assert not claude.conductor_model_matches('full-model','another-model')


def test_codex_project_and_ancestor_config_refused_without_read(installed,tmp_path,monkeypatch):
    adapter=CodexAdapter()
    directory=installed.root/'.codex';directory.mkdir();(directory/'config.toml').write_text('private contents must never be read')
    monkeypatch.setattr(Path,'read_text',lambda *a,**k:pytest.fail('configuration opened'))
    assert not adapter.conductor_capabilities(installed).supported
    with pytest.raises(HeadlessRefused):adapter.conductor_command(installed,request(installed),'model')
    (directory/'config.toml').unlink();directory.rmdir();(installed.root.parent/'.codex').mkdir()
    assert not adapter.conductor_capabilities(installed).supported


@pytest.mark.parametrize('key',['HOME','CODEX_HOME','HTTP_PROXY','ANTHROPIC_API_KEY','NODE_OPTIONS','DISABLE_SANDBOX'])
def test_generic_environment_rejects_profile_secret_permission_overrides(key):
    with pytest.raises(HeadlessRefused):native_environment({key:'1'})


def test_legacy_dict_entrypoint_returns_typed_contract_shape(context):
    runner=FakeConductorRunner([ConductorExecution(0,'done')])
    got=OtherProvider().run_headless_task(context,{'operation':'plan','prompt':'plan','tier':'top'},
        {'purpose':'conductor','authorized':True,'sandbox_dir':str(context.root),'runner':runner})
    assert got['status']=='completed' and got['requested_model']=='synthetic-big' and got['effective_model'] is None
