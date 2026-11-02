"""The shared read-only tools disk (guest /dev/vdc, mounted at /opt/bwx-tools).

Why a separate disk: 3 of the 4 sample images contain no python3 at all, and some
trajectory steps write and run a python script (`cat > /tmp/x.py <<EOF ... EOF;
python3 /tmp/x.py`). Without an interpreter those steps exit 127, which the action
loop treats as "unsupported action". Rather than patching each delivery image, we
hand every VM the same read-only tools disk:

* one copy on the host, shared by all concurrent VMs (Firecracker opens it
  read-only, so sharing is safe and costs no extra disk);
* never shadows an image-provided interpreter -- init.sh only prepends it to PATH
  when `command -v python3` fails, so images that already have python keep theirs.

The python runtime is relocatable: the conda tree plus its ELF dependency closure,
launched through a private loader with --library-path so it never touches guest
glibc (mixing host and guest libc is what breaks these images).
"""
import os
import platform
import shutil
import subprocess
from pathlib import Path

from .util import Error, MIB, run, srun, write_json

TOOLS_SUBDIR = 'bwx-tools'
# The staged runtime must import everything a trajectory step might plausibly use,
# otherwise the failure only shows up inside a VM as a 127/nonzero action.
_SELF_TEST_SOURCE = (
    'import json,re,os,sys,ssl,sqlite3,hashlib,subprocess,socket,struct,shutil,'
    'tempfile,argparse,csv,base64,zlib,lzma,bz2,ctypes,platform,uuid,glob,'
    'pathlib,unittest,xml.etree.ElementTree;'
    'print("BWX_PY_OK", sys.version.split()[0])')
# Where the tools disk is mounted inside the guest (see guest/init.sh).
GUEST_MOUNT = '/opt/bwx-tools'


def find_python_runtime(config):
    """Locate a relocatable python tree, if the dataset ships one."""
    candidates = [Path(config.dataset) / 'python-deps' / 'python-runtime' / 'miniconda3',
                  Path(config.dataset) / 'python-runtime' / 'miniconda3']
    for path in candidates:
        if (path / 'bin' / 'python3').is_file() or (path / 'bin' / 'python3.14').is_file():
            return path
    return None


def build_tools_dir(directory, loopback_binary, python_runtime=None):
    """Materialize the tools tree (loopback + optional python runtime)."""
    directory = Path(directory)
    if directory.exists():
        shutil.rmtree(directory)
    (directory / 'bin').mkdir(parents=True)
    shutil.copy2(loopback_binary, directory / 'bin' / 'loopback')
    os.chmod(directory / 'bin' / 'loopback', 0o755)

    meta = {'loopback': True, 'python': False, 'machine': platform.machine()}
    if python_runtime is not None:
        python_runtime = Path(python_runtime)
        meta.update(_stage_python(directory, python_runtime))
    write_json(directory / 'tools-meta.json', meta)
    return meta


