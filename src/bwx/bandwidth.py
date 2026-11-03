"""Memory-bandwidth ground truth from the HiSilicon ddrc uncore PMUs.

Why this exists
---------------
Guest-side self-reported bandwidth is untrustworthy for a host-level study, so
real DRAM traffic is observed through the uncore PMU (resctrl monitoring is not
used at all):

    hisi_sccl<N>_ddrc<M>/flux_rd/   and   /flux_wr/

Each counter increment is `ddrc_scale_bytes` (32 on this host; verified by
`bwx calibrate`). Summing every ddrc of one `sccl` gives that NUMA node's
read+write bandwidth. The sccl -> node mapping is derived from each PMU's
`cpumask` (the CPU the driver samples on) against
`/sys/devices/system/node/node*/cpulist` -- measured, never hard-coded.

Cost control
------------
One `perf stat -a -I <interval>` process for the whole run, line-buffered CSV to
a file, parsed after the fact. At the default 1s interval this is 60 counter
reads per second machine-wide, which is negligible next to a VM. The collector is
switchable (`bandwidth_enabled = false`) for hosts without these PMUs, and every
consumer degrades gracefully to "no bandwidth data" rather than failing.

Because the counters are per *node* and not per *group*, per-VM attribution in a
concurrent run is impossible -- see docs/EXPERIMENT-PLAN.md "known limitations".
What we can do, and what the analysis relies on, is measure each node's aggregate
and correlate it with the VMs pinned to that node.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

from .util import Error, sudo_prefix, write_json

PMU_RE = re.compile(r'^hisi_sccl(\d+)_ddrc(\d+)_(\d+)$')
EVENTS = ('flux_rd', 'flux_wr')


def node_cpulists():
    """{node_id: set(cpu)} from sysfs."""
    result = {}
    base = Path('/sys/devices/system/node')
    if not base.is_dir():
        return result
    for entry in sorted(base.glob('node[0-9]*')):
        path = entry / 'cpulist'
        if path.is_file():
            result[int(entry.name.removeprefix('node'))] = expand_cpulist(path.read_text())
    return result


def expand_cpulist(text):
    """`0-3,8,10-11` -> {0,1,2,3,8,10,11}."""
    cpus = set()
    for part in (text or '').strip().split(','):
        if not part:
            continue
        if '-' in part:
            low, high = part.split('-', 1)
            cpus.update(range(int(low), int(high) + 1))
        else:
            cpus.add(int(part))
    return cpus


def format_cpulist(cpus):
    """{0,1,2,5} -> `0-2,5` (compact, for numactl --physcpubind)."""
    ordered = sorted(cpus)
    if not ordered:
        return ''
    out = []
    start = previous = ordered[0]
    for cpu in ordered[1:]:
        if cpu == previous + 1:
            previous = cpu
            continue
        out.append('%d' % start if start == previous else '%d-%d' % (start, previous))
        start = previous = cpu
    out.append('%d' % start if start == previous else '%d-%d' % (start, previous))
    return ','.join(out)


def discover_pmus(devices_dir=Path('/sys/bus/event_source/devices')):
    """-> {sccl: [pmu names]}, i.e. the ddrc counters grouped by socket cluster."""
    grouped = {}
    for entry in sorted(Path(devices_dir).iterdir()):
        match = PMU_RE.match(entry.name)
        if not match:
            continue
        events_dir = entry / 'events'
        if not all((events_dir / event).is_file() for event in EVENTS):
            continue
        grouped.setdefault(int(match.group(1)), []).append(entry.name)
    return grouped


def sccl_to_node(devices_dir=Path('/sys/bus/event_source/devices')):
    """Map sccl id -> NUMA node using each PMU's cpumask. Measured per host."""
    cpulists = node_cpulists()
    mapping, unresolved = {}, {}
    for sccl, names in discover_pmus(devices_dir).items():
        node = None
        for name in names:
            path = Path(devices_dir) / name / 'cpumask'
            if not path.is_file():
                continue
            try:
                cpu = int(path.read_text().strip().split(',')[0])
            except (ValueError, OSError):
                continue
            for candidate, cpus in cpulists.items():
                if cpu in cpus:
                    node = candidate
                    break
            if node is not None:
                break
        if node is None:
            unresolved[sccl] = names
        else:
            mapping[sccl] = node
    return mapping, unresolved


class BandwidthTopology:
    """Everything needed to build perf event lists and attribute them to nodes."""

    def __init__(self, scale_bytes=32, devices_dir=Path('/sys/bus/event_source/devices')):
        self.scale_bytes = int(scale_bytes)
        self.grouped = discover_pmus(devices_dir)
        self.sccl_node, self.unresolved = sccl_to_node(devices_dir)

    @property
    def available(self):
        return bool(self.grouped)

    def nodes(self):
        return sorted(set(self.sccl_node.values()))

    def events(self):
        """Flat list of `<pmu>/flux_rd/` specs for `perf stat -e`."""
        specs = []
        for names in self.grouped.values():
            for name in names:
                for event in EVENTS:
                    specs.append('%s/%s/' % (name, event))
        return specs

    def node_of_pmu(self, pmu_name):
        match = PMU_RE.match(pmu_name)
        if not match:
            return None
        return self.sccl_node.get(int(match.group(1)))

    def as_dict(self):
        return {'available': self.available, 'scale_bytes': self.scale_bytes,
                'sccl_to_node': {str(k): v for k, v in sorted(self.sccl_node.items())},
                'unresolved_sccl': {str(k): v for k, v in sorted(self.unresolved.items())},
                'pmus_per_sccl': {str(k): len(v) for k, v in sorted(self.grouped.items())},
                'nodes': self.nodes(), 'event_count': len(self.events())}


