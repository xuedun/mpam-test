"""`bwx list` -- show converted cases and dataset availability."""
import json
from pathlib import Path

from .. import cases as cases_mod
from .. import config as config_mod
from .. import convert
from ..util import human_size

COMMAND = 'list'
HELP = 'list converted cases (and which dataset cases could be converted)'


def add_arguments(parser):
    parser.add_argument('--json', action='store_true', help='machine-readable output')
    parser.add_argument('--available', action='store_true',
                        help='also list dataset cases whose image is in podman storage '
                             'but not yet converted')


def run(args):
    config = config_mod.from_args(args)
    converted = cases_mod.catalog(config)
    available = []
    if args.available:
        done = {row['instance_id'] for row in converted}
        try:
            manifest = convert.read_manifest(config.dataset)
            local = convert.list_local_images()
        except Exception as exc:  # noqa: BLE001 - listing must not hard-fail
            manifest, local = {}, {}
            print('warning: cannot enumerate dataset/podman: %s' % exc)
        for iid, row in manifest.items():
            if iid in done:
                continue
            if convert.resolve_image(iid, row, local) is not None:
                replay = convert.find_replay(config.dataset, iid)
                available.append({'instance_id': iid, 'language': row.get('language', ''),
                                  'trajectory': str(replay) if replay else None})

    if args.json:
        print(json.dumps({'converted': converted, 'convertible': available},
                         indent=2, ensure_ascii=False))
        return 0

    if converted:
        print('%-45s %-8s %8s %6s %12s' % ('instance_id', 'lang', 'image', 'steps', 'sha256'))
        for row in converted:
            print('%-45s %-8s %8s %6s %12s'
                  % (row['instance_id'], row['language'] or '-',
                     human_size(row['image_bytes'] or 0), row['steps'] or '-',
                     row['sha256']))
    else:
        print('no converted case yet (run: bwx convert --all)')
    if available:
        print('\nconvertible now (image present in root podman storage):')
        for row in available:
            print('  %-45s %s' % (row['instance_id'], row['language'] or '-'))
    return 0
