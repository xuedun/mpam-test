import tempfile
import threading
import unittest
from pathlib import Path

from bwx import cpu as cpu_mod
from bwx.cpu import CpuCollector, busy_pct, read_cpu_stat, read_psi


def _stat_text(rows):
    lines = ['cpu  100 0 100 1000 10 0 10 0 0 0']
    lines += ['cpu%d  %s' % (cpu_, ' '.join(str(v) for v in values))
              for cpu_, values in rows]
    return '\n'.join(lines) + '\n'


_PROC_LINE = ('1 (some (comm)) R 1 1 1 0 -1 4194560 100 200 0 0 30 20 '
              '0 0 20 0 1 0 100')


class ReadCpuStatTest(unittest.TestCase):
    def _file(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'stat'
        path.write_text(text)
        return path

    def test_parses_per_cpu_lines_and_skips_aggregate(self):
        path = self._file(_stat_text([(0, (10, 0, 5, 100, 2, 0, 1, 0)),
                                      (1, (10, 0, 5, 100, 2, 0, 1, 0))]))
        cpus = read_cpu_stat(path)
        self.assertEqual(sorted(cpus), [0, 1])
        busy, total = cpus[0]
        self.assertEqual(busy, 16)          # user+nice+system+irq+softirq+steal
        self.assertEqual(total, 118)        # + idle + iowait

    def test_busy_pct_differential(self):
        before = {0: (100, 200), 1: (100, 200)}
        after = {0: (150, 300), 1: (100, 300)}   # cpu1 idles: idle ticks run
        # cpu0: busy +50 / total +100 -> 50%; cpu1: 0/100
        self.assertEqual(busy_pct(before, after, [0]), 50.0)
        self.assertEqual(busy_pct(before, after, [0, 1]), 25.0)

    def test_missing_cpus_are_skipped(self):
        before = {0: (100, 200)}
        after = {0: (120, 300), 5: (0, 0)}
        self.assertEqual(busy_pct(before, after, [0, 5]), 20.0)

    def test_no_data_returns_none(self):
        self.assertIsNone(busy_pct({}, {}, [0]))


class ReadPsiTest(unittest.TestCase):
    def _patch_dir(self, text, name='cpu'):
        import bwx.cpu as cpu_mod
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / name).write_text(text)
        original = cpu_mod._PROC_PRESSURE
        cpu_mod._PROC_PRESSURE = Path(tmp.name)
        self.addCleanup(setattr, cpu_mod, '_PROC_PRESSURE', original)
        return Path(tmp.name)

    def test_parses_some_and_full(self):
        text = ('some avg10=1.25 avg60=0.50 avg300=0.10 total=123456\n'
                'full avg10=0.00 avg60=0.10 avg300=0.02 total=23456\n')
        self._patch_dir(text)
        reading = read_psi('cpu')
        self.assertEqual(reading['some']['avg10'], 1.25)
        self.assertEqual(reading['some']['avg60'], 0.5)
        self.assertEqual(reading['full']['avg300'], 0.02)

    def test_missing_file_returns_none(self):
        original = cpu_mod._PROC_PRESSURE
        cpu_mod._PROC_PRESSURE = Path('/nonexistent-psi')
        self.addCleanup(setattr, cpu_mod, '_PROC_PRESSURE', original)
        self.assertIsNone(read_psi('cpu'))


class CpuCollectorTest(unittest.TestCase):
    """Frames are produced synchronously (thread loop is trivial waiting)."""

    def _collector(self, pool=(0,)):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name)
        collector = CpuCollector(out, interval=1.0, pool_cpus=pool,
                                 label='test')
        collector.epoch = 1000.0
        return collector, out

    def _frame(self, collector, prev, snap):
        original = cpu_mod.read_psi
        cpu_mod.read_psi = lambda kind='cpu': None
        try:
            return collector._frame(prev, snap)
        finally:
            cpu_mod.read_psi = original

    def test_frame_scopes_and_pool_filter(self):
        collector, _ = self._collector(pool=[0])
        frame = self._frame(collector,
                            {0: (100, 200), 1: (100, 200), 2: (100, 200)},
                            {0: (150, 300), 1: (100, 300), 2: (120, 300)})
        t, scopes, psi = frame
        self.assertEqual(scopes['pool'], 50.0)      # only cpu0 in the pool
        self.assertIn('total', scopes)
        self.assertIsNotNone(scopes['0'])
        self.assertEqual(psi, {})

    def test_write_outputs_series_and_csv(self):
        collector, out = self._collector(pool=[0])
        with collector._lock:
            collector._frames.append((0.5, {'pool': 50.0, 'total': 25.0,
                                            '2': 10.0}, {}))
            collector._frames.append((1.5, {'pool': 60.0, 'total': 30.0,
                                            '2': 12.0}, {}))
        series = collector.write()
        self.assertEqual(series['pool'], [[0.5, 50.0], [1.5, 60.0]])
        self.assertEqual(series['2'], [[0.5, 10.0], [1.5, 12.0]])
        import json
        doc = json.loads((out / 'cpu-series.json').read_text())
        self.assertEqual(doc['pool_cpus'], [0])
        self.assertEqual(doc['interval_s'], 1.0)
        self.assertIn('psi', doc)
        csv_text = (out / 'cpu-test.csv').read_text(encoding='utf-8-sig')
        self.assertEqual(csv_text.splitlines()[0], 't_s,scope,busy_pct')
        self.assertEqual(len(csv_text.splitlines()), 7)   # header + 6 rows


if __name__ == '__main__':
    unittest.main()
