"""Verification CLI: preview discovery, then explicitly authorize checks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from . import verify


def add_parser(subparsers):
    parser = subparsers.add_parser('verify', help='preview or explicitly run offline project checks')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--home', type=Path)
    parser.add_argument('--run', action='store_true', help='authorize trusted synchronous project checks; detached/background jobs are unsupported')
    parser.add_argument('--check', nargs=2, action='append', default=[], metavar=('KIND', 'JSON_ARGV'),
                        help='explicit required command, replacing detected commands of this kind')
    parser.add_argument('--timeout', type=int, default=120, help='per-check timeout in seconds (1–1800)')
    parser.add_argument('--json', action='store_true')


def run(args):
    root = Path(os.path.abspath(args.root or Path.cwd()))
    home = Path(os.path.abspath(args.home or Path.home()))
    try:
        explicit = []
        for kind, value in args.check:
            try:
                argv = json.loads(value)
            except ValueError:
                raise ValueError('--check needs a JSON array of command arguments.') from None
            if not isinstance(argv, list):
                raise ValueError('--check needs a JSON array of command arguments.')
            explicit.append(verify.Check(kind, tuple(argv)))
        selection = verify.select(verify.detect(root, home), explicit)
        if args.run and not args.json:
            for check in selection['checks']:
                print(f'{check.kind}: {json.dumps(list(check.argv))}', flush=True)
        result = verify.verify(root, home, selection, authorized=args.run, timeout=args.timeout)
        if args.json:
            print(json.dumps(result, ensure_ascii=True))
        else:
            print('Verification: ' + result['status'])
            if not args.run:
                for check in result['checks']:
                    print(f"{check['kind']}: {json.dumps(check['argv'])}")
                print('Preview only. Review these commands; --run authorizes trusted synchronous project checks with external networking blocked.')
            print('Supported checks must finish synchronously. Cleanup covers the process group; a pass records the observed inputs, not descendant quiescence.')
            for issue in result['unresolved']:
                print(issue)
            for row in result['results']:
                print(f"{row['kind']}: {row['status']} ({row['duration_s']}s)")
                if row['output']:
                    print(row['output'])
            if result.get('reason'):
                print(result['reason'])
            if result.get('manifest_path'):
                print('Undo: tsmax undo ' + json.dumps(result['manifest_path']))
        return {'preview': 0, 'passed': 0, 'failed': 1, 'incomplete': 2}[result['status']]
    except (OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else 'Verification could not safely access local files.'
        print(json.dumps({'status': 'incomplete', 'error': message}) if args.json else message, file=sys.stdout if args.json else sys.stderr)
        return 2