def _stage_python(directory, runtime):
    """Copy the interpreter + stdlib + ELF closure, and write a launcher.

    Trimmed of test/idle/tkinter/venv/site-packages and __pycache__ to keep the
    disk small (measured: 199 MiB -> ~50 MiB stdlib here). Only stdlib is needed:
    the trajectories use json/re/os/sys/sqlite3, never third-party packages.
    """
    binary = next((runtime / 'bin' / name for name in
                   ('python3', 'python3.14', 'python3.13', 'python3.12', 'python3.11')
                   if (runtime / 'bin' / name).is_file()), None)
    if binary is None:
        return {'python': False, 'reason': 'no python3 binary in %s' % runtime}
    version = _python_version(binary)
    stdlib_src = runtime / 'lib' / ('python%s' % version)
    if not stdlib_src.is_dir():
        return {'python': False, 'reason': 'stdlib dir missing: %s' % stdlib_src}

    stage = directory / 'python'
    (stage / 'bin').mkdir(parents=True)
    (stage / 'lib').mkdir()
    (stage / 'ld').mkdir()
    shutil.copy2(binary, stage / 'bin' / 'python3')
    os.chmod(stage / 'bin' / 'python3', 0o755)

    _copy_tree_trimmed(stdlib_src, stage / 'lib' / ('python%s' % version))
    # Shared libpython if the binary links against one.
    for lib in (runtime / 'lib').glob('libpython*.so*'):
        if lib.is_file():
            shutil.copy2(lib, stage / 'ld' / lib.name)
    # ELF dependency closure + the loader. The closure must cover the extension
    # modules too: `_ssl` needs libssl/libcrypto from the conda tree, and an
    # `import ssl` failure looks like a broken runtime rather than a missing .so.
    # Measured cost of the full closure (binary + every lib-dynload .so): ~15 MiB.
    # Run ldd against the SOURCE runtime, not the staged copies: a staged
    # extension module's DT_RPATH (`../../lib`) no longer resolves once it has
    # been moved, so its real dependencies (e.g. libbz2 for _bz2) would be missed.
    binaries = [binary,
                *sorted((runtime / 'lib' / ('python%s' % version) / 'lib-dynload').glob('*.so'))]
    # A library may exist both in the host's /usr/lib64 and in the runtime tree
    # (conda ships its own libcrypto/libssl). The host copy wins on a plain ldd
    # from the host, but the interpreter's extension modules were built against the
    # runtime's version and fail with a symbol-version error
    # (`OPENSSL_3.3.0 not found`). So: always prefer the runtime-tree copy.
    preferred = {}
    for lib_dir in (runtime / 'lib',
                    runtime / 'lib' / ('python%s' % version) / 'lib-dynload'):
        for candidate_lib in Path(lib_dir).glob('*.so*'):
            if candidate_lib.is_file():
                preferred.setdefault(candidate_lib.name, candidate_lib)

    loader = None
    for candidate in binaries:
        for dependency in _ldd(candidate):
            name = Path(dependency).name
            source = preferred.get(name, Path(dependency))
            target = stage / 'ld' / name
            if not target.exists() or (source in preferred and
                                       _sha(target) != _sha(source)):
                shutil.copy2(source, target)
            if loader is None and ('ld-linux' in name or 'ld.so' in name):
                loader = name
    explicit_loader = '/lib/ld-linux-%s.so.1' % ('aarch64' if platform.machine() == 'aarch64'
                                                 else 'x86-64')
    if loader is None and Path(explicit_loader).is_file():
        shutil.copy2(explicit_loader, stage / 'ld' / Path(explicit_loader).name)
        loader = Path(explicit_loader).name
    if loader is None:
        return {'python': False, 'reason': 'could not determine the ELF loader'}
    (stage / 'loader-name.txt').write_text(loader + '\n', encoding='utf-8')

    # Launcher: private loader + --library-path keeps guest libc untouched.
    # GUEST_TOOLS is where the tools disk is mounted inside the VM.
    launcher = directory / 'bin' / 'python3'
    launcher.write_text(
        '#!/bin/sh\n'
        'PYTHONHOME=%s/python\n'
        'export PYTHONHOME\n'
        'exec %s/python/ld/%s --library-path %s/python/ld '
        '%s/python/bin/python3 "$@"\n'
        % (GUEST_MOUNT, GUEST_MOUNT, loader, GUEST_MOUNT, GUEST_MOUNT),
        encoding='utf-8')
    os.chmod(launcher, 0o755)
    (directory / 'bin' / 'python').symlink_to('python3')

    # Self-test through the private loader using HOST paths (the launcher itself
    # refers to the guest mount point, which does not exist on the host). Without
    # this the staged runtime is only found to be broken inside a VM.
    env = dict(os.environ, PYTHONHOME=str(stage))
    check = subprocess.run(
        [str(stage / 'ld' / loader), '--library-path', str(stage / 'ld'),
         str(stage / 'bin' / 'python3'), '-c',
         _SELF_TEST_SOURCE],
        capture_output=True, text=True, timeout=120, env=env)
    if check.returncode != 0 or 'BWX_PY_OK' not in check.stdout:
        return {'python': False, 'reason': 'staged python failed self-test: %s'
                % (check.stderr or check.stdout)[-1200:]}
    return {'python': True, 'version': version, 'loader': loader,
            'self_test': check.stdout.strip(), 'guest_mount': GUEST_MOUNT}


_TRIM = {'test', 'tests', 'idlelib', 'tkinter', 'lib2to3', 'site-packages',
         'ensurepip', 'venv', '__pycache__', 'turtledemo', 'pydoc_data'}


def _copy_tree_trimmed(src, dst):
    shutil.copytree(src, dst, symlinks=True,
                    ignore=shutil.ignore_patterns(*_TRIM))
    for cache in dst.rglob('__pycache__'):
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)


def _ldd(binary):
    result = subprocess.run(['ldd', str(binary)], capture_output=True, text=True)
    paths = []
    for line in (result.stdout or '').splitlines():
        parts = line.split()
        if '=>' in parts:
            target = parts[parts.index('=>') + 1]
        elif parts and parts[0].startswith('/'):
            target = parts[0]
        else:
            continue
        if target.startswith('/') and Path(target).is_file():
            paths.append(target)
    return paths


def _python_version(binary):
    result = subprocess.run([str(binary), '-c',
                             'import sys;print("%d.%d"%sys.version_info[:2])'],
                            capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise Error('cannot determine python version of %s: %s' % (binary, result.stderr[:200]))
    return result.stdout.strip()


def build_tools_image(directory, disk_path, *, extra_mib=32):
    """mkfs.ext4 -d <tools dir> -> read-only tools disk image."""
    directory = Path(directory)
    disk_path = Path(disk_path)
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    used = sum(p.stat().st_size for p in directory.rglob('*')
               if p.is_file() and not p.is_symlink())
    size = max(64 * MIB, used + extra_mib * MIB)
    if disk_path.exists():
        disk_path.unlink()
    with disk_path.open('wb') as stream:
        stream.truncate(size)
    srun(['mkfs.ext4', '-q', '-F', '-m', '0', '-O', '^metadata_csum_seed',
          '-d', str(directory), str(disk_path)])
    return {'path': str(disk_path), 'bytes': size, 'content_bytes': used}
