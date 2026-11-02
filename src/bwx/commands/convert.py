"""`bwx convert` -- podman image -> bootable base ext4 (built once per case)."""
import json
from pathlib import Path

from .. import cases as cases_mod
from .. import config as config_mod
from .. import convert
from ..util import Error, human_size, require_root, write_json

COMMAND = 'convert'
HELP = 'convert podman images into base ext4 rootfs images (idempotent)'


def add_arguments(parser):
    parser.add_argument('--case', action='append', default=[], metavar='ID',
                        help='instance_id to convert (repeatable)')
    parser.add_argument('--case-file', type=Path, help='one instance_id per line (# = comment)')
    parser.add_argument('--all', action='store_true',
                        help='every manifest row whose image is in root podman storage')
    parser.add_argument('--image', action='append', default=[], metavar='ID',
                        help='convert a podman image id directly (bypasses manifest lookup)')
    parser.add_argument('--min-gib', type=int, default=4,
                        help='minimum ext4 size in GiB (default 4)')


def _case_ids_from_file(path):
    ids = []
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        token = line.split('#', 1)[0].strip()
        if token:
            ids.append(token)
    return ids


def run(args):
    config = config_mod.from_args(args)
    config_mod.validate_runtime(config, need_images=True)
    require_root('convert (podman root storage, mkfs.ext4, debugfs)')

    ids = list(args.case)
    if args.case_file:
        ids.extend(_case_ids_from_file(args.case_file))
    if args.all:
        ids.extend(_discover_convertible(config))
    # Deduplicate preserving order.
    seen, ordered = set(), []
    for iid in ids:
        if iid not in seen:
            seen.add(iid)
            ordered.append(iid)

    direct_images = list(args.image)
    if not ordered and not direct_images:
        raise Error('nothing to convert: pass --case/--case-file/--all/--image')

    results = []
    failures = []
    for iid in ordered:
        print('=== convert %s' % iid)
        try:
            record = convert.build_base_image(config, iid, min_gib=args.min_gib)
            results.append(record)
            print('    -> %s (%s, sha %s)' % (record['path'], human_size(record['bytes']),
                                              record['sha256'][:12]))
        except Error as exc:
            failures.append({'instance_id': iid, 'error': str(exc)})
            print('    FAILED: %s' % exc)
    for image_id in direct_images:
        label = 'image-%s' % image_id[:12]
        print('=== convert %s' % label)
        try:
            record = convert.build_base_image(config, label, image_id=image_id,
                                              min_gib=args.min_gib)
            results.append(record)
            print('    -> %s' % record['path'])
        except Error as exc:
            failures.append({'image': image_id, 'error': str(exc)})
            print('    FAILED: %s' % exc)

    write_json(config.logs_dir / 'convert-last.json',
               {'converted': len(results), 'failed': failures,
                'records': results})
    print('\nconverted %d, failed %d' % (len(results), len(failures)))
    return 1 if failures else 0


def _discover_convertible(config):
    """manifest rows whose image is actually present in root podman storage."""
    manifest = convert.read_manifest(config.dataset)
    local = convert.list_local_images()
    out = []
    for iid, row in manifest.items():
        if convert.resolve_image(iid, row, local) is not None:
            out.append(iid)
    return sorted(out)