class BandwidthCollector:
    """Runs one `perf stat -a -I <interval>` for the lifetime of an experiment.

    Usage::

        with BandwidthCollector(topology, out_dir, interval=1.0) as collector:
            ... run VMs ...
        series = collector.read()          # per-node GB/s time series

    The child is started with sudo when we are not root (perf_event_paranoid
    usually forbids uncore access otherwise) and is always terminated on exit,
    including on exceptions, so a failed run cannot leave a perf process behind.
    """

    def __init__(self, topology, out_dir, *, interval=1.0, label='run'):
        self.topology = topology
        self.out_dir = Path(out_dir)
        self.interval = float(interval)
        self.label = label
        self.csv_path = self.out_dir / ('bandwidth-%s.csv' % label)
        self.meta_path = self.out_dir / ('bandwidth-%s.json' % label)
        self.process = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def start(self):
        if not self.topology.available:
            return False
        self.out_dir.mkdir(parents=True, exist_ok=True)
        command = [*sudo_prefix(), 'perf', 'stat', '-a', '-I',
                   str(int(self.interval * 1000)), '-x', ',',
                   '-e', ','.join(self.topology.events()), '--',
                   'sleep', '86400']
        self.csv_path.write_text('', encoding='utf-8')
        self.stream = self.csv_path.open('a', encoding='utf-8')
        # `perf stat -x,` writes its CSV records to stderr (stdout is reserved for
        # the command's own output), so the counter stream must be captured there.
        self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                        stderr=self.stream,
                                        start_new_session=True)
        write_json(self.meta_path, {'label': self.label, 'interval_s': self.interval,
                                    'command': command, 'topology': self.topology.as_dict(),
                                    'pid': self.process.pid})
        return True

    def stop(self):
        if self.process is None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        except Exception:  # noqa: BLE001 - stop() must not mask the real error
            pass
        finally:
            self.process = None
            try:
                self.stream.close()
            except (AttributeError, OSError):
                pass

    # --------------------------------------------------------------- parsing
    def read(self):
        """Parse the CSV into {node: [(t_s, rd_gbs, wr_gbs)]} plus an all-node sum."""
        if not self.csv_path.is_file():
            return {}
        series = {}
        scale = self.topology.scale_bytes
        with self.csv_path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                fields = line.split(',')
                if len(fields) < 5:
                    continue
                try:
                    seconds = float(fields[0])
                    count = float(fields[1])
                except ValueError:
                    continue   # perf's header/footer lines and <not counted>
                spec = fields[3].strip()
                if '/' not in spec:
                    continue
                pmu, _, event = spec.partition('/')
                node = self.topology.node_of_pmu(pmu)
                if node is None:
                    continue
                gbs = count * scale / (self.interval * 1e9)
                bucket = series.setdefault(node, {})
                point = bucket.setdefault(seconds, [seconds, 0.0, 0.0])
                if event.startswith('flux_rd'):
                    point[1] += gbs
                elif event.startswith('flux_wr'):
                    point[2] += gbs
        out = {}
        for node, bucket in series.items():
            out[node] = [[t, round(v[1], 4), round(v[2], 4)]
                         for t, v in sorted(bucket.items())]
        # Aggregate across nodes for whole-machine accounting.
        if out:
            times = sorted({point[0] for rows in out.values() for point in rows})
            aggregate = []
            for t in times:
                rd = sum(dict((p[0], p[1]) for p in out[n]).get(t, 0.0) for n in out)
                wr = sum(dict((p[0], p[2]) for p in out[n]).get(t, 0.0) for n in out)
                aggregate.append([t, round(rd, 4), round(wr, 4)])
            out['all'] = aggregate
        return out

    @staticmethod
    def window_stats(series, node, t_start, t_end):
        """peak/mean GB/s over [t_start, t_end] for one node (or 'all')."""
        rows = series.get(node, [])
        points = [r for r in rows if t_start <= r[0] <= t_end]
        if not points:
            return None
        totals = [r[1] + r[2] for r in points]
        reads = [r[1] for r in points]
        writes = [r[2] for r in points]
        return {'samples': len(points),
                'span_s': round(points[-1][0] - points[0][0], 3),
                'peak_gbs': round(max(totals), 3),
                'mean_gbs': round(sum(totals) / len(totals), 3),
                'mean_rd_gbs': round(sum(reads) / len(reads), 3),
                'mean_wr_gbs': round(sum(writes) / len(writes), 3)}


def require_perf():
    path = shutil.which('perf')
    if not path:
        raise Error('perf not found; bandwidth collection needs it '
                    '(or set bandwidth_enabled=false)')
    return path
