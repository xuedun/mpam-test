"""podman image -> bootable ext4 rootfs (the "base" image, built once per case).

Pipeline (no loop mount, no KVM, no device-mapper):
    podman create --entrypoint /bin/true <iid>
    podman export <cid> > rootfs.tar
    tar --numeric-owner --xattrs --acls -xpf rootfs.tar -C rootfs/
    inject /bwx/{init.sh,image-env.sh}
    truncate <size> ; mkfs.ext4 -q -F -m 0 -N <inodes*2> -O ^metadata_csum_seed -d rootfs <dest>
    e2fsck -fn <dest> ; sha256 ; atomic rename

Lessons carried over from the upstream implementations (all still true):
* `mkfs.ext4 -d` populates the filesystem directly, so no root loop mount is
  needed -- but root is still required for podman's storage and later debugfs.
* Image size is estimated from `du -sb` (apparent size), not block usage, because
  sparse files and host-filesystem holes make block counts misleading.
* The image must not already contain /bwx; that is our reserved namespace.
* Absolute symlinks in the export resolve against the *host*, so the /bin/sh check
  is weak; the authoritative check is the boot smoke test.
* The delivery image is written once and then treated as immutable: replay copies
  it (reflink when the filesystem supports it) and never patches it again.
"""
import csv
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from .util import (Error, GIB, MIB, free_bytes, human_size, read_json, run, srun,
                   sha256_file, write_json)

GUEST_DIR = Path(__file__).resolve().parent / 'guest'
RESERVED_DIR = '/bwx'


def canonical_tag(tag):
    """`docker://host/ns/name:tag` -> `name:tag` (registry-independent identity)."""
    return tag.removeprefix('docker://').split('/')[-1]


def read_manifest(dataset):
    """manifest.csv -> {instance_id: row}; requires instance_id and image_arm64."""
    path = Path(dataset) / 'manifest.csv'
    if not path.is_file():
        raise Error('manifest.csv not found in dataset: %s' % path)
    rows = {}
    with path.open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            iid = (row.get('instance_id') or '').strip()
            if iid:
                rows[iid] = row
    if not rows:
        raise Error('manifest.csv has no usable rows: %s' % path)
    return rows


def find_replay(dataset, instance_id):
    """Locate <instance_id>.replay.json under replay-*/ (layout varies)."""
    dataset = Path(dataset)
    direct = dataset / ('%s.replay.json' % instance_id)
    if direct.is_file():
        return direct
    for candidate in sorted(dataset.glob('*/*.replay.json')):
        if candidate.name == '%s.replay.json' % instance_id:
            return candidate
    return None


def load_trajectory(path):
    """Validate a trajectory file and return (steps, raw_bytes, sha256).

    Each step must have a non-empty string `action`. `delay_time` must be a finite
    non-negative number when present (it is the model-call delay before the step);
    malformed values are rejected here rather than silently ignored.
    """
    path = Path(path)
    raw = path.read_bytes()
    try:
        doc = json.loads(raw.decode('utf-8-sig'))
    except ValueError as exc:
        raise Error('invalid trajectory JSON %s: %s' % (path, exc))
    steps = doc.get('trajectory')
    if not isinstance(steps, list) or not steps:
        raise Error('trajectory has no steps: %s' % path)
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise Error('step %d is not an object: %s' % (index, path))
        action = step.get('action')
        if not isinstance(action, str) or not action.strip():
            raise Error('step %d has no usable action: %s' % (index, path))
        delay = step.get('delay_time', 0)
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) \
                or delay != delay or delay < 0:
            raise Error('step %d has invalid delay_time %r: %s' % (index, delay, path))
    sha = sha256_file(path)
    return steps, raw, sha


def list_local_images():
    """root podman storage -> {image_id: [tags]}. Requires root (`sudo podman`)."""
    ids = srun(['podman', 'images', '-q', '--no-trunc'], quiet=True).stdout.split()
    if not ids:
        return {}
    details = json.loads(srun(['podman', 'image', 'inspect', *sorted(set(ids))],
                              quiet=True).stdout or '[]')
    return {image['Id'].removeprefix('sha256:'): (image.get('RepoTags') or [])
            for image in details if image.get('Id')}


