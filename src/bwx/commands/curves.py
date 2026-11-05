"""`bwx curves` -- unified CPU-utilization / memory-bandwidth curves.

Turns a finished run's cpu-series.json + bandwidth-series.json into the
two things an experiment report needs:

1. **one merged, wall-clock-aligned time series**: pool busy% next to
   per-node ddrc read/write GB/s on a shared second-bucket grid
   (`--save-merged FILE` writes it as CSV; the terminal gets a compact
   sparkline preview per scope);
2. **global averages**: mean/peak over the *active* window (while VMs
   were actually executing -- the wall-clock anchors in result.json
   exclude the serial boot/prepare tail; `--full-window` overrides),
   falling back to the whole series when no anchors exist.

`--sweep` accepts either a single run/group dir (contains
cpu-series.json) or a linearity sweep root (contains run.json): for a
sweep root the same stats are aggregated per level and printed as a
table (`--save-stats` writes it).

Older runs whose series predate `epoch_wall` are aligned at t=0 with a
warning -- the real offset is below the 1s sample granularity anyway.
"""
import argparse
import json
from pathlib import Path

from ..util import Error, write_csv, write_json

COMMAND = 'curves'
HELP = 'merged CPU-utilization / memory-bandwidth curves and averages'

_SPARK = '▁▂▃▄▅▆▇█'


def add_arguments(parser):
    parser.add_argument('--sweep', type=Path, metavar='PATH', required=True,
                        help='a run/group dir (cpu-series.json) or a '
                             'linearity sweep root (run.json)')
    parser.add_argument('--grid', type=float, metavar='SEC', default=None,
                        help='bucket size of the merged series (default: '
                             'the collectors\' interval)')
    parser.add_argument('--full-window', action='store_true',
                        help='average over the whole series, not the '
                             'active (VMs executing) window')
    parser.add_argument('--save-merged', type=Path, metavar='FILE',
                        help='write the merged time-grid CSV (single-dir '
                             'mode only)')
    parser.add_argument('--save-stats', type=Path, metavar='FILE',
                        help='write the stats table as CSV')


def run(args):
    path = args.sweep
    if (path / 'run.json').is_file():
        _run_sweep_root(path, args)
    elif (path / 'cpu-series.json').is_file():
        _run_group(path, args)
    else:
        raise Error('%s: no run.json (sweep root) and no cpu-series.json '
                    '(run dir) -- not a curves input' % path)
    return 0


# ------------------------------------------------------------- single group
def _run_group(group_dir, args):
    cpu = _load_cpu(group_dir / 'cpu-series.json')
    bw = _load_bw(group_dir / 'bandwidth-series.json')
    if cpu is None and bw is None:
        raise Error('%s: neither cpu-series.json nor bandwidth-series.json'
                    % group_dir)
    window = None if args.full_window else _active_window(group_dir)

    grid = args.grid or (cpu or {}).get('interval') or 1.0
    stats = _stats(cpu, bw, window)
    _print_stats(group_dir, stats, window)
    _print_sparklines(cpu, bw, window)

    if args.save_merged is not None:
        columns, rows = _merged_grid(cpu, bw, grid, window)
        write_csv(args.save_merged, rows, columns)
        print('wrote %s (%d bucket(s), grid=%ss)'
              % (args.save_merged, len(rows), grid))
    if args.save_stats is not None:
        write_csv(args.save_stats, stats, list(stats[0]) if stats else [])
    return 0


def _load_cpu(path):
    """{'epoch', 'interval', 'series'} or None."""
    if not path.is_file():
        return None
    doc = json.loads(path.read_text())
    series = doc.get('series') or {}
    if not series:
        return None
    return {'epoch': doc.get('epoch_wall'), 'interval': doc.get('interval_s'),
            'series': series}


def _load_bw(path):
    """{'epoch', 'series'} or None; accepts the pre-epoch layout too."""
    if not path.is_file():
        return None
    doc = json.loads(path.read_text())
    if 'series' in doc:
        series = doc['series']
        epoch = doc.get('epoch_wall')
    else:                                  # legacy: node keys at the top level
        series = {key: value for key, value in doc.items()
                  if key != 'epoch_wall'}
        epoch = doc.get('epoch_wall')
    if not series:
        return None
    return {'epoch': epoch, 'series': series}


