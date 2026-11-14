"""Project-local solved-pattern suggestions and exact reviewed saves."""
from pathlib import Path
import json
import os
import sys

from . import patterns


def add_parser(subparsers):
    skills = subparsers.add_parser('skills', help='suggest historical local procedures or preview saving a verified task')
    commands = skills.add_subparsers(dest='skills_command', required=True)
    suggest = commands.add_parser('suggest', help='read-only lexical top-three summaries; no model calls')
    suggest.add_argument('query')
    save = commands.add_parser('save', help='preview a project-local procedure from an applied, historically verified task')
    save.add_argument('task')
    save.add_argument('--title', help='concise reviewed title override')
    save.add_argument('--approach', help='concise reviewed approach; required if the verified attempt has no note')
    save.add_argument('--apply', action='store_true', help='save only the exact preview bound by --review')
    save.add_argument('--review', help='exact review token returned by the preview')
    save.add_argument('--home', type=Path, help='home containing private source manifests and undo backups')
    for parser in (suggest, save):
        parser.add_argument('--root', type=Path)
        parser.add_argument('--json', action='store_true')


def run(args):
    root = Path(os.path.abspath(args.root or Path.cwd()))
    try:
        if args.skills_command == 'suggest':
            result = patterns.suggest(root, args.query)
            if args.json:
                print(json.dumps(result, ensure_ascii=True))
            elif result:
                for row in result:
                    print(row['slug'] + ': ' + row['summary'])
                print(patterns.HISTORY)
            else:
                print('No matching local patterns.')
        else:
            home = Path(os.path.abspath(args.home or Path.home()))
            result = patterns.save(root, home, args.task, title=args.title, approach=args.approach,
                                   apply=args.apply, review=args.review)
            if args.json:
                print(json.dumps(result, ensure_ascii=True))
            else:
                print('Pattern: ' + result['status'])
                print('Destination: ' + result['destination'])
                print(result['skill_markdown'])
                print('Evidence: ' + json.dumps(result['candidate']['evidence'], ensure_ascii=True))
                if result['status'] == 'preview':
                    print('Save this exact reviewed pattern: ' + result['apply_command'])
                if result.get('manifest_path'):
                    print('Undo: tsmax undo ' + json.dumps(result['manifest_path']))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else 'Pattern evidence or local files could not be accessed safely.'
        print(json.dumps({'status': 'refused', 'error': message}) if args.json else message,
              file=sys.stdout if args.json else sys.stderr)
        return 2
