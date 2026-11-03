"""Configuration: config.toml (gitignored) < environment < command line.

Mirrors the `scripts/config.local.sh` convention of /home/cxd/mpam so per-machine
paths never enter version control, but uses TOML + environment overrides so the
same values are reachable from Python and from the shell entry points.
"""
import os
import tomllib
from pathlib import Path

from . import util
from .util import Error, GIB

DEFAULTS = {
    'root': None,               # resolved to the repository root when unset
    'dataset': '/home/cxd/data',
    'firecracker': '/var/lib/aenv/deps/firecracker/1.15.1-patch-v3/firecracker',
    'kernel': '/var/lib/aenv/deps/kernel/vmlinux-6.1.175/vmlinux.bin',
    'ddrc_scale_bytes': 32,
    'bandwidth_enabled': True,
    'vcpus': 2,
    'mem_mib': 4096,
    'numa_node': 0,
    'case_timeout': 1800,
    'boot_timeout': 120,
    'console': 'markers',       # off | markers | full
    'console_verify': True,
}

_ENV_PREFIX = 'BWX_'
_BOOL_KEYS = {'bandwidth_enabled', 'console_verify'}
_INT_KEYS = {'ddrc_scale_bytes', 'vcpus', 'mem_mib', 'numa_node'}
_FLOAT_KEYS = {'case_timeout', 'boot_timeout'}

# Repository root = parent of src/ (…/mpam-bw-experiment).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class Config:
    """Flat attribute view over the merged configuration."""

    def __init__(self, values, source):
        self._values = values
        self.source = source

    def __getattr__(self, name):
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(name)

    def __getitem__(self, name):
        return self._values[name]

    def get(self, name, default=None):
        return self._values.get(name, default)

    def as_dict(self):
        return dict(self._values)

    # Derived paths ---------------------------------------------------------
    @property
    def images_dir(self):
        return self.root / 'images'

    @property
    def cases_dir(self):
        return self.root / 'cases'

    @property
    def work_dir(self):
        return self.root / 'work'

    @property
    def logs_dir(self):
        return self.root / 'logs'

    def case_workspace(self, instance_id):
        return self.cases_dir / instance_id

    def case_image(self, instance_id):
        return self.case_workspace(instance_id) / 'image.ext4'


def find_config_file(explicit=None):
    """Locate config.toml: explicit path > $BWX_CONFIG > root/config.toml > cwd."""
    for candidate in (explicit, os.environ.get(_ENV_PREFIX + 'CONFIG'),
                      REPO_ROOT / 'config.toml', Path.cwd() / 'config.toml'):
        if candidate:
            path = Path(candidate)
            if path.is_file():
                return path
    return None


def load(explicit=None, overrides=None):
    """Merge defaults < config.toml < environment < explicit overrides."""
    values = dict(DEFAULTS)
    values['root'] = REPO_ROOT
    source = {'defaults': sorted(DEFAULTS)}

    path = find_config_file(explicit)
    if path:
        with path.open('rb') as stream:
            values.update(tomllib.load(stream))
        source['file'] = str(path)

    for key in list(values):
        env = os.environ.get(_ENV_PREFIX + key.upper())
        if env is None:
            continue
        if key in _BOOL_KEYS:
            values[key] = env.strip().lower() in ('1', 'true', 'yes', 'on')
        elif key in _INT_KEYS:
            values[key] = int(env)
        elif key in _FLOAT_KEYS:
            values[key] = float(env)
        else:
            values[key] = env
        source.setdefault('env', []).append(key)

    for key, value in (overrides or {}).items():
        if value is None:
            continue
        values[key] = value
        source.setdefault('cli', []).append(key)

    values['root'] = Path(values['root']).expanduser().resolve()
    for key in ('dataset', 'firecracker', 'kernel'):
        values[key] = Path(values[key]).expanduser()

    if values['console'] not in ('off', 'markers', 'full'):
        raise Error('--console must be one of: off, markers, full')
    return Config(values, source)


def add_arguments(parser):
    """Register the config-related CLI flags shared by every entry point."""
    parser.add_argument('--config', type=Path, metavar='FILE',
                        help='config.toml path (default: $BWX_CONFIG, <repo>/config.toml)')
    parser.add_argument('--root', type=Path, metavar='DIR', help='data/log root directory')
    parser.add_argument('--dataset', type=Path, metavar='DIR',
                        help='dataset dir with manifest.csv and replay-50/')
    parser.add_argument('--firecracker', type=Path, metavar='PATH', help='Firecracker binary')
    parser.add_argument('--kernel', type=Path, metavar='PATH', help='guest kernel image')
    parser.add_argument('--console', choices=('off', 'markers', 'full'),
                        help='serial console capture: off (discard), markers '
                             '(keep protocol lines only, default), full (keep everything)')
    parser.add_argument('--no-console-verify', dest='console_verify', action='store_false',
                        help='skip serial marker verification (NOT recommended: '
                             'a VM can exit 0 after a failed init)')


def from_args(args):
    """Build a Config from a parsed argparse namespace."""
    overrides = {key: getattr(args, key, None) for key in DEFAULTS}
    if getattr(args, 'console_verify', True) is False:
        overrides['console_verify'] = False
    return load(getattr(args, 'config', None), overrides)


def validate_runtime(config, *, need_images=True):
    """Check the binaries/paths a VM launch depends on. Fails early and loudly.

    A missing kernel or firecracker path is the #1 cause of "VM exits 0 with an
    empty console.log" (see docs/REPOSITORIES.md), so it is verified here rather
    than inside the VM launch path.
    """
    missing = []
    for label, path in (('firecracker', config.firecracker), ('kernel', config.kernel)):
        if not Path(path).is_file():
            missing.append('%s not found: %s' % (label, path))
        elif label == 'firecracker' and not os.access(path, os.X_OK):
            missing.append('firecracker not executable: %s' % path)
    if not os.path.exists('/dev/kvm'):
        missing.append('/dev/kvm missing')
    elif not os.access('/dev/kvm', os.R_OK | os.W_OK):
        missing.append('/dev/kvm not readable+writable (run with sudo)')
    if need_images and not Path(config.dataset).is_dir():
        missing.append('dataset dir: %s' % config.dataset)
    if missing:
        raise Error('runtime prerequisites missing:\n  ' + '\n  '.join(missing))
    for directory in (config.images_dir, config.cases_dir, config.work_dir, config.logs_dir):
        directory.mkdir(parents=True, exist_ok=True)


def summary(config):
    """Short one-line-per-key rendering for plan.json and logs."""
    out = {}
    for key in sorted(config.as_dict()):
        value = config[key]
        out[key] = str(value) if isinstance(value, Path) else value
    out['repo_root'] = str(REPO_ROOT)
    out['disk_free_root_gib'] = round(util.free_bytes(config.root) / GIB, 1)
    return out
