import argparse
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from bwx.commands.curves import (_active_window, _load_bw, _load_cpu,
                                 _merged_grid, _run_group, _stats, run)


def _group_dir():
    tmp = tempfile.TemporaryDirectory()
    unittest.TestCase().addCleanup  # noqa: B010 - placeholder
    return Path(tmp.name), tmp


def _write_group(base, *, cpu=None, bw=None, result=None):
    """cpu: {'epoch', 'interval', 'pool': [(t, v)]}; bw: {'epoch', series}."""
    if cpu is not None:
        doc = {'epoch_wall': cpu.get('epoch'),
               'interval_s': cpu.get('interval', 1.0),
               'pool_cpus': [38],
               'series': {'pool': cpu['series']['pool']}, 'psi': {}}
        (base / 'cpu-series.json').write_text(json.dumps(doc))
    if bw is not None:
        (base / 'bandwidth-series.json').write_text(json.dumps(
            {'epoch_wall': bw.get('epoch'), 'series': bw['series']}))
    if result is not None:
        (base / 'result.json').write_text(json.dumps(result))


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_load_cpu(self):
        _write_group(self.base, cpu={'epoch': 100.0,
                                     'series': {'pool': [[1, 50.0]]}})
        cpu = _load_cpu(self.base / 'cpu-series.json')
        self.assertEqual(cpu['epoch'], 100.0)
        self.assertEqual(cpu['series']['pool'], [[1, 50.0]])

    def test_load_bw_accepts_legacy_layout(self):
        (self.base / 'bandwidth-series.json').write_text(
            json.dumps({'2': [[0, 1.0, 2.0]], 'all': [[0, 1.0, 2.0]]}))
        bw = _load_bw(self.base / 'bandwidth-series.json')
        self.assertIsNone(bw['epoch'])
        self.assertEqual(bw['series']['2'], [[0, 1.0, 2.0]])

    def test_active_window_from_records(self):
        result = {'real': [{'t_launch_wall': 100.0, 't_end_wall': 110.0}],
                  'filler': [{'t_launch_wall': 102.0, 't_end_wall': 109.0}]}
        _write_group(self.base, result=result)
        self.assertEqual(_active_window(self.base), (100.0, 110.0))

    def test_active_window_none_without_anchors(self):
        _write_group(self.base, result={'real': [{'status': 'completed'}]})
        self.assertIsNone(_active_window(self.base))


class StatsAndGridTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_stats_window_filters_and_averages(self):
        cpu = {'epoch': 1000.0, 'interval': 1.0,
               'series': {'pool': [[1, 40.0], [2, 60.0], [3, 20.0]]}}
        bw = {'epoch': 1000.0, 'series': {'2': [[1, 3.0, 1.0], [2, 5.0, 1.0],
                                                [3, 1.0, 1.0]]}}
        _write_group(self.base, cpu=cpu, bw=bw,
                     result={'real': [{'t_launch_wall': 1001.5,
                                       't_end_wall': 1002.5}]})
        rows = _stats(_load_cpu(self.base / 'cpu-series.json'),
                      _load_bw(self.base / 'bandwidth-series.json'),
                      _active_window(self.base))
        by_scope = {r['scope']: r for r in rows}
        # active window [1001.5, 1002.5]: only t=1002 (cpu) and t=2 (bw) count
        self.assertEqual(by_scope['pool busy%']['mean'], 60.0)
        self.assertEqual(by_scope['pool busy%']['frames'], 1)
        self.assertEqual(by_scope['node2 rd+wr GB/s']['mean'], 6.0)

    def test_stats_full_window(self):
        cpu = {'epoch': 0.0, 'interval': 1.0,
               'series': {'pool': [[1, 40.0], [2, 60.0]]}}
        _write_group(self.base, cpu=cpu)
        rows = _stats(_load_cpu(self.base / 'cpu-series.json'), None, None)
        self.assertEqual(rows[0]['mean'], 50.0)
        self.assertEqual(rows[0]['frames'], 2)

    def test_merged_grid_buckets_and_columns(self):
        cpu = {'epoch': 1000.0, 'interval': 1.0,
               'series': {'pool': [[1, 10.0], [2, 30.0]]}}
        bw = {'epoch': 1002.0, 'series': {'2': [[1, 4.0, 2.0]]}}
        columns, rows = _merged_grid(cpu, bw, 1.0, None)
        self.assertEqual(columns, ['t_s', 'pool_busy_pct', '2_rd_gbs',
                                   '2_wr_gbs', 'all_rd_gbs', 'all_wr_gbs'])
        by_t = {row['t_s']: row for row in rows}
        # t0 = cpu wall 1001; buckets are relative: cpu(1001)->0, (1002)->1,
        # bw t=1 with its own epoch -> wall 1003 -> bucket 2
        self.assertEqual(by_t[0]['pool_busy_pct'], 10.0)
        self.assertEqual(by_t[1]['pool_busy_pct'], 30.0)
        self.assertEqual(by_t[2]['2_rd_gbs'], 4.0)
        self.assertEqual(by_t[2]['pool_busy_pct'], '')

    def test_merged_grid_window_filters(self):
        cpu = {'epoch': 0.0, 'interval': 1.0,
               'series': {'pool': [[1, 10.0], [5, 50.0]]}}
        columns, rows = _merged_grid(cpu, None, 1.0, (1.5, 4.5))
        self.assertEqual([row['t_s'] for row in rows], [])

    def test_missing_epoch_aligns_at_zero(self):
        cpu = {'epoch': None, 'interval': 1.0,
               'series': {'pool': [[1, 10.0]]}}
        bw = {'epoch': None, 'series': {'2': [[1, 4.0, 2.0]]}}
        columns, rows = _merged_grid(cpu, bw, 1.0, None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['t_s'], 0)   # t relative to first sample


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def _args(self, **kwargs):
        base = dict(sweep=self.base, grid=None, full_window=False,
                    save_merged=None, save_stats=None, plot=None)
        base.update(kwargs)
        return argparse.Namespace(**base)

    def test_group_mode_prints_and_saves(self):
        _write_group(self.base,
                     cpu={'epoch': 0.0,
                          'series': {'pool': [[1, 40.0], [2, 60.0]]}},
                     bw={'epoch': 0.0, 'series': {'2': [[1, 3.0, 1.0]]}},
                     result={'real': [{'t_launch_wall': 1.0,
                                       't_end_wall': 2.0}]})
        merged = self.base / 'merged.csv'
        import io
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(run(self._args(save_merged=merged)), 0)
        text = out.getvalue()
        self.assertIn('pool busy%', text)
        self.assertIn('active window', text)
        self.assertIn('wrote %s' % merged, text)
        lines = merged.read_text(encoding='utf-8-sig').splitlines()
        self.assertEqual(lines[0], 't_s,pool_busy_pct,2_rd_gbs,2_wr_gbs,'
                                   'all_rd_gbs,all_wr_gbs')

    def test_sweep_root_mode_tables_levels(self):
        level = self.base / 'n4'
        group = level / 'g00'
        group.mkdir(parents=True)
        (self.base / 'run.json').write_text(json.dumps(
            {'cases': ['a'], 'levels': [4], 'run_id': 'r'}))
        _write_group(group,
                     cpu={'epoch': 0.0, 'series': {'pool': [[1, 80.0]]}},
                     bw={'epoch': 0.0, 'series': {'2': [[1, 3.0, 1.0]]}})
        import io
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(run(self._args(sweep=self.base)), 0)
        text = out.getvalue()
        self.assertIn('level', text)
        self.assertIn('pool_busy_mean', text)
        self.assertIn('80.0', text)

    def test_plot_group_renders_png(self):
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            self.skipTest('matplotlib not installed')
        _write_group(self.base,
                     cpu={'epoch': 0.0,
                          'series': {'pool': [[1, 40.0], [2, 60.0]]}},
                     bw={'epoch': 0.0, 'series': {'2': [[1, 3.0, 1.0]]}},
                     result={'real': [{'t_launch_wall': 1.0,
                                       't_end_wall': 2.0}]})
        target = self.base / 'plot.png'
        import io
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(run(self._args(plot=target)), 0)
        self.assertTrue(target.is_file())
        self.assertGreater(target.stat().st_size, 1000)
        self.assertIn('wrote %s' % target, out.getvalue())

    def test_plot_without_samples_errors(self):
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            self.skipTest('matplotlib not installed')
        _write_group(self.base, cpu={'epoch': 0.0, 'series': {'pool': []}})
        import io
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(Exception):
                run(self._args(plot=self.base / 'plot.png'))

    def test_sweep_root_rejects_save_merged(self):
        (self.base / 'run.json').write_text(json.dumps({'cases': [], 'levels': []}))
        import io
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(Exception):
                run(self._args(sweep=self.base, save_merged=self.base / 'm.csv'))

    def test_rejects_unknown_dir(self):
        import io
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(Exception):
                run(self._args(sweep=self.base / 'nowhere'))


if __name__ == '__main__':
    unittest.main()
