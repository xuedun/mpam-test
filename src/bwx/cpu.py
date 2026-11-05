"""Host CPU utilization sampling (/proc/stat differential) + global PSI.

The CPU-side counterpart of bandwidth.BandwidthCollector, but pure
userspace: a daemon thread snapshots /proc/stat every interval and reports
busy% series for

* ``pool`` -- exactly the CPUs this run's VMs are pinned to (the caller
  supplies the set), i.e. the VM pool pressure regardless of what anyone
  else does elsewhere;
* every NUMA node -- *all* of the node's online CPUs, bound or not: a node
  running clearly busier than the pool is the tell for foreign load on the
  un-bound tail (e.g. cpus 134-181 at 48 VMs);
* ``total`` -- whole machine, sanity only (shared hosts pollute it).

Each frame also reads /proc/pressure/{cpu,memory}: PSI avg10/60/300 are
sliding-window stall percentages, the direct "slowdown" evidence that a
plain busy% cannot give.

busy% = d(user+nice+system+irq+softirq+steal) / d(total) per interval,
where total includes idle+iowait (iowait counted as not-busy, conservative).
"""
import threading
import time
from pathlib import Path

from .util import write_csv, write_json

_PSI_SOURCES = ('cpu', 'memory')
_PROC_STAT = Path('/proc/stat')
_PROC_PRESSURE = Path('/proc/pressure')


def read_cpu_stat(path=_PROC_STAT):
    """{cpu: (busy_jiffies, total_jiffies)} from the per-cpu /proc/stat lines.

    The aggregate `cpu ` line is skipped: only logical-cpu rows are kept.
    Jiffy counters are monotonic since boot, so diffs never wrap.
    """
    cpus = {}
    with Path(path).open() as stream:
        for line in stream:
            if not line.startswith('cpu'):
                continue
            name = line.split(None, 1)[0]
            if name == 'cpu':                       # the machine-wide line
                continue
            try:
                cpu = int(name[3:])
                fields = [int(value) for value in line.split()[1:9]]
            except (ValueError, IndexError):
                continue
            user, nice, system, idle, iowait, irq, softirq, steal = fields
            busy = user + nice + system + irq + softirq + steal
            cpus[cpu] = (busy, busy + idle + iowait)
    return cpus


def read_psi(kind='cpu'):
    """{'some': {avg10, avg60, avg300}, 'full': {...}} from /proc/pressure."""
    path = _PROC_PRESSURE / kind
    try:
        text = path.read_text()
    except OSError:
        return None
    out = {}
    for line in text.splitlines():
        kind_of_line, _, rest = line.partition(' ')
        stats = {}
        for token in rest.split():
            key, _, value = token.partition('=')
            if key.startswith('avg'):
                try:
                    stats[key] = round(float(value), 3)
                except ValueError:
                    continue
        if stats:
            out[kind_of_line] = stats
    return out


def busy_pct(prev, snap, cpus):
    """Aggregate busy% over `cpus` between two read_cpu_stat snapshots."""
    busy = total = 0
    for cpu in cpus:
        before, after = prev.get(cpu), snap.get(cpu)
        if before is None or after is None:
            continue
        busy += max(0, after[0] - before[0])
        total += max(0, after[1] - before[1])
    return round(100.0 * busy / total, 2) if total else None


class CpuCollector:
    """Sample /proc/stat + PSI for the lifetime of an experiment.

    Usage::

        collector = CpuCollector(out_dir, interval=1.0, pool_cpus={86..133})
        collector.start()
        ... run VMs ...
        collector.stop()          # writes cpu-series.json / cpu-<label>.csv
        series = collector.read() # {'pool': [[t, busy%], ...], nodes, 'total'}
    """

    def __init__(self, out_dir, *, interval=1.0, pool_cpus=(), label='replay'):
        self.out_dir = Path(out_dir)
        self.interval = float(interval)
        self.label = label
        self.pool = sorted(int(cpu) for cpu in set(pool_cpus))
        self.series_path = self.out_dir / 'cpu-series.json'
        self.csv_path = self.out_dir / ('cpu-%s.csv' % label)
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._frames = []           # [(t, {scope: busy%}, psi dict)]
        self.epoch = None
        self._last = None

    # ------------------------------------------------------------- lifecycle
    def start(self):
        if self._thread is not None:
            return False
        self.epoch = time.time()
        self._last = read_cpu_stat()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='cpu-collector')
        self._thread.start()
        return True

    def _loop(self):
        while not self._stop.wait(self.interval):
            snap = read_cpu_stat()
            frame = self._frame(self._last, snap)
            self._last = snap
            if frame is not None:
                with self._lock:
                    self._frames.append(frame)

    def stop(self):
        """Stop sampling and flush one final (partial) frame, then write."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        snap = read_cpu_stat()
        frame = self._frame(self._last, snap)
        if frame is not None:
            with self._lock:
                self._frames.append(frame)
        self.write()

    # --------------------------------------------------------------- frames
    def _frame(self, prev, snap):
        from .bandwidth import node_cpulists

        if not prev or not snap:
            return None
        t = round(time.time() - self.epoch, 3)
        scopes = {}
        if self.pool:
            scopes['pool'] = busy_pct(prev, snap, self.pool)
        for node, cpus in node_cpulists().items():
            scopes[str(node)] = busy_pct(prev, snap, cpus)
        scopes['total'] = busy_pct(prev, snap, set(snap) & set(prev))
        psi = {}
        for source in _PSI_SOURCES:
            reading = read_psi(source)
            if reading:
                psi[source] = reading
        return (t, scopes, psi)

    # --------------------------------------------------------------- output
    def write(self):
        series = {}
        psi = {source: [] for source in _PSI_SOURCES}
        with self._lock:
            frames = list(self._frames)
        for t, scopes, frame_psi in frames:
            for scope, value in scopes.items():
                if value is not None:
                    series.setdefault(scope, []).append([t, value])
            for source in _PSI_SOURCES:
                if source in frame_psi:
                    psi[source].append([t, frame_psi[source]])
        self.out_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.series_path, {
            'epoch_wall': self.epoch,
            'interval_s': self.interval,
            'pool_cpus': self.pool,
            'series': series,
            'psi': psi,
        })
        rows = []
        for scope in sorted(series):
            for t, value in series[scope]:
                rows.append({'t_s': t, 'scope': scope, 'busy_pct': value})
        write_csv(self.csv_path, rows, ['t_s', 'scope', 'busy_pct'])
        return series

    def read(self):
        """The written series (or the in-memory one before write)."""
        if self.series_path.is_file():
            import json
            doc = json.loads(self.series_path.read_text())
            return doc.get('series', {})
        return self.write()
