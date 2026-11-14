"""Synthetic usage metadata only; no real logs, prompts, credentials, or provider calls."""
import json
from pathlib import Path

import pytest

from tsmax.adapters.codex import CodexAdapter, CodexRolloutParser
from tsmax.context import Context
from tsmax.logcache import LogCache


def usage(n=1):
    return dict(input_tokens=100*n, cached_input_tokens=80*n,
                cache_write_input_tokens=5*n, output_tokens=10*n,
                reasoning_output_tokens=3*n, total_tokens=110*n)


def native(response='r1', thread='child', amount=1, cumulative=1):
    return {'type': 'token_usage_record', 'timestamp': '2026-11-14T09:00:00Z',
            'payload': {'thread_id': thread, 'session_id': 'root-session',
                        'response_id': response, 'usage': usage(amount),
                        'thread_token_usage': usage(cumulative) if cumulative else {}}}


def count(amount=1, cumulative=1, thread=None, last=True):
    info = {'total_token_usage': usage(cumulative)} if cumulative else {}
    if last:
        info['last_token_usage'] = usage(amount)
    return {'type': 'event_msg', 'timestamp': '2026-11-14T09:00:00.100Z',
            'payload': {'type': 'token_count', 'info': info,
                        **({'thread_id': thread} if thread else {})}}


def parser():
    p = CodexRolloutParser(Path('synthetic.jsonl'), 'codex')
    p.feed_obj({'type': 'session_meta', 'payload': {'id': 'root-session'}})
    p.feed_obj({'type': 'turn_context', 'payload': {'model': 'synthetic'}})
    return p


def tokens(records):
    return sum(r.input_tokens + r.cache_read_tokens + r.cache_write_tokens +
               r.output_tokens + r.reasoning_tokens for r in records)


@pytest.mark.parametrize('native_first', [False, True])
@pytest.mark.parametrize('has_native_cumulative', [False, True])
def test_both_event_orders_count_response_once(native_first, has_native_cumulative):
    p = parser()
    n = native(cumulative=1 if has_native_cumulative else None)
    events = [n, count()] if native_first else [count(), n]
    # Split feeds model an append between incremental cache polls.
    for event in events:
        p.feed([json.dumps(event).encode()])
        assert len(p.records) == 1
    r, = p.records
    assert r.session_id == 'child'
    assert r.task_id == 'r1'
    assert (r.input_tokens, r.cache_read_tokens, r.cache_write_tokens,
            r.output_tokens, r.reasoning_tokens) == (15, 80, 5, 7, 3)
    assert tokens(p.records) == 110
    p.feed_obj(count())  # unchanged cumulative/rate-limit snapshot
    p.feed_obj(n)  # repeated native response ID
    assert tokens(p.records) == 110


@pytest.mark.parametrize('native_first', [False, True])
def test_equal_amounts_are_distinct_responses(native_first):
    p = parser()
    for i in range(1, 4):
        events = [native('r'+str(i), cumulative=i), count(cumulative=i)]
        for event in events if native_first else reversed(events):
            p.feed_obj(event)
    assert len(p.records) == 3
    assert tokens(p.records) == 330
    assert {r.task_id for r in p.records} == {'r1', 'r2', 'r3'}


def test_native_only_responses_use_thread_identity_across_files():
    records = []
    for thread in ['a', 'b', 'a']:
        p = parser()
        p.feed_obj(native('r1', thread))
        p.feed_obj(native('r2', thread, cumulative=2))
        records.extend(p.records)
    finished = CodexAdapter().finish_usage_records(records)
    assert [(r.session_id, r.task_id) for r in finished] == [
        ('a', 'r1'), ('a', 'r2'), ('b', 'r1'), ('b', 'r2')]
    assert tokens(finished) == 440


def test_turn_id_is_not_a_response_identity():
    p = parser()
    for _ in range(2):
        event = native()
        del event['payload']['response_id']
        event['payload']['turn_id'] = 'one-turn-many-responses'
        p.feed_obj(event)
    assert len(p.records) == 2


def test_disagreeing_cumulative_totals_do_not_pair_equal_amounts():
    p = parser()
    p.feed_obj(native(cumulative=1))
    p.feed_obj(count(cumulative=2))
    assert len(p.records) == 2


def test_cumulative_only_snapshots_use_deltas_and_handle_reset():
    p = parser()
    for total in [2, 2, 5, 1, 1, 3]:
        p.feed_obj(count(cumulative=total, last=False))
    assert [r.total_input() for r in p.records] == [200, 300, 100, 200]
    assert tokens(p.records) == 880


def test_counts_are_independent_across_threads():
    p = parser()
    for thread, total in [('a', 2), ('b', 2), ('a', 3), ('b', 2)]:
        p.feed_obj(count(cumulative=total, last=False, thread=thread))
    assert [(r.session_id, r.total_input()) for r in p.records] == [
        ('a', 200), ('b', 200), ('a', 100)]


def test_last_usage_only_does_not_drop_equal_genuine_records():
    p = parser()
    p.feed_obj(count(cumulative=None))
    p.feed_obj(count(cumulative=None))
    assert len(p.records) == 2
    assert tokens(p.records) == 220


def test_legacy_one_line_normalizes_inclusive_output():
    p = parser()
    p.feed_obj({'usage': usage(), 'model': 'legacy'})
    assert tokens(p.records) == 110
    assert p.records[0].output_tokens == 7


