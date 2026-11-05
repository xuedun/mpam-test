"""`bwx slice` -- cut a sub-matrix out of a linearity sweep's result CSVs.

Reads <out>/linearity.csv (seconds) or <out>/linearity_ratio.csv (relative
to the case's lowest level with data) and slices it:

* columns: `--levels` uses the same inclusive slice syntax as the sweep
  (`8:48:8` = 8,16,24,32,40,48; integers and slices mix). Levels missing
  from the CSV are warned and skipped, so a coarse re-slicing of a dense
  sweep (`--levels 1:48:1`) just works.
* rows: `--cases a,b` keeps exact ids; `--top N` keeps the N rows with the
  highest value at the highest selected level (find the slowest-scaling
  cases first). Both default to every row.

Prints an aligned table by default; `--save FILE` additionally writes the
sub-matrix as CSV (same layout as linearity.csv, so it can be fed back
into other tools).

    sudo ./bwx slice --out logs/linearity/run1 --levels 8:48:8 --top 10
"""
import argparse
import csv
from pathlib import Path

from ..util import Error, write_csv
from .linearity import _parse_levels

COMMAND = 'slice'
HELP = 'slice levels (columns) and cases (rows) out of the linearity CSVs'

_METRICS = ('seconds', 'ratio')


def add_arguments(parser):
    parser.add_argument('--sweep', type=Path, metavar='PATH', required=True,
                        help='INPUT: a sweep dir (reads its linearity*.csv, '
                             '--metric picks which) or a csv file directly '
                             '(e.g. one written by --save)')
    parser.add_argument('--levels', metavar='EXPR',
                        help='columns to keep: integers and inclusive '
                             'slices start:stop[:step] mix freely '
                             '(e.g. "1,8:48:8"); default: every column')
    parser.add_argument('--cases', metavar='LIST',
                        help='comma-separated case ids to keep '
                             '(default: all rows)')
    parser.add_argument('--top', type=int, metavar='N',
                        help='keep the N rows with the highest value at the '
                             'highest selected level (default: all rows)')
    parser.add_argument('--metric', choices=_METRICS, default='seconds',
                        help='which matrix: seconds (linearity.csv, default) '
                             'or ratio (linearity_ratio.csv)')
    parser.add_argument('--save', type=Path, metavar='FILE',
                        help='write the sub-matrix to this CSV in addition '
                             'to printing it')


def run(args):
    if args.cases is not None and args.top is not None:
        raise Error('--cases and --top are mutually exclusive')
    path = _source_csv(args.sweep, args.metric)

    rows = _read_matrix(path)
    columns = _select_columns(rows, args.levels)
    rows = _select_rows(rows, args.cases, args.top, columns)
    if not rows:
        raise Error('no rows left after selection')

    _print_table(rows, columns, args.metric)
    if args.save is not None:
        write_csv(args.save,
                  [_matrix_row(row, columns) for row in rows],
                  ['case'] + ['n%d' % level for level in columns])
        print('wrote %s (%d row(s) x %d level(s))'
              % (args.save, len(rows), len(columns)))
    return 0


def _source_csv(source, metric):
    """--sweep accepts a sweep directory or a csv file path directly."""
    if source.is_file():
        return source
    if source.is_dir():
        path = source / ('linearity.csv' if metric == 'seconds'
                         else 'linearity_ratio.csv')
        if path.is_file():
            return path
        raise Error('%s not found (run `bwx linearity --out %s '
                    '--summarize-only` first?)' % (path, source))
    raise Error('%s does not exist (want a sweep dir or a csv file)'
                % source)


def _read_matrix(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return [row for row in csv.DictReader(stream) if row.get('case')]


def _available_levels(rows):
    levels = []
    for key in rows[0] if rows else []:
        if key != 'case' and key.startswith('n'):
            try:
                levels.append(int(key[1:]))
            except ValueError:
                continue
    return sorted(levels)


def _select_columns(rows, expr):
    """Intersect the wanted levels with the ones the CSV actually has."""
    available = _available_levels(rows)
    if not available:
        raise Error('no n<level> columns found in the csv')
    wanted = _parse_levels(expr, default=None) if expr else available
    selected = [level for level in wanted if level in available]
    missing = [level for level in wanted if level not in available]
    if missing:
        shown = ','.join(str(level) for level in missing[:8])
        if len(missing) > 8:
            shown += ',... (%d more)' % (len(missing) - 8)
        print('warning: level(s) %s not in the csv; available: %s'
              % (shown, ','.join(str(l) for l in available)))
    if not selected:
        raise Error('none of the requested levels exist in the csv '
                    '(available: %s)'
                    % ','.join(str(l) for l in available))
    return selected


def _select_rows(rows, cases, top, columns):
    if cases is not None:
        wanted = [part.strip() for part in cases.split(',') if part.strip()]
        by_id = {row['case']: row for row in rows}
        missing = [case for case in wanted if case not in by_id]
        if missing:
            print('warning: case(s) %s not in the csv; %d row(s) available'
                  % (','.join(missing), len(rows)))
        return [by_id[case] for case in wanted if case in by_id]
    if top is not None and top > 0:
        pivot = 'n%d' % columns[-1]
        def sort_key(row):
            value = row.get(pivot, '')
            try:
                return (0, -float(value)) if value else (1, 0.0)
            except ValueError:
                return (1, 0.0)
        return sorted(rows, key=sort_key)[:top]
    return rows


def _matrix_row(row, columns):
    out = {'case': row['case']}
    for level in columns:
        out['n%d' % level] = row.get('n%d' % level, '')
    return out


def _print_table(rows, columns, metric):
    name_width = max([len('case')] + [len(r['case']) for r in rows])
    header = ('%-*s' % (name_width, 'case')
              + ''.join('%10s' % ('n%d' % level) for level in columns))
    print('%s  (%s)' % (header, 'seconds' if metric == 'seconds'
                        else 'x lowest level'))
    for row in rows:
        line = '%-*s' % (name_width, row['case'])
        for level in columns:
            value = row.get('n%d' % level, '')
            try:
                line += '%10s' % ('%.3f' % float(value)) if value else '%10s' % '-'
            except ValueError:
                line += '%10s' % '-'
        print(line)