def resolve_image(instance_id, row, local):
    """manifest row + local storage -> (image_id, tag) or None."""
    wanted = canonical_tag(row.get('image_arm64') or '')
    for image_id, tags in local.items():
        if any(canonical_tag(t) == wanted for t in tags):
            return image_id, wanted
    return None


def inspect_image(image_id):
    """Config fields needed to size the disk and reproduce the environment."""
    doc = json.loads(srun(['podman', 'image', 'inspect', image_id], quiet=True).stdout)[0]
    config = doc.get('Config') or {}
    return {'id': doc.get('Id', ''), 'architecture': doc.get('Architecture', ''),
            'os': doc.get('Os', ''), 'working_dir': config.get('WorkingDir') or '/testbed',
            'env': config.get('Env') or [], 'user': config.get('User') or '',
            'entrypoint': config.get('Entrypoint') or [], 'cmd': config.get('Cmd') or [],
            'size_bytes': int(doc.get('Size') or 0)}


def env_script(env, working_dir):
    """Render /bwx/image-env.sh: exports + the recorded workdir default."""
    lines = ['#!/bin/sh', '# Captured from the image Config at convert time.']
    for entry in env:
        key, sep, value = entry.partition('=')
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            continue
        lines.append('export %s=%s' % (key, shlex.quote(value)))
    lines.append('export BWX_IMAGE_CWD=%s' % shlex.quote(working_dir or '/testbed'))
    return '\n'.join(lines) + '\n'


def export_rootfs(image_id, workdir, log):
    """podman create + export + tar -> <workdir>/rootfs. Returns (container, rootfs).

    `podman export` is streamed straight to disk so a multi-GB image never has to
    fit in memory.
    """
    from .util import sudo_prefix
    container = 'bwx-export-%s' % image_id[:12]
    srun(['podman', 'rm', '-f', container], check=False, quiet=True)
    srun(['podman', 'create', '--name', container, '--entrypoint', '/bin/true', image_id])
    log.append('container=%s' % container)
    try:
        tar_path = workdir / 'rootfs.tar'
        with tar_path.open('wb') as stream:
            subprocess.run([*sudo_prefix(), 'podman', 'export', container],
                           stdout=stream, check=True)
        rootfs = workdir / 'rootfs'
        rootfs.mkdir()
        srun(['tar', '--numeric-owner', '--xattrs', '--acls', '-xpf', str(tar_path),
              '-C', str(rootfs)])
        tar_path.unlink(missing_ok=True)
        return container, rootfs
    finally:
        srun(['podman', 'rm', '-f', container], check=False, quiet=True)


def inject_support(rootfs, image_config):
    """Write the /bwx support tree into the exported rootfs.

    Only the *stub* init and the captured image environment are baked in. The real
    guest framework lives on the payload disk (rebuilt in milliseconds per run), so
    a converted multi-GB image never has to be rebuilt to iterate on guest code.
    """
    target = rootfs / 'bwx'
    if target.exists():
        raise Error('image already contains %s; refusing to overwrite' % RESERVED_DIR)
    target.mkdir(parents=True)
    shutil.copy2(GUEST_DIR / 'init-stub.sh', target / 'init.sh')
    os.chmod(target / 'init.sh', 0o755)
    (target / 'image-env.sh').write_text(
        env_script(image_config['env'], image_config['working_dir']), encoding='utf-8')
    os.chmod(target / 'image-env.sh', 0o644)
    # PID1 needs these to exist before anything is mounted into them.
    for name in ('dev', 'proc', 'sys', 'run', 'tmp', 'work'):
        (rootfs / name).mkdir(exist_ok=True)
    sh = rootfs / 'bin' / 'sh'
    if not sh.exists() and not sh.is_symlink():
        raise Error('image has no /bin/sh; cannot run the bwx contract')


