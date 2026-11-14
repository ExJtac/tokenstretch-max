"""Optional installed local runtime, default preview and explicit inference consent."""
from __future__ import annotations

import json
import sys

from . import local_offload
from .adapters.ollama_local import DEFAULT_ENDPOINT, LocalError


def add_parser(subparsers):
    parser = subparsers.add_parser('local', help='optional local text proposals; no model installation or file changes')
    commands = parser.add_subparsers(dest='local_operation', required=True)
    for operation in ('status', *local_offload.OPERATIONS):
        command = commands.add_parser(operation)
        command.add_argument('--endpoint', default=DEFAULT_ENDPOINT, help='numeric loopback HTTP endpoint only')
        command.add_argument('--timeout', type=float, default=60, help='total deadline per HTTP request, at most 300 seconds')
        command.add_argument('--json', action='store_true')
        if operation != 'status':
            command.add_argument('--model', required=True, help='exact locally listed model name')
            inputs = command.add_mutually_exclusive_group(required=True)
            inputs.add_argument('--text', help='bounded supplied text; use --stdin to avoid shell history')
            inputs.add_argument('--stdin', action='store_true', help='read up to 16000 UTF-8 bytes from standard input')
            command.add_argument('--run', action='store_true', help='authorize one local inference request; default checks metadata only')


def run(args):
    try:
        if args.local_operation == 'status':
            result = local_offload.status(endpoint=args.endpoint, timeout=args.timeout)
        else:
            text = sys.stdin.read(local_offload.MAX_INPUT + 1) if args.stdin else args.text
            result = local_offload.transform(args.local_operation, text, args.model, run=args.run,
                                             endpoint=args.endpoint, timeout=args.timeout)
        if args.json:
            print(json.dumps(result, ensure_ascii=True))
        else:
            print('Local: ' + result['status'])
            if result.get('reason'):
                print(result['reason'])
            if 'models' in result:
                print('Listed models (not yet validated): ' + json.dumps(result['models']))
            if result.get('evidence'):
                print('Selected model: ' + result['model'] + '; input bytes: ' + str(result['input_bytes']))
            if result.get('output'):
                print(result['output'])
            for limitation in result['limitations']:
                print(limitation)
        return 2 if result['status'] == 'unavailable' else 0
    except (LocalError, UnicodeError) as exc:
        result = {'status': 'refused', 'reason': str(exc) if isinstance(exc, LocalError) else 'Input is not valid UTF-8 text.'}
        print(json.dumps(result) if args.json else result['reason'], file=sys.stdout if args.json else sys.stderr)
        return 2
