import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from bwx.commands.slice import (_available_levels, _matrix_row,
                                _read_matrix, _select_columns, _select_rows,
                                run)


def _row(case, levels):
    row = {'case': case}
    row.update(('n%d' % level, '%.3f' % value) for level, value in levels.items())
    return row


class ColumnSelectionTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(self.base))
        self.rows = [
            _row('case-a', {1: 1.0, 2: 1.1, 4: 1.4, 8: 2.0, 16: 3.0, 48: 9.0}),
            _row('case-b', {1: 4.0, 2: 4.2, 4: 4.8, 8: 6.0, 16: 8.0, 48: 20.0}),
        ]
        (self.base / 'linearity.csv').write_text(
            'case,n1,n2,n4,n8,n16,n48\ncase-a,1.000,1.100,1.400,2.000,3.000,9.000\n'
            'case-b,4.000,4.200,4.800,6.000,8.000,20.000\n', encoding='utf-8-sig')

    def test_available_levels(self):
        rows = _read_matrix(self.base / 'linearity.csv')
        self.assertEqual(_available_levels(rows), [1, 2, 4, 8, 16, 48])

    def test_all_columns_by_default(self):
        rows = _read_matrix(self.base / 'linearity.csv')
        self.assertEqual(_select_columns(rows, None), [1, 2, 4, 8, 16, 48])

    def test_slice_selection(self):
        rows = _read_matrix(self.base / 'linearity.csv')
        # '4:8:4' = 4,8 (both exist); '1,16' picks singles.
        self.assertEqual(_select_columns(rows, '4:8:4'), [4, 8])
        self.assertEqual(_select_columns(rows, '1,16'), [1, 16])

    def test_missing_levels_warned_and_kept_silent_union(self):
        rows = _read_matrix(self.base / 'linearity.csv')
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(_select_columns(rows, '8:48:8'), [8, 16, 48])
        self.assertIn('warning', out.getvalue())
        self.assertIn('24', out.getvalue())

    def test_all_missing_rejected(self):
        rows = _read_matrix(self.base / 'linearity.csv')
        with self.assertRaises(Exception):
            _select_columns(rows, '100:200')


class RowSelectionTest(unittest.TestCase):
    def setUp(self):
        self.rows = [
            _row('case-a', {1: 1.0, 8: 2.0, 16: 3.0}),
            _row('case-b', {1: 4.0, 8: 6.0, 16: 8.0}),
            _row('case-c', {1: 2.0, 8: 2.4, 16: 2.8}),
        ]

    def test_keep_exact_ids(self):
        kept = _select_rows(self.rows, 'case-b,case-c', None, [1, 8, 16])
        self.assertEqual([r['case'] for r in kept], ['case-b', 'case-c'])

    def test_unknown_id_warned(self):
        out = io.StringIO()
        with redirect_stdout(out):
            kept = _select_rows(self.rows, 'case-a,nope', None, [1, 8, 16])
        self.assertEqual([r['case'] for r in kept], ['case-a'])
        self.assertIn('nope', out.getvalue())

    def test_top_by_highest_level(self):
        kept = _select_rows(self.rows, None, 2, [1, 8, 16])
        self.assertEqual([r['case'] for r in kept], ['case-b', 'case-a'])

    def test_top_empty_cells_sort_last(self):
        rows = [_row('empty', {1: 9.0}),   # no n16
                _row('case-b', {1: 4.0, 16: 8.0})]
        kept = _select_rows(rows, None, 2, [1, 16])
        self.assertEqual([r['case'] for r in kept], ['case-b', 'empty'])

    def test_matrix_row_keeps_only_selected(self):
        row = _matrix_row(self.rows[0], [1, 16])
        self.assertEqual(row, {'case': 'case-a', 'n1': '1.000', 'n16': '3.000'})


class RunTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(self.base))
        (self.base / 'linearity.csv').write_text(
            'case,n1,n8,n16\ncase-a,1.000,2.000,3.000\ncase-b,4.000,6.000,8.000\n',
            encoding='utf-8-sig')

    def _args(self, **kwargs):
        base = dict(out=self.base, levels=None, cases=None, top=None,
                    metric='seconds', save=None)
        base.update(kwargs)
        return argparse.Namespace(**base)

    def test_prints_table(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(run(self._args(levels='8:16')), 0)
        text = out.getvalue()
        header = text.splitlines()[1].split()
        self.assertIn('case', header)
        self.assertIn('n8', header)
        self.assertIn('n16', header)
        self.assertNotIn('n1', header)
        self.assertIn('2.000', text)

    def test_save_writes_submatrix(self):
        target = self.base / 'sub.csv'
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(run(self._args(levels='8:16', cases='case-b',
                                            save=target)), 0)
        rows = _read_matrix(target)
        self.assertEqual([r['case'] for r in rows], ['case-b'])
        self.assertEqual(rows[0]['n8'], '6.000')
        self.assertNotIn('n1', rows[0])

    def test_missing_sweep_dir_rejected(self):
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(Exception):
                run(self._args(out=self.base / 'nowhere'))

    def test_cases_and_top_are_exclusive(self):
        with self.assertRaises(Exception):
            run(self._args(cases='case-a', top=2))


if __name__ == '__main__':
    unittest.main()
