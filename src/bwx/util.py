"""Shared helpers: subprocess, hashing, POSIX cksum, atomic JSON/CSV writes.

Standard library only. Every helper is used by more than one module.
"""
import csv
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

MIB = 1024 ** 2
GIB = 1024 ** 3


class Error(Exception):
    """Fatal, user-facing error. Message is printed without a traceback."""


def run(argv, *, check=True, capture=True, timeout=None, cwd=None, input_text=None,
        quiet=False):
    """Run a command. Returns CompletedProcess with text stdout/stderr."""
    cmd = [str(a) for a in argv]
    if not quiet:
        log_cmd(cmd)
    return subprocess.run(cmd, check=check, text=True, cwd=cwd, timeout=timeout,
                          input=input_text,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def log_cmd(cmd):
    """Echo a command to stderr when BWX_VERBOSE is set (debugging aid)."""
    if os.environ.get('BWX_VERBOSE'):
        print('+ ' + ' '.join(shlex.quote(str(c)) for c in cmd), file=sys.stderr)


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(chunk), b''):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------- cksum
# POSIX `cksum`: CRC-32 with polynomial 0x04C11DB7, no input/output
# reflection, init 0, message length folded in big-endian, final complement.
# The guest only has coreutils, so `cksum` is the one integrity primitive both
# sides can compute identically without a scripting runtime.
_CKSUM_POLY = 0x04C11DB7
_CKSUM_TABLE = []
for _byte in range(256):
    _crc = _byte << 24
    for _ in range(8):
        _crc = ((_crc << 1) ^ _CKSUM_POLY) & 0xFFFFFFFF if _crc & 0x80000000 \
            else (_crc << 1) & 0xFFFFFFFF
    _CKSUM_TABLE.append(_crc)


def posix_cksum(data):
    """Return (crc, length) exactly as coreutils `cksum` prints them."""
    crc = 0
    for byte in data:
        crc = ((crc << 8) ^ _CKSUM_TABLE[((crc >> 24) ^ byte) & 0xFF]) & 0xFFFFFFFF
    length = len(data)
    size = length
    while size:
        crc = ((crc << 8) ^ _CKSUM_TABLE[((crc >> 24) ^ (size & 0xFF)) & 0xFF]) & 0xFFFFFFFF
        size >>= 8
    return (~crc) & 0xFFFFFFFF, length


def posix_cksum_file(path):
    with open(path, 'rb') as stream:
        return posix_cksum(stream.read())


# ---------------------------------------------------------------------- json
def write_json(path, data):
    """Atomic JSON write: temp file in the same directory, then os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        if default is None:
            raise Error('cannot read JSON: %s' % path)
        return default


def append_jsonl(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + '\n')


def write_csv(path, rows, fieldnames):
    """Atomic CSV write with a stable column order (utf-8-sig for Excel)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    os.replace(tmp, path)


def utc_stamp(when=None):
    return time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(when))


def human_size(num):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(num) < 1024:
            return '%.1f%s' % (num, unit)
        num /= 1024
    return '%.1fPiB' % num


def copy_sparse(src, dst):
    """Reflink when the filesystem supports it (xfs/btrfs), else sparse copy."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    for mode in ('--reflink=auto', '--reflink=never'):
        result = subprocess.run(['cp', mode, '--sparse=always', str(src), str(dst)],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return mode
    raise Error('copy failed %s -> %s: %s' % (src, dst, result.stderr.strip()))


def free_bytes(path):
    return shutil.disk_usage(str(path)).free


def require_root(action):
    if os.geteuid() != 0:
        raise Error('%s requires root: re-run with sudo (or cache credentials '
                    'with `sudo -v`)' % action)


def sudo_prefix():
    """Empty when already root; otherwise `sudo -n` after a credential check."""
    if os.geteuid() == 0:
        return []
    if os.environ.get('SUDO_ASKPASS') and subprocess.run(
            ['sudo', '-A', '-v'], capture_output=True).returncode == 0:
        return ['sudo', '-A']
    if subprocess.run(['sudo', '-n', '-v'], capture_output=True).returncode == 0:
        return ['sudo', '-n']
    raise Error('root required: re-run with sudo, or cache credentials with `sudo -v`')


def srun(argv, **kwargs):
    """Run a command with sudo when needed (no-op when already root)."""
    return run([*sudo_prefix(), *argv], **kwargs)


def which_or_die(name, hint='/usr/sbin'):
    path = shutil.which(name) or shutil.which(name, path=os.environ.get('PATH', '') + ':' + hint)
    if not path:
        raise Error('missing required tool: %s' % name)
    return path