def _active_window(group_dir):
    """[start_wall, end_wall] while VMs (real+filler) were executing, or None."""
    path = group_dir / 'result.json'
    if not path.is_file():
        return None
    doc = json.loads(path.read_text())
    launches, ends = [], []
    for record in (doc.get('real') or []) + (doc.get('filler') or []):
        if isinstance(record.get('t_launch_wall'), (int, float)):
            launches.append(record['t_launch_wall'])
        if isinstance(record.get('t_end_wall'), (int, float)):
            ends.append(record['t_end_wall'])
    if not launches or not ends:
        return None
    start = min(launches)
    end = max(ends)
    return (start, end) if end > start else None


def _in_window(t_wall, window):
    return window is None or (window[0] <= t_wall <= window[1])


def _stats(cpu, bw, window):
    """Rows of (scope, metric, mean, peak, frames) for the report table."""
    rows = []
    if cpu:
        points = [(t + (cpu['epoch'] or 0.0), v)
                  for t, v in cpu['series'].get('pool') or []
                  if _in_window(t + (cpu['epoch'] or 0.0), window)]
        if points:
            values = [v for _, v in points]
            rows.append({'scope': 'pool busy%', 'mean': _mean(values),
                         'peak': max(values), 'frames': len(values)})
    if bw:
        for key in sorted(bw['series']):
            if key == 'all':
                label = 'all rd+wr'
            else:
                label = 'node%s rd+wr' % key
            points = [(t + (bw['epoch'] or 0.0), rd, wr)
                      for t, rd, wr in bw['series'].get(key) or []
                      if _in_window(t + (bw['epoch'] or 0.0), window)]
            if points:
                totals = [rd + wr for _, rd, wr in points]
                rows.append({'scope': '%s GB/s' % label,
                             'mean': _mean(totals),
                             'peak': max(totals),
                             'frames': len(points)})
    return rows


def _mean(values):
    return round(sum(values) / len(values), 2)


def _merged_grid(cpu, bw, grid, window):
    """Bucket both series onto one shared wall-clock grid, t relative to
    the first sample.

    Returns (column names, row dicts); empty buckets are blank cells so
    plotting tools can interpolate.
    """
    def wall_points(series, epoch, selector):
        return [(t + (epoch or 0.0), *selector(row))
                for t, *row in series or []]

    cpu_points = []
    if cpu:
        cpu_points = wall_points(cpu['series'].get('pool'), cpu['epoch'],
                                 lambda row: (row[0],))
    bw_points = {}
    if bw:
        for key, points in bw['series'].items():
            bw_points[key] = wall_points(points, bw['epoch'],
                                         lambda row: (row[0], row[1]))
    anchors = [p[0] for p in cpu_points]
    for points in bw_points.values():
        anchors.extend(p[0] for p in points)
    if not anchors:
        return ['t_s', 'pool_busy_pct'], []
    t0 = min(anchors)

    buckets = {}
    for wall, value in cpu_points:
        if not _in_window(wall, window):
            continue
        buckets.setdefault(int((wall - t0) / grid), {})['pool_busy_pct'] = value
    nodes = []
    for key, points in sorted(bw_points.items()):
        rd_col = ('%s_rd_gbs' % key if key != 'all' else 'all_rd_gbs')
        wr_col = ('%s_wr_gbs' % key if key != 'all' else 'all_wr_gbs')
        if key != 'all':
            nodes.append(key)
        for wall, rd, wr in points:
            if not _in_window(wall, window):
                continue
            cell = buckets.setdefault(int((wall - t0) / grid), {})
            cell.setdefault(rd_col, []).append(rd)
            cell.setdefault(wr_col, []).append(wr)
    columns = ['t_s', 'pool_busy_pct']
    for node in sorted(nodes):
        columns += ['%s_rd_gbs' % node, '%s_wr_gbs' % node]
    columns += ['all_rd_gbs', 'all_wr_gbs']
    rows = []
    for index in sorted(buckets):
        cell = buckets[index]
        row = {'t_s': index * grid}
        for column in columns[1:]:
            value = cell.get(column)
            if isinstance(value, list):        # average samples in a bucket
                value = round(sum(value) / len(value), 3)
            row[column] = value if value is not None else ''
        rows.append(row)
    return columns, rows


