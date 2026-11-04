"""`bwx case-list` -- extract the replayable case list from a case directory.

Scans a directory of converted cases (default `<root>/cases`) and prints one
instance_id per line, in directory order -- exactly the .txt case-list format
`bwx replay --case-file` and `bwx linearity --case-file` accept:

    bwx case-list > my-cases.txt
    sudo ./bwx linearity --case-file my-cases.txt --levels 1,2,4,...

A subdirectory counts as replayable when its `base-image.json` says
`status=converted` and the image file it points to exists. With `--strict`
the trajectory under the dataset is loaded too (parse + shape), so a case
whose replay JSON went missing or rotted is reported now instead of failing
mid-sweep. Skipped entries and the summary go to stderr, keeping stdout a
clean id list (safe to redirect). `--out FILE` additionally writes the list
with a `#` header (source dir, count, timestamp).
"""
import argparse
import json
import sys
from pathlib import Path

from .. import config as config_mod
from .. import convert
from ..util import Error, read_json, utc_stamp

COMMAND = 'case-list'
HELP = 'list the replayable case ids found under a case directory'


def add_arguments(parser):
    parser.add_argument('--dir', type=Path, metavar='DIR',
                        help='case directory to scan (default: <root>/cases)')
    parser.add_argument('--strict', action='store_true',
                        help='also load the dataset trajectory (parse + '
                             'shape), not just the converted image')
    parser.add_argument('--out', type=Path, metavar='FILE',
                        help='write the list to FILE as txt (with a # header) '
                             'in addition to stdout')
    parser.add_argument('--json', action='store_true',
                        help='machine-readable JSON on stdout (includes '
                             'skipped entries with reasons)')


def run(args):
    config = config_mod.load(args.config, {})
    case_dir = args.dir if args.dir is not None else config.cases_dir
    if not case_dir.is_dir():
        raise Error('case directory not found: %s' % case_dir)

    usable, skipped = _scan(case_dir)
    if args.strict:
        usable, strict_skipped = _check_trajectories(config, usable)
        skipped.extend(strict_skipped)

    if args.json:
        print(json.dumps({'case_dir': str(case_dir),
                          'count': len(usable),
                          'cases': usable,
                          'skipped': skipped}, indent=2, ensure_ascii=False))
    else:
        for row in usable:
            print(row['instance_id'])

    print('%d replayable case(s) under %s' % (len(usable), case_dir),
          file=sys.stderr)
    for entry in skipped:
        print('  skipped %-45s %s' % (entry['instance_id'], entry['reason']),
              file=sys.stderr)
    if args.out is not None:
        _write_list(args.out, case_dir, usable)
    return 0


def _scan(case_dir):
    """Split the subdirectories into replayable rows and skipped reasons.

    Replayable = base-image.json with status=converted whose image file
    still exists. The trajectory's step count / delay sum ride along for
    `--json` (sweep time estimation); they are not validated here.
    """
    usable, skipped = [], []
    for workspace in sorted(case_dir.glob('*')):
        if not workspace.is_dir():
            continue
        record_path = workspace / 'base-image.json'
        if not record_path.is_file():
            skipped.append({'instance_id': workspace.name,
                            'reason': 'no base-image.json'})
            continue
        record = read_json(record_path, {})
        iid = record.get('instance_id') or workspace.name
        if record.get('status') != 'converted':
            skipped.append({'instance_id': iid,
                            'reason': 'status=%r' % (record.get('status'),)})
            continue
        image = Path(record.get('path', ''))
        if not image.is_file():
            skipped.append({'instance_id': iid,
                            'reason': 'image missing: %s' % image})
            continue
        trajectory = record.get('trajectory') or {}
        usable.append({'instance_id': iid,
                       'language': record.get('language', ''),
                       'steps': trajectory.get('steps'),
                       'delay_sum_s': trajectory.get('delay_sum_s'),
                       'image_bytes': record.get('bytes'),
                       'dir': str(workspace)})
    return usable, skipped


def _check_trajectories(config, usable):
    """--strict pass: every case's trajectory must exist and parse."""
    ok, skipped = [], []
    for row in usable:
        try:
            replay = convert.find_replay(config.dataset, row['instance_id'])
            if replay is None:
                raise Error('no trajectory under %s' % config.dataset)
            convert.load_trajectory(replay)
        except (Error, OSError, ValueError) as exc:
            skipped.append({'instance_id': row['instance_id'],
                            'reason': 'trajectory: %s' % exc})
            continue
        ok.append(row)
    return ok, skipped


def _write_list(path, case_dir, usable):
    """txt case list: `#` header (ignored by parse_case_file) + one id/line."""
    header = ('# bwx case-list --dir %s\n'
              '# %d replayable case(s), generated %s\n'
              % (case_dir, len(usable), utc_stamp()))
    path.write_text(header + ''.join('%s\n' % row['instance_id']
                                     for row in usable),
                    encoding='utf-8')
    print('wrote %s (%d case(s))' % (path, len(usable)), file=sys.stderr)
