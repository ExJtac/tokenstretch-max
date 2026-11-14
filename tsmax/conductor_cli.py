"""Explicit authorization boundaries for provider-neutral Conductor commands."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from . import conductor


def add_parser(subparsers):
    plan = subparsers.add_parser('plan', help='preview or authorize a structured top-tier plan through your selected AI CLI')
    plan.add_argument('goal')
    plan.add_argument('--tool', required=True, help='registered adapter id; no provider is selected automatically')
    plan.add_argument('--run', action='store_true', help='authorize one top-tier planning call through your existing CLI login')
    run = subparsers.add_parser('run', help='preview the plan, authorize bounded work, or apply an exact reviewed task')
    authorization = run.add_mutually_exclusive_group()
    authorization.add_argument('--prepare', action='store_true', help='create or inspect a retained worktree without model calls; never installs or shares dependencies')
    authorization.add_argument('--execute', action='store_true', help='authorize bounded worker calls and the exact required project checks in the saved plan')
    authorization.add_argument('--apply', metavar='TASK', help='apply an exact reviewed task, then rerun its approved checks in the original project')
    run.add_argument('--review', help='exact review token shown with the verified diff; required with --apply')
    run.add_argument('--max-calls', type=int, help='explicit persisted total worker call cap (1–72); retries/escalation count')
    for parser in (plan, run):
        parser.add_argument('--root', type=Path)
        parser.add_argument('--home', type=Path)
        parser.add_argument('--timeout', type=int, default=900)
        parser.add_argument('--json', action='store_true')


def _display(result):
    print('Conductor: ' + result['status'])
    if result.get('operation') == 'plan':
        print(f"Tool: {result['tool']}; tier: {result['tier']}. Preview only; --run authorizes one planning call.")
        print(result['prompt'])
    if result.get('models'):
        print('Approved model tiers: ' + json.dumps(result['models']))
    if result.get('tasks'):
        print('Review task scopes and exact project commands before --execute --max-calls N:')
        for task in result['tasks']:
            print(f"{task['id']}: {task['title']} | files: {', '.join(task['files'])}")
            for check in task['checks']:
                print('  ' + check['kind'] + ': ' + json.dumps(check['argv']))
    if result.get('plan'):
        print(conductor.render_plan(result['plan']))
    review = result.get('review')
    if review:
        print('Verified proposed changes (original project has not been changed):')
        print(review.get('diff') or '(no content diff)')
        print('Apply this exact reviewed task and rerun its approved checks in the original project: tsmax run --apply ' + review['task'] + ' --review ' + review['token'])
    if result.get('reason'):
        print(result['reason'])
    for limitation in result.get('limitations', []):
        print(limitation)
    if result.get('manifest_path'):
        print('Undo recorded change: tsmax undo ' + json.dumps(result['manifest_path']))
    if result.get('stage'):
        print('Retained worktree: ' + result['stage'])


def run(args):
    root = Path(os.path.abspath(args.root or Path.cwd()))
    home = Path(os.path.abspath(args.home or Path.home()))
    try:
        if not 1 <= args.timeout <= 1800:
            raise ValueError('Timeout must be from 1 to 1800 seconds.')
        if args.cmd == 'plan':
            result = conductor.plan(root, home, args.goal, args.tool, authorized=args.run, timeout=args.timeout)
        elif args.prepare:
            if args.review or args.max_calls is not None:
                raise ValueError('--prepare makes no model calls; use --max-calls only with --execute.')
            result = conductor.prepare(root, home)
        elif args.apply:
            if not args.review:
                raise ValueError('--apply requires the exact --review token shown with the diff.')
            result = conductor.apply_review(root, home, args.apply, args.review)
        else:
            if args.review:
                raise ValueError('--review is only valid with --apply.')
            result = conductor.execute(root, home, authorized=args.execute, max_calls=args.max_calls, timeout=args.timeout)
        print(json.dumps(result, ensure_ascii=True)) if args.json else _display(result)
        return 2 if result['status'] in {'needs-review', 'needs-preparation', 'call-limit', 'waiting'} else 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else 'Conductor state or local files could not be accessed safely; inspect before retrying.'
        print(json.dumps({'status': 'incomplete', 'error': message}) if args.json else message,
              file=sys.stdout if args.json else sys.stderr)
        return 2
