"""Per-run payload disk: trajectory actions, framework scripts, result area.

The delivery rootfs is built once by :mod:`bwx.convert` and never patched again.
Everything that varies per run lives on this second ext4 disk (guest `/dev/vdb`,
mounted at `/work`), which is cheap to rebuild and can be made per-instance so N
concurrent VMs of the same case never share mutable state.

Payload layout (guest paths):
    /work/supervise.sh        budget watchdog + summary writer (bash/awk only)
    /work/run.sh              action loop
    /work/default.sh          per-case variables (cwd, token-independent)
    /work/steps.txt           "<index> <delay_s>" per step, 1-based
    /work/actions/<i>.sh      raw action text of step i
    /work/tools/loopback      static ELF that brings lo up (images lack ip/ifconfig)
    /work/result/...          created by the guest at run time

The disk is built with `mkfs.ext4 -d <dir>` -- no loop mount, no root needed for
this step (root is needed elsewhere: podman storage, debugfs reads, KVM).
"""
import os
import platform
import shutil
import subprocess
from pathlib import Path

from .util import Error, MIB, posix_cksum, run, srun, write_json

GUEST_DIR = Path(__file__).resolve().parent / 'guest'
PROBE_DIR = GUEST_DIR.parent.parent.parent / 'probe'
TOOLCHAIN = ('bash', 'awk', 'date', 'cksum', 'wc', 'sed', 'tr', 'sync')


def steps_text(steps):
    """Render steps.txt: `<index> <delay_s>` per line, 1-based."""
    lines = []
    for index, step in enumerate(steps, 1):
        lines.append('%d %s' % (index, float(step.get('delay_time') or 0)))
    return '\n'.join(lines) + '\n'


def default_sh(workdir):
    """Per-case shell variables sourced by run.sh (POSIX-safe quoting)."""
    import shlex
    return ('#!/bin/sh\n'
            '# Per-case defaults; overridden by kernel cmdline parameters.\n'
            'BWX_CWD=%s\n' % shlex.quote(workdir or '/testbed'))


