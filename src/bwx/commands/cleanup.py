"""`bwx cleanup` -- remove VM scratch dirs left by an interrupted run.

Safety rules:
* scratch directories are only removed under <root>/logs;
* a directory that is currently a mount point is never removed;
* `--dry-run` (the default output) shows exactly what would happen.
"""
import os
from pathlib import Path

from .. import config as config_mod
from .. import util
from ..util import Error, sudo_prefix, write_json

COMMAND = 'cleanup'
HELP = 'remove VM scratch dirs left by an interrupted run'

PREFIX = 'bwx-'


def add_arguments(parser):
    parser.add_argument('--scratch', action='store_true', default=True,
                        help='remove VM scratch dirs (default action)')
    parser.add_argument('--older-than-hours', type=float, default=6.0,
                        help='only remove scratch dirs idle for this many hours '
                             '(default 6; guards against deleting a live run)')
    parser.add_argument('--dry-run', action='store_true', default=True,
                        help='print what would be removed (default)')
    parser.add_argument('--force', dest='dry_run', action='store_false',
                        help='actually remove')


def run(args):
    config = config_mod.from_args(args)
    if not args.dry_run:
        util.require_root('cleanup --force')

    report = {'scratch': [], 'errors': []}
    _cleanup_scratch(config, args, report)

    write_json(config.logs_dir / 'cleanup-last.json', report)
    mode = 'would remove' if args.dry_run else 'removed'
    print('\n%s: %d scratch dir(s)' % (mode, len(report['scratch'])))
    for entry in report['errors']:
        print('  skipped: %s' % entry)
    return 0


def _cleanup_scratch(config, args, report):
    import time
    cutoff = time.time() - args.older_than_hours * 3600
    roots = [config.logs_dir]
    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or not entry.name.startswith('vm-'):
                continue
            try:
                if entry.stat().st_mtime > cutoff:
                    report['errors'].append('%s: modified within %.1fh'
                                            % (entry, args.older_than_hours))
                    continue
            except OSError:
                continue
            if _is_mountpoint(entry):
                report['errors'].append('%s: is a mount point; refusing' % entry)
                continue
            report['scratch'].append(str(entry))
            if not args.dry_run:
                import shutil
                shutil.rmtree(entry, ignore_errors=True)


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ''


def _is_mountpoint(path):
    try:
        text = Path('/proc/self/mountinfo').read_text()
    except OSError:
        return False
    target = str(Path(path).resolve())
    return any(' %s ' % target in line for line in text.splitlines())
