import json
import tempfile
import unittest
from pathlib import Path

from bwx.commands import analyze


class FakeSweep:
    """Build a small sweep CSV and check the analysis end to end (offline)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.out = self.dir / 'analyze'
        self.out.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_sweep(self, rows):
        import csv
        path = self.dir / 'sweep.csv'
        fields = ['case', 'language', 'level_applied', 'concurrency', 'status',
                  'actions_s', 'case_wall_s', 'mean_gbs']
        with path.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)
        return path

    def _args(self, **kwargs):
        import argparse
        parser = argparse.ArgumentParser()
        analyze.add_arguments(parser)
        defaults = dict(sweep=[], alloc=None, out=self.out, tolerance=5.0,
                        hungry_threshold=15.0, medium_threshold=3.0,
                        emit_demand=None)
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_hungry_classification_and_knee(self):
        rows = []
        # Baseline and four levels: degradation grows as the cap tightens.
        for level, wall in (('full', 100.0), ('97', 101.0), ('54', 104.0),
                            ('26', 110.0), ('9', 130.0)):
            rows.append({'case': 'hungry', 'language': 'go', 'level_applied': level,
                         'concurrency': 1, 'status': 'completed', 'actions_s': wall,
                         'case_wall_s': wall + 5, 'mean_gbs': 2.0})
        path = self._write_sweep(rows)
        analyze._analyze_sweep(self._args(sweep=[path]), self.out, quiet=True)
        classes = self._read_csv('case-classes.csv')
        self.assertEqual(len(classes), 1)
        row = classes[0]
        self.assertEqual(row['class'], 'hungry', '30% degradation at level 9')
        self.assertEqual(row['min_acceptable_level'], '26',
                         '26 is the tightest cap within the 5% tolerance')

    def test_light_classification(self):
        rows = []
        for level, wall in (('full', 100.0), ('9', 100.8)):
            rows.append({'case': 'light', 'language': 'ts', 'level_applied': level,
                         'concurrency': 1, 'status': 'completed', 'actions_s': wall,
                         'case_wall_s': wall, 'mean_gbs': 1.5})
        path = self._write_sweep(rows)
        analyze._analyze_sweep(self._args(sweep=[path]), self.out, quiet=True)
        row = self._read_csv('case-classes.csv')[0]
        self.assertEqual(row['class'], 'light')
        self.assertEqual(row['min_acceptable_level'], '9',
                         'even the tightest cap is acceptable')

    def test_unverified_and_failed_runs_excluded(self):
        rows = [
            {'case': 'x', 'language': 'go', 'level_applied': 'full',
             'concurrency': 1, 'status': 'completed', 'actions_s': 10.0,
             'case_wall_s': 10.0, 'mean_gbs': 1},
            {'case': 'x', 'language': 'go', 'level_applied': '9',
             'concurrency': 1, 'status': 'guest_failed', 'actions_s': 0.1,
             'case_wall_s': 0.1, 'mean_gbs': 1},
            {'case': 'x', 'language': 'go', 'level_applied': '9',
             'concurrency': 1, 'status': 'completed', 'actions_s': 12.0,
             'case_wall_s': 12.0, 'mean_gbs': 1},
        ]
        path = self._write_sweep(rows)
        analyze._analyze_sweep(self._args(sweep=[path]), self.out, quiet=True)
        sensitivity = self._read_csv('sensitivity.csv')
        level9 = [row for row in sensitivity if row['level_applied'] == '9']
        self.assertEqual(level9[0]['runs'], '1', 'the failed run must not count')
        self.assertAlmostEqual(float(level9[0]['degradation_pct']), 20.0)

    def _read_csv(self, name):
        import csv
        with (self.out / name).open(encoding='utf-8-sig', newline='') as stream:
            return list(csv.DictReader(stream))


class NStarTest(unittest.TestCase):
    def _by_concurrency(self, case, levels):
        return {(case, level, conc): values for (case, level, conc), values in levels.items()}

    def test_no_concurrency_data_returns_none(self):
        data = {('c', 'full', 1): [10.0]}
        self.assertIsNone(analyze._n_star('c', self._by_concurrency('c', data),
                                          _args(tolerance=5.0)))

    def test_first_degrading_concurrency(self):
        data = {('c', 'full', 1): [10.0, 10.1],
                ('c', 'full', 2): [10.3],
                ('c', 'full', 4): [11.5],
                ('c', 'full', 8): [20.0]}
        self.assertEqual(analyze._n_star('c', self._by_concurrency('c', data),
                                         _args(tolerance=5.0)), 4)

    def test_flat_case_has_no_n_star(self):
        data = {('c', 'full', 1): [10.0], ('c', 'full', 4): [10.2]}
        self.assertIsNone(analyze._n_star('c', self._by_concurrency('c', data),
                                          _args(tolerance=5.0)))


def _args(**kwargs):
    import argparse
    return argparse.Namespace(**kwargs)


class AllocSummaryTest(unittest.TestCase):
    def test_paired_comparison_excludes_incomplete_batches(self):
        import csv
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'alloc.csv'
            fields = ['strategy', 'mix', 'repeat', 'case', 'status', 'actions_s',
                      'case_wall_s']
            rows = []
            # repeat 0: every strategy complete -> paired
            for strategy, wall in (('none', 10.0), ('shared', 12.0)):
                rows.append({'strategy': strategy, 'mix': 'm', 'repeat': '0',
                             'case': 'c', 'status': 'completed', 'actions_s': wall,
                             'case_wall_s': wall})
            # repeat 1: shared has a crashed VM -> the whole batch is unpaired
            rows.append({'strategy': 'none', 'mix': 'm', 'repeat': '1', 'case': 'c',
                         'status': 'completed', 'actions_s': 10.5, 'case_wall_s': 10.5})
            rows.append({'strategy': 'shared', 'mix': 'm', 'repeat': '1', 'case': 'c',
                         'status': 'vmm_failed', 'actions_s': '', 'case_wall_s': 1})
            with path.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(rows)
            out = Path(tmp) / 'out'
            out.mkdir()
            args = _args(alloc=path, sweep=None, out=out, tolerance=5.0,
                         hungry_threshold=15.0, medium_threshold=3.0, emit_demand=None)
            analyze._analyze_alloc(args, out, quiet=True)
            summary = _read(out / 'alloc-summary.csv')
            shared = [row for row in summary if row['strategy'] == 'shared'][0]
            self.assertEqual(shared['mean_makespan_s'], '12.0',
                             'only the fully-completed paired batch counts')


def _read(path):
    import csv
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


if __name__ == '__main__':
    unittest.main()
