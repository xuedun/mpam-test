"""`bwx doctor` -- verify every host prerequisite before burning experiment time.

Checks are ordered from cheapest to most intrusive, and each one prints the
evidence it used so a failure is actionable rather than mysterious. This exists
because the two most expensive failure modes on a new machine are silent:
a missing kernel path produces a VM that exits 0 with an empty console.log, and a
wrong ddrc scale silently rescales every bandwidth number.
"""
import os
import platform
import shutil
import sys
from pathlib import Path

from .. import bandwidth as bw
from .. import config as config_mod
from ..util import Error, run, srun, sudo_prefix

COMMAND = 'doctor'
HELP = 'check host prerequisites (tools, KVM, resctrl, ddrc PMU, dataset)'

TOOLS = ('podman', 'mkfs.ext4', 'e2fsck', 'debugfs', 'numactl', 'tar', 'du',
         'find', 'sha256sum', 'cksum', 'perf', 'awk', 'sed')


def add_arguments(parser):
    parser.add_argument('--json', action='store_true', help='emit a machine-readable report')


def run(args):
    config = config_mod.from_args(args)
    report = {'host': {'machine': platform.machine(), 'python': platform.python_version(),
                       'kernel': platform.release(), 'euid': os.geteuid()},
              'checks': []}

    def check(name, ok, detail='', *, warn=False):
        # `warn` records a fact that shapes the experiment but is not a defect
        # (e.g. no MB_MON: per-group bandwidth simply has to come from the PMU).
        report['checks'].append({'name': name, 'ok': bool(ok),
                                 'warn': bool(warn), 'detail': detail})
        return ok

    # -- tools ---------------------------------------------------------------
    missing = []
    for tool in TOOLS:
        found = shutil.which(tool) or shutil.which(tool, path=os.environ['PATH'] + ':/usr/sbin:/sbin')
        if not found:
            missing.append(tool)
    check('tools', not missing,
          'missing: %s' % ','.join(missing) if missing
          else 'all %d tools found' % len(TOOLS))

    # -- root / sudo ---------------------------------------------------------
    if os.geteuid() == 0:
        check('privileges', True, 'running as root')
    else:
        try:
            prefix = sudo_prefix()
            check('privileges', True, 'sudo available: %s' % ' '.join(prefix))
        except Error as exc:
            check('privileges', False, str(exc))

    # -- KVM + binaries ------------------------------------------------------
    check('kvm', os.path.exists('/dev/kvm') and os.access('/dev/kvm', os.R_OK | os.W_OK),
          '/dev/kvm %s' % ('readable+writable' if os.access('/dev/kvm', os.R_OK | os.W_OK)
                           else 'present but not accessible (need root/kvm group)'))
    for label, path in (('firecracker', config.firecracker), ('kernel', config.kernel)):
        ok = Path(path).is_file() and (label != 'firecracker' or os.access(path, os.X_OK))
        check(label, ok, str(path))

    # -- dataset -------------------------------------------------------------
    dataset = Path(config.dataset)
    manifest = dataset / 'manifest.csv'
    check('dataset', manifest.is_file(), str(manifest))
    if manifest.is_file():
        rows = _count_manifest(manifest)
        traces = len(list(dataset.glob('*/*.replay.json'))) + len(list(dataset.glob('*.replay.json')))
        check('dataset_cases', rows > 0, '%d manifest rows, %d trajectory files' % (rows, traces))

    # -- resctrl / MPAM ------------------------------------------------------
    resctrl = Path(config.resctrl)
    mounted = (resctrl / 'schemata').is_file()
    check('resctrl_mounted', mounted, str(resctrl))
    if mounted:
        try:
            from ..mpam import Platform
            plat = Platform(resctrl)
            check('mpam_mb', bool(plat.domains),
                  'domains=%s gran=%d min=%d closids=%d mbhdl=%s'
                  % (plat.domains, plat.granularity, plat.min_bandwidth,
                     plat.num_closids, plat.has_mbhdl))
            # Group capacity is a real constraint on MPAM hosts with a tiny
            # monitoring pool: num_closids may say 32 while num_rmids=1 means a
            # handful of groups, and exhausting it needs a resctrl remount.
            capacity = plat.group_capacity(limit=4)
            pool = plat.monitoring_pool()
            check('mpam_group_capacity', capacity >= 1, warn=capacity < 4,
                  detail='can create %d group(s) right now; num_rmids=%s existing=%d. '
                         '%s' % (capacity, pool['num_rmids'], pool['existing_groups'],
                                 'OK' if capacity >= 4 else
                                 'Group creation is scarce here: reuse one group per '
                                 'distinct cap (GroupPool does this automatically). '
                                 'If this is 0, a previous run leaked groups or '
                                 'exhausted the pool -- run `bwx cleanup --groups '
                                 '--force`, and if that does not help the resctrl '
                                 'mount needs remounting (host-level action).'))
            check('mpam_monitoring', plat.has_mb_monitoring, warn=True,
                  detail='MB_MON %s; L3_MON features=%s num_rmids=%s'
                  % ('present' if plat.has_mb_monitoring else 'ABSENT (per-group '
                     'bandwidth cannot be read from resctrl; ddrc PMU is the only '
                     'ground truth)', ','.join(plat.monitor_features) or '-',
                     _read(resctrl / 'info/L3_MON/num_rmids', '?')))
        except Error as exc:
            check('mpam_mb', False, str(exc))

    # -- ddrc PMU ------------------------------------------------------------
    topo = bw.BandwidthTopology(scale_bytes=config.ddrc_scale_bytes)
    check('ddrc_pmu', topo.available,
          '%d events across %d sccl' % (len(topo.events()), len(topo.grouped))
          if topo.available else 'no hisi_sccl*_ddrc* PMUs found')
    if topo.available:
        unresolved = list(topo.unresolved)
        check('ddrc_node_mapping', not unresolved,
              'sccl->node %s' % {k: v for k, v in sorted(topo.sccl_node.items())}
              if not unresolved else 'unresolved sccl: %s' % unresolved)
        # Access requires root (perf_event_paranoid).
        paranoid = _read(Path('/proc/sys/kernel/perf_event_paranoid'), '?')
        accessible = _probe_perf(topo)
        # Uncore counters need root on most hosts; without it the rest of doctor
        # is still meaningful, so report it as a warning when we are not root.
        check('ddrc_readable', accessible, warn=os.geteuid() != 0,
              detail='perf_event_paranoid=%s%s' % (paranoid, '' if accessible else
                     ' (re-run under sudo to read uncore counters)'))

    # -- NUMA ----------------------------------------------------------------
    nodes = []
    for entry in sorted(Path('/sys/devices/system/node').glob('node[0-9]*')):
        cpulist = entry / 'cpulist'
        if cpulist.is_file():
            cpus = bw.expand_cpulist(cpulist.read_text())
            online = bw.expand_cpulist(_read(Path('/sys/devices/system/cpu/online'), ''))
            nodes.append((entry.name, len(cpus & online)))
    check('numa', bool(nodes), 'node usable CPUs: %s' % dict(nodes))

    # -- disk ----------------------------------------------------------------
    from ..util import free_bytes, human_size
    free = free_bytes(config.root)
    check('disk_space', free > 8 * 1024 ** 3, '%s free under %s' % (human_size(free), config.root))

    # -- writable paths ------------------------------------------------------
    try:
        for directory in (config.images_dir, config.cases_dir, config.work_dir, config.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)
        check('paths_writable', True, str(config.root))
    except OSError as exc:
        check('paths_writable', False, str(exc))

    failures = [c for c in report['checks'] if not c['ok'] and not c.get('warn')]
    warnings = [c for c in report['checks'] if not c['ok'] and c.get('warn')]
    if args.json:
        report['failures'] = [c['name'] for c in failures]
        report['warnings'] = [c['name'] for c in warnings]
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print('bwx doctor (%s, %s, python %s)' % (report['host']['machine'],
                                                  report['host']['kernel'],
                                                  report['host']['python']))
        width = max(len(c['name']) for c in report['checks'])
        for entry in report['checks']:
            mark = 'ok  ' if entry['ok'] else ('warn' if entry.get('warn') else 'FAIL')
            print('  [%s] %-*s  %s' % (mark, width, entry['name'], entry['detail']))
        print('\n%d passed, %d warning(s), %d failure(s)'
              % (len(report['checks']) - len(failures) - len(warnings),
                 len(warnings), len(failures)))
        if failures:
            print('fix the FAIL entries above before running experiments', file=sys.stderr)
    return 1 if failures else 0


def _read(path, default=''):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def _count_manifest(path):
    import csv
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return sum(1 for row in csv.DictReader(stream) if (row.get('instance_id') or '').strip())


def _probe_perf(topology):
    """Can we actually read one ddrc counter? Needs root on most hosts."""
    if not topology.events():
        return False
    try:
        result = srun(['perf', 'stat', '-a', '-I', '1000', '-x', ',',
                       '-e', topology.events()[0], '--', 'sleep', '1'],
                      check=False, timeout=30, quiet=True)
    except (Error, OSError):
        return False
    # perf stat -x, prints CSV records on stderr.
    output = (result.stderr or '') + (result.stdout or '')
    return result.returncode == 0 and any(ch.isdigit() for ch in output)