def estimate_size(rootfs, min_gib=4):
    """apparent size * 1.3 + 2GiB, floored at min_gib, rounded up to MiB."""
    apparent = int(run(['du', '-sb', str(rootfs)], quiet=True).stdout.split()[0])
    inodes = int(run(['bash', '-c',
                      'find %s -xdev -printf . 2>/dev/null | wc -c' % shlex.quote(str(rootfs))],
                     quiet=True).stdout.strip() or 0)
    size = max(apparent * 13 // 10 + 2 * GIB, min_gib * GIB)
    size = ((size + MIB - 1) // MIB) * MIB
    return size, apparent, max(inodes * 2, 65536)


def build_base_image(config, instance_id, *, image_id=None, tag=None, log_path=None,
                     min_gib=4, space_factor=1.3):
    """Convert one case's image into <cases>/<iid>/image.ext4. Idempotent."""
    workspace = config.case_workspace(instance_id)
    destination = config.case_image(instance_id)
    workspace.mkdir(parents=True, exist_ok=True)

    if destination.is_file():
        record = read_json(workspace / 'base-image.json', {})
        if record.get('status') == 'converted' and record.get('sha256') == sha256_file(destination):
            return record
        raise Error('%s exists but does not match its record; delete it to rebuild'
                    % destination)

    manifest = read_manifest(config.dataset)
    row = manifest.get(instance_id)
    if row is None and image_id is None:
        raise Error('instance_id not in manifest.csv: %s' % instance_id)

    if image_id is None:
        local = list_local_images()
        resolved = resolve_image(instance_id, row, local)
        if resolved is None:
            raise Error('image for %s is not in root podman storage (%s); '
                        'pull it first with sudo podman pull'
                        % (instance_id, canonical_tag(row.get('image_arm64') or '')))
        image_id, tag = resolved
    image_config = inspect_image(image_id)
    if image_config['architecture'] and 'arm' not in image_config['architecture'].lower() \
            and os.uname().machine != image_config['architecture']:
        raise Error('image architecture %s does not match host %s'
                    % (image_config['architecture'], os.uname().machine))

    replay_path = find_replay(config.dataset, instance_id)
    trajectory_meta = None
    if replay_path is not None:
        steps, replay_raw, replay_sha = load_trajectory(replay_path)
        trajectory_meta = {'path': str(replay_path), 'sha256': replay_sha,
                           'steps': len(steps),
                           'delay_sum_s': round(sum(float(s.get('delay_time') or 0)
                                                    for s in steps), 3)}

    free = free_bytes(config.root)
    needed = int(image_config['size_bytes'] * space_factor) + 8 * GIB
    if free < needed:
        raise Error('insufficient free space under %s: have %s, need %s'
                    % (config.root, human_size(free), human_size(needed)))

    log = []
    with tempfile.TemporaryDirectory(prefix='bwx-convert-', dir=str(config.work_dir)) as tmp:
        workdir = Path(tmp)
        container, rootfs = export_rootfs(image_id, workdir, log)
        inject_support(rootfs, image_config)
        size, apparent, inodes = estimate_size(rootfs, min_gib=min_gib)
        log.append('size=%s apparent=%s inodes=%d' % (human_size(size),
                                                      human_size(apparent), inodes))
        partial = workspace / 'image.ext4.partial'
        with partial.open('wb') as stream:
            stream.truncate(size)
        srun(['mkfs.ext4', '-q', '-F', '-m', '0', '-N', str(inodes),
              '-O', '^metadata_csum_seed', '-d', str(rootfs), str(partial)])
        srun(['e2fsck', '-fn', str(partial)], check=False)
        os.replace(partial, destination)

    record = {
        'instance_id': instance_id, 'image_id': image_id, 'tag': tag,
        'architecture': image_config['architecture'], 'status': 'converted',
        'path': str(destination), 'bytes': destination.stat().st_size,
        'allocated_bytes': destination.stat().st_blocks * 512,
        'sha256': sha256_file(destination),
        'working_dir': image_config['working_dir'], 'original_user': image_config['user'],
        'entrypoint': image_config['entrypoint'], 'cmd': image_config['cmd'],
        'trajectory': trajectory_meta,
        'language': (row or {}).get('language', ''), 'workdir': (row or {}).get('workdir', ''),
        'convert_log': log,
    }
    write_json(workspace / 'base-image.json', record)
    return record