def _print_stats(group_dir, rows, window):
    label = ('active window %.1fs (boot/prepare excluded)'
             % (window[1] - window[0]) if window
             else 'full series (no wall-clock anchors)')
    print('\n%s  [%s]' % (group_dir, label))
    print('%-22s %10s %10s %8s' % ('scope', 'mean', 'peak', 'frames'))
    for row in rows:
        print('%-22s %10s %10s %8d'
              % (row['scope'], row['mean'], row['peak'], row['frames']))


def _print_sparklines(cpu, bw, window):
    """One sparkline per major scope (pool busy, each node's rd+wr)."""
    def spark(points):
        if not points:
            return ''
        values = [v for _, v in points]
        low, high = min(values), max(values)
        span = (high - low) or 1.0
        return ''.join(_SPARK[min(7, int((v - low) / span * 7))]
                       for v in values)

    lines = []
    if cpu:
        points = [(t + (cpu['epoch'] or 0.0), v)
                  for t, v in cpu['series'].get('pool') or []
                  if _in_window(t + (cpu['epoch'] or 0.0), window)]
        if points:
            lines.append(('pool busy%%   %s (%d frames)'
                          % (spark(points), len(points))))
    if bw:
        epoch = bw['epoch'] or 0.0
        for key in sorted(bw['series']):
            points = [(t + epoch, rd + wr)
                      for t, rd, wr in bw['series'].get(key) or []
                      if _in_window(t + epoch, window)]
            if points:
                lines.append(('%-11s GB/s %s'
                              % ('node%s' % key if key != 'all' else 'all',
                                 spark(points))))
    if lines:
        print('\n' + '\n'.join(lines))


# ------------------------------------------------------------- sweep root
def _run_sweep_root(root, args):
    rows = []
    for level_dir in sorted((d for d in root.glob('n*') if d.is_dir()),
                            key=lambda d: int(d.name[1:])):
        group_dirs = [d for d in sorted(level_dir.glob('g*'))
                      if (d / 'cpu-series.json').is_file()
                      or (d / 'bandwidth-series.json').is_file()]
        if not group_dirs:
            continue
        pool_means, pool_peaks = [], []
        node_stats = {}
        for group in group_dirs:
            cpu = _load_cpu(group / 'cpu-series.json')
            bw = _load_bw(group / 'bandwidth-series.json')
            window = None if args.full_window else _active_window(group)
            for row in _stats(cpu, bw, window):
                if row['scope'] == 'pool busy%':
                    pool_means.append(row['mean'])
                    pool_peaks.append(row['peak'])
                else:
                    node_stats.setdefault(row['scope'], ([], []))
                    node_stats[row['scope']][0].append(row['mean'])
                    node_stats[row['scope']][1].append(row['peak'])
        entry = {'level': int(level_dir.name[1:]), 'groups': len(group_dirs)}
        if pool_means:
            entry['pool_busy_mean'] = _mean(pool_means)
            entry['pool_busy_peak'] = round(max(pool_peaks), 2)
        for scope, (means, peaks) in sorted(node_stats.items()):
            column = scope.split()[0] if scope != 'pool busy%' else 'pool'
            entry['%s_mean' % column] = _mean(means)
            entry['%s_peak' % column] = round(max(peaks), 2)
        rows.append(entry)

    if not rows:
        raise Error('%s: no n<level>/g*/ with series files' % root)
    order = {'level': 0, 'groups': 1, 'pool_busy_mean': 2, 'pool_busy_peak': 3}
    columns = sorted({key for row in rows for key in row},
                     key=lambda k: (order.get(k, 10), k))
    print('\n%s  (per level, active window unless --full-window)' % root)
    header = '  '.join('%14s' % key for key in columns)
    print(header)
    for row in rows:
        print('  '.join('%14s' % row.get(key, '-') for key in columns))
    if args.save_stats is not None:
        write_csv(args.save_stats, rows, columns)
        print('wrote %s (%d level(s))' % (args.save_stats, len(rows)))
    return 0