def test_incremental_logcache_keeps_identity_when_native_arrives_later(tmp_path):
    ctx = Context(home=tmp_path / 'home', root=tmp_path / 'root', env={})
    folder = ctx.home / '.codex' / 'sessions'
    folder.mkdir(parents=True)
    log = folder / 'synthetic.jsonl'
    log.write_text(json.dumps(count())+'\n')
    cache = LogCache()
    adapter = CodexAdapter()
    assert tokens(cache.records(ctx, [adapter])) == 110
    with log.open('a') as f:
        f.write(json.dumps(native())+'\n')
    records = cache.records(ctx, [adapter])
    assert tokens(records) == 110
    assert records[0].session_id == 'child'
    assert records[0].task_id == 'r1'
    assert cache.stats['appended'] == 1


def test_old_disk_memo_is_invalidated(tmp_path, monkeypatch):
    from tsmax import logcache
    monkeypatch.setattr(logcache, '_DISK', None)
    path = tmp_path / 'cache.json'
    path.write_text(json.dumps({'v': [1, 'same-release'], 'rows': {'old': [[], 220]}}))
    logcache.open_disk_memo(path, 'same-release')
    assert logcache._DISK['rows'] == {}


def test_equal_usage_in_new_turn_is_not_an_old_envelope_pair():
    p = parser()
    p.feed_obj(count())
    p.feed_obj({'type': 'turn_context', 'payload': {'model': 'synthetic'}})
    p.feed_obj(native(cumulative=None))
    assert len(p.records) == 2


def test_native_session_fallback_when_thread_id_is_missing():
    p = parser()
    p.feed_obj(count())
    event = native()
    del event['payload']['thread_id']
    p.feed_obj(event)
    assert len(p.records) == 1
    assert p.records[0].session_id == 'root-session'


def test_new_session_metadata_keeps_cumulative_history_separate():
    p = parser()
    p.feed_obj(native(thread='a'))
    p.feed_obj(count())
    p.feed_obj({'type': 'session_meta', 'payload': {'id': 'new-root'}})
    p.feed_obj(count())
    p.feed_obj(native(thread='b'))
    p.feed_obj(count())
    assert [r.session_id for r in p.records] == ['a', 'b']
    assert tokens(p.records) == 220


def test_native_identity_does_not_relabel_prior_session_segment():
    p = parser()
    p.feed_obj(count())
    p.feed_obj({'type': 'session_meta', 'payload': {'id': 'root-session'}})
    p.feed_obj(count(cumulative=2))
    p.feed_obj(native(thread='child', cumulative=2))
    assert [r.session_id for r in p.records] == ['root-session', 'child']


def test_native_coverage_precedes_cumulative_only_increase():
    p = parser()
    p.feed_obj(native())
    p.feed_obj(count(cumulative=2, last=False))
    assert tokens(p.records) == 220
    assert len(p.records) == 2


def test_multiple_native_responses_precede_cumulative_only_mirror():
    p = parser()
    p.feed_obj(native('r1', cumulative=1))
    p.feed_obj(native('r2', cumulative=2))
    p.feed_obj(count(cumulative=2, last=False))
    p.feed_obj(count(cumulative=3, last=False))
    assert tokens(p.records) == 330
    assert len(p.records) == 3


@pytest.mark.parametrize('responses', [(1, 2), (2, 1)])
def test_cumulative_aggregate_replaced_by_native_responses_in_either_order(responses):
    p = parser()
    p.feed_obj(count(cumulative=2, last=False))
    for i in responses:
        p.feed_obj(native('r'+str(i), cumulative=i))
    assert tokens(p.records) == 220
    assert len(p.records) == 2
    assert {r.task_id for r in p.records} == {'r1', 'r2'}
    # The late older native envelope must not lower the cumulative baseline.
    p.feed_obj(count(cumulative=3, last=False))
    assert tokens(p.records) == 330


def test_cumulative_aggregate_keeps_unidentified_residual():
    p = parser()
    p.feed_obj(count(cumulative=2, last=False))
    p.feed_obj(native('r2', cumulative=2))
    assert tokens(p.records) == 220
    assert len(p.records) == 2
    assert tokens([r for r in p.records if r.task_id is None]) == 110
    assert tokens([r for r in p.records if r.task_id == 'r2']) == 110


def test_native_response_replaces_only_its_thread_aggregate():
    p = parser()
    p.feed_obj(count(cumulative=2, thread='a', last=False))
    p.feed_obj(native(thread='b'))
    p.feed_obj(count(cumulative=1, thread='b', last=False))
    assert tokens(p.records) == 330
    assert tokens([r for r in p.records if r.session_id == 'a']) == 220
    assert tokens([r for r in p.records if r.session_id == 'b']) == 110


def test_native_and_cumulative_only_counter_reset():
    p = parser()
    p.feed_obj(native('old', amount=2, cumulative=2))
    p.feed_obj(count(cumulative=2, last=False))
    p.feed_obj(native('reset', cumulative=1))
    p.feed_obj(count(cumulative=1, last=False))
    p.feed_obj(count(cumulative=2, last=False))
    assert tokens(p.records) == 440
    assert len(p.records) == 3


def test_native_mirror_does_not_consume_next_equal_legacy_response():
    p = parser()
    p.feed_obj(native())
    p.feed_obj(count())
    p.feed_obj(count(cumulative=None))
    assert tokens(p.records) == 220
    assert len(p.records) == 2