def build_loopback_tool(cache_dir):
    """Compile the static loopback helper once per host architecture.

    Cached under <root>/work/tools/loopback-<machine>; rebuilt when the source is
    newer than the binary. Static linking is required: the guest libc differs from
    the host's (Ubuntu 22.04 glibc 2.35 in the images vs the host distribution).
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    binary = cache_dir / ('loopback-%s' % platform.machine())
    source = PROBE_DIR / 'loopback.c'
    if not source.is_file():
        raise Error('loopback source missing: %s' % source)
    if binary.is_file() and binary.stat().st_mtime >= source.stat().st_mtime:
        return binary
    compiler = shutil.which('gcc') or shutil.which('cc')
    if not compiler:
        raise Error('no C compiler on the host; cannot build the loopback helper')
    result = subprocess.run([compiler, '-static', '-O2', '-o', str(binary), str(source)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        binary.unlink(missing_ok=True)
        raise Error('loopback helper build failed: %s' % result.stderr.strip()[:400])
    os.chmod(binary, 0o755)
    return binary


def write_payload_tree(directory, steps, *, workdir, loopback_binary=None,
                       extra_tools=()):
    """Materialize the payload directory tree (no disk image yet).

    `loopback_binary` is optional when the run also attaches the shared tools disk
    (which carries the same helper); keeping it off the payload avoids copying a
    600 KiB static binary into every concurrent VM's scratch area.
    """
    directory = Path(directory)
    if directory.exists():
        shutil.rmtree(directory)
    (directory / 'actions').mkdir(parents=True)
    (directory / 'tools').mkdir()
    (directory / 'result').mkdir()

    # The rootfs only contains a stub that execs /work/init.sh, so the whole guest
    # framework ships on the payload disk and can be changed without reconverting.
    for name in ('init.sh', 'supervise.sh', 'run.sh'):
        shutil.copy2(GUEST_DIR / name, directory / name)
        os.chmod(directory / name, 0o755)

    text = steps_text(steps)
    (directory / 'steps.txt').write_text(text, encoding='utf-8')
    (directory / 'default.sh').write_text(default_sh(workdir), encoding='utf-8')

    for index, step in enumerate(steps, 1):
        action = Path(directory / 'actions' / ('%d.sh' % index))
        action.write_text(step['action'] + '\n', encoding='utf-8')
        os.chmod(action, 0o644)

    if loopback_binary is not None:
        shutil.copy2(loopback_binary, directory / 'tools' / 'loopback')
        os.chmod(directory / 'tools' / 'loopback', 0o755)
    for tool in extra_tools:
        source = Path(tool)
        if not source.is_file():
            raise Error('extra tool not found: %s' % source)
        shutil.copy2(source, directory / 'tools' / source.name)
        os.chmod(directory / 'tools' / source.name, 0o755)

    crc, length = posix_cksum(text.encode('utf-8'))
    return {'steps': len(steps), 'steps_crc': crc, 'steps_bytes': length,
            'workdir': workdir or '/testbed',
            'framework_sha256': {name: _sha(directory / name) for name in
                                 ('init.sh', 'supervise.sh', 'run.sh', 'default.sh',
                                  'steps.txt')}}


def _sha(path):
    from .util import sha256_file
    return sha256_file(path)


def build_payload_image(directory, disk_path, *, result_mib=64):
    """mkfs.ext4 -d <directory> <disk>. Sparse file, sized from the tree."""
    directory = Path(directory)
    disk_path = Path(disk_path)
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    used = sum(p.stat().st_size for p in directory.rglob('*') if p.is_file())
    size = max(result_mib * MIB, used * 2 + 16 * MIB)
    if disk_path.exists():
        disk_path.unlink()
    with disk_path.open('wb') as stream:
        stream.truncate(size)
    srun(['mkfs.ext4', '-q', '-F', '-m', '0', '-O', '^metadata_csum_seed',
          '-d', str(directory), str(disk_path)])
    return {'path': str(disk_path), 'bytes': size, 'content_bytes': used}


def extract_results(disk_path, destination, *, timeout=120):
    """debugfs rdump /result -> destination. Returns the extracted dir or None.

    Read-only: the payload disk is never modified here, and debugfs failures are
    reported rather than raised so a crashed VM still yields whatever it wrote.
    """
    disk_path = Path(disk_path)
    destination = Path(destination)
    if not disk_path.is_file():
        return None
    destination.mkdir(parents=True, exist_ok=True)
    result = srun(['debugfs', '-R', 'rdump /result %s' % destination, str(disk_path)],
                  check=False, timeout=timeout, quiet=True)
    extracted = destination / 'result'
    if not extracted.is_dir():
        return None
    return extracted


def read_summary(extracted):
    """Parse the guest-written summary.json (authoritative result copy)."""
    if not extracted:
        return None
    path = Path(extracted) / 'summary.json'
    if not path.is_file():
        return None
    import json
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except ValueError:
        return {'status': 'summary_unparseable',
                'raw': path.read_text(encoding='utf-8', errors='replace')[:2000]}


def read_events(extracted):
    """Parse events lines from the guest (kind=...,step=...,exit_code=...)."""
    if not extracted:
        return []
    path = Path(extracted) / 'events.jsonl'
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line:
            continue
        fields = {}
        for part in line.split(','):
            key, _, value = part.partition('=')
            fields[key.strip()] = value.strip()
        if 'kind' in fields:
            events.append(fields)
    return events


def action_logs(extracted):
    """Per-action log paths, for post-mortem inspection."""
    if not extracted:
        return {}
    directory = Path(extracted) / 'actions'
    if not directory.is_dir():
        return {}
    return {p.stem: str(p) for p in sorted(directory.glob('*.log'),
                                           key=lambda q: int(q.stem) if q.stem.isdigit() else 0)}
