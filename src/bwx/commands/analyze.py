"""`bwx analyze` -- Phase 2/4: turn sweep/alloc records into decisions.

Inputs are the CSVs written by `bwx sweep` and `bwx alloc`. Outputs:

1. **Per-case sensitivity** (from sweep): for each case and applied level, the
   median actions_s and the degradation vs the uncapped baseline. Classification
   into hungry / medium / light by the degradation at the lowest level.
2. **Knee level** per case: the highest level (i.e. the tightest cap) whose
   degradation is still within a tolerance of the baseline. Below the knee the case
   falls off a cliff. This is the number the demand strategy allocates against.
3. **N\\*** per case: with concurrency > 1 cells present, the smallest N at which
   degradation crosses the tolerance. A small N* means the case starts fighting for
   bandwidth even with few neighbours.
4. **Demand table** (`--emit-demand`): the per-case {level, class, knee} that
   `bwx alloc --demand` consumes.
5. **Strategy comparison** (from alloc): makespan / mean actions per strategy, plus
   a paired comparison restricted to mixes where every VM of every strategy
   completed (so a crashed VM cannot flatter a strategy).

Everything is descriptive. With the small repeat counts a sweep realistically gets,
these are medians and shapes, not significance tests, and the output says so.
"""
import json
import statistics
from pathlib import Path

from .. import config as config_mod
from ..util import Error, write_csv, write_json

COMMAND = 'analyze'
HELP = 'Phase 2/4: degradation, classification, knee, N*, strategy comparison'

SENSITIVITY_FIELDS = ['case', 'language', 'level_applied', 'runs', 'median_actions_s',
                      'baseline_actions_s', 'degradation_pct', 'mean_gbs', 'peak_gbs']
CLASS_FIELDS = ['case', 'language', 'class', 'baseline_actions_s', 'knee_level',
                'cliff_level', 'knee_degradation_pct', 'n_star', 'demand_gbs',
                'min_acceptable_level', 'levels_measured']
STRATEGY_FIELDS = ['strategy', 'batches', 'vms_total', 'vms_ok', 'mean_actions_s',
                   'median_actions_s', 'mean_makespan_s', 'pairwise_vs_none_s']


def add_arguments(parser):
    parser.add_argument('--sweep', action='append', type=Path,
                        help='sweep.csv or a sweep output dir (repeatable: pass one '
                             'per concurrency to get N*)')
    parser.add_argument('--alloc', type=Path, help='alloc.csv (or an alloc output dir)')
    parser.add_argument('--out', type=Path, help='output dir (default logs/analyze/<stamp>)')
    parser.add_argument('--tolerance', type=float, default=5.0,
                        help='degradation %% still considered "no real cost" '
                             '(default 5)')
    parser.add_argument('--hungry-threshold', type=float, default=15.0,
                        help='degradation %% at the lowest level that makes a case '
                             '"hungry" (default 15)')
    parser.add_argument('--medium-threshold', type=float, default=3.0,
                        help='lower bound of the "medium" class (default 3)')
    parser.add_argument('--emit-demand', type=Path, metavar='JSON',
                        help='also write the demand table for `bwx alloc --demand`')


def run(args):
    config = config_mod.from_args(args)
    from ..util import utc_stamp
    out = args.out or config.logs_dir / ('analyze/' + utc_stamp())
    out.mkdir(parents=True, exist_ok=True)

    did_something = False
    if args.sweep:
        _analyze_sweep(args, out)
        did_something = True
    if args.alloc:
        _analyze_alloc(args, out)
        did_something = True
    if not did_something:
        raise Error('pass --sweep and/or --alloc')
    print('\nwrote results to %s' % out)
    return 0


def _resolve_csv(path, name):
    """Accept either a .csv or the run directory containing it."""
    path = Path(path)
    if path.is_file():
        return path
    candidate = path / name
    if candidate.is_file():
        return candidate
    raise Error('%s not found (looked for %s and %s)' % (name, path, candidate))


def _read_csv(path):
    import csv
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def _to_float(value):
    try:
        if value in (None, '', 'None'):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _level_key(value):
    """Sort key for applied levels: 'full' last, numbers ascending."""
    if value in ('full', None, ''):
        return (1, 0)
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, 0)


# ------------------------------------------------------------------- sweep
def _analyze_sweep(args, out, *, quiet=False):
    paths = [_resolve_csv(p, 'sweep.csv') for p in args.sweep]
    rows = []
    for path in paths:
        loaded = _read_csv(path)
        if not loaded:
            raise Error('sweep CSV is empty: %s' % path)
        rows.extend(loaded)
    path = paths[0]

    # Median actions_s per (case, level). Prefer actions_s; fall back to case_wall_s.
    # Bandwidth is collected in a parallel dict keyed the same way.
    grouped, bandwidth_map, languages = {}, {}, {}
    # Per (case, level, concurrency) medians, used for N*.
    by_concurrency = {}
    for row in rows:
        if row.get('status') not in ('completed', 'budget_exhausted'):
            continue
        value = _to_float(row.get('actions_s')) or _to_float(row.get('case_wall_s'))
        if value is None:
            continue
        level = row.get('level_applied', '')
        try:
            conc = int(_to_float(row.get('concurrency')) or 1)
        except (TypeError, ValueError):
            conc = 1
        grouped.setdefault((row['case'], level), []).append(value)
        by_concurrency.setdefault((row['case'], level, conc), []).append(value)
        languages[row['case']] = row.get('language', '')
        gbs = _to_float(row.get('mean_gbs'))
        if gbs is not None:
            bandwidth_map.setdefault((row['case'], level), []).append(gbs)

    cases = sorted({case for case, _ in grouped})
    sensitivity = []
    classes = []
    demand = {}
    for case in cases:
        # Baseline: level 'full' if present, else the highest measured level.
        levels = sorted({level for (c, level) in grouped if c == case}, key=_level_key)
        baseline_level = 'full' if 'full' in levels else (levels[-1] if levels else None)
        baseline_values = grouped.get((case, baseline_level), [])
        baseline = statistics.median(baseline_values) if baseline_values else None

        per_level = {}
        for level in levels:
            values = grouped.get((case, level), [])
            if not values:
                continue
            median = statistics.median(values)
            bandwidth = bandwidth_map.get((case, level), [])
            degradation = ((median - baseline) / baseline * 100) if baseline else None
            mean_gbs = statistics.mean(bandwidth) if bandwidth else None
            per_level[level] = {'median': median, 'degradation': degradation,
                                'runs': len(values), 'mean_gbs': mean_gbs}
            sensitivity.append({
                'case': case, 'language': languages.get(case, ''), 'level_applied': level,
                'runs': len(values), 'median_actions_s': round(median, 3),
                'baseline_actions_s': round(baseline, 3) if baseline else None,
                'degradation_pct': round(degradation, 2) if degradation is not None else None,
                'mean_gbs': round(mean_gbs, 3) if mean_gbs is not None else None,
                'peak_gbs': None,
            })

        classification = _classify(case, per_level, baseline, levels, args)
        classification['language'] = languages.get(case, '')
        classification['n_star'] = _n_star(case, by_concurrency, args)
        classes.append(classification)
        demand[case] = {'min_acceptable_level': classification['min_acceptable_level'],
                        'demand_gbs': classification['demand_gbs'],
                        'class': classification['class'],
                        'knee_level': classification['knee_level'],
                        'n_star': classification['n_star']}

    write_csv(out / 'sensitivity.csv', sensitivity, SENSITIVITY_FIELDS)
    write_csv(out / 'case-classes.csv', classes, CLASS_FIELDS)
    if args.emit_demand:
        if not quiet:
            write_json(args.emit_demand, {'tolerance_pct': args.tolerance,
                                      'hungry_threshold_pct': args.hungry_threshold,
                                      'cases': demand})
        print('wrote demand table: %s (%d cases)' % (args.emit_demand, len(demand)))

    print('=== sensitivity (%s) ===' % path.name)
    print('%-40s %6s %6s %10s %10s %8s' % ('case', 'level', 'runs', 'median_s', 'base_s', 'degrad%'))
    for row in sensitivity:
        print('%-40s %6s %6d %10s %10s %8s'
              % (row['case'][:40], row['level_applied'], row['runs'],
                 row['median_actions_s'], row['baseline_actions_s'] or '-',
                 row['degradation_pct'] if row['degradation_pct'] is not None else '-'))
    print('\n=== classes ===')
    print('%-40s %-8s %6s %6s %5s %8s' % ('case', 'class', 'knee', 'cliff', 'N*', 'gbs'))
    for row in classes:
        print('%-40s %-8s %6s %6s %5s %8s'
              % (row['case'][:40], row['class'], row['knee_level'] or '-',
                 row['cliff_level'] or '-', row['n_star'] or '-', row['demand_gbs'] or '-'))
    print('\nDescriptive medians over %d run(s); not a significance test.' % len(sensitivity))


def _classify(case, per_level, baseline, levels, args):
    """Hungry/medium/light + knee level + min acceptable level + demand GB/s."""
    numeric_levels = [level for level in levels if _level_key(level)[0] == 0]
    # Degradation at the tightest cap is the classification signal.
    lowest = min(numeric_levels, key=lambda l: int(l)) if numeric_levels else None
    lowest_degradation = per_level.get(lowest, {}).get('degradation') if lowest else None

    if lowest_degradation is None:
        klass = 'unknown'
    elif lowest_degradation > args.hungry_threshold:
        klass = 'hungry'
    elif lowest_degradation > args.medium_threshold:
        klass = 'medium'
    else:
        klass = 'light'

    # Knee = the TIGHTEST cap whose degradation is still within tolerance. Below it
    # the case falls off a cliff, so this is the level allocation should honour.
    # `cliff_level` is the tightest cap that broke the tolerance, reported for
    # context; it is None when even the tightest measured cap was acceptable.
    acceptable = [(int(level), per_level[level])
                  for level in numeric_levels
                  if per_level.get(level, {}).get('degradation') is not None
                  and per_level[level]['degradation'] <= args.tolerance]
    broke = [(int(level), per_level[level])
             for level in numeric_levels
             if per_level.get(level, {}).get('degradation') is not None
             and per_level[level]['degradation'] > args.tolerance]
    if acceptable:
        knee_level = str(min(level for level, _ in acceptable))
        knee = per_level[knee_level]
    else:
        # Nothing was acceptable: the case needs more than the loosest cap measured.
        knee_value = max(int(level) for level in numeric_levels) if numeric_levels else None
        knee_level = str(knee_value) if knee_value is not None else None
        knee = per_level.get(knee_level, {}) if knee_level else {}
    knee_degradation = knee.get('degradation')
    cliff_level = str(max(level for level, _ in broke)) if broke else None

    # Demand GB/s: the bandwidth the case actually drew while still inside
    # tolerance. This is what `alloc --strategy demand` weights by.
    demand_gbs = knee.get('mean_gbs')

    return {'case': case, 'language': '', 'class': klass,
            'baseline_actions_s': round(baseline, 3) if baseline else None,
            'knee_level': knee_level, 'cliff_level': cliff_level,
            'knee_degradation_pct': round(knee_degradation, 2)
            if knee_degradation is not None else None,
            'n_star': None,   # filled by the caller when concurrency>1 cells exist
            'demand_gbs': round(demand_gbs, 3) if demand_gbs is not None else None,
            'min_acceptable_level': knee_level,
            'levels_measured': len(numeric_levels)}


def _n_star(case, by_concurrency, args):
    """Smallest concurrency at which this case starts losing performance.

    Reference is the N=1 median *at the same cap*, so the number describes
    contention with neighbours rather than the cap itself. Measured at the
    uncapped level ('full') when present, otherwise at the loosest capped level.
    Returns None when the sweep has no concurrency > 1 cells.
    """
    levels = sorted({level for (c, level, _) in by_concurrency if c == case},
                    key=_level_key)
    if not levels:
        return None
    level = 'full' if 'full' in levels else levels[-1]
    concurrencies = sorted({conc for (c, lv, conc) in by_concurrency
                            if c == case and lv == level})
    if len(concurrencies) < 2 or concurrencies[0] != 1:
        return None
    reference = by_concurrency.get((case, level, 1))
    if not reference:
        return None
    base = statistics.median(reference)
    for conc in concurrencies[1:]:
        values = by_concurrency.get((case, level, conc), [])
        if not values:
            continue
        degradation = (statistics.median(values) - base) / base * 100
        if degradation > args.tolerance:
            return conc
    return None


# ------------------------------------------------------------------- alloc
def _analyze_alloc(args, out, *, quiet=False):
    path = _resolve_csv(args.alloc, 'alloc.csv')
    rows = _read_csv(path)
    if not rows:
        raise Error('alloc CSV is empty: %s' % path)

    strategies = {}
    batches = {}
    for row in rows:
        strategy = row['strategy']
        bucket = strategies.setdefault(strategy, {'actions': [], 'total': 0, 'ok': 0})
        bucket['total'] += 1
        if row.get('status') in ('completed', 'budget_exhausted'):
            bucket['ok'] += 1
            value = _to_float(row.get('actions_s')) or _to_float(row.get('case_wall_s'))
            if value is not None:
                bucket['actions'].append(value)
        key = (strategy, row.get('mix'), row.get('repeat'))
        batch = batches.setdefault(key, [])
        value = _to_float(row.get('actions_s')) or _to_float(row.get('case_wall_s'))
        if row.get('status') in ('completed', 'budget_exhausted') and value is not None:
            batch.append(value)

    # Paired comparison: only batches where every VM of every strategy in the same
    # (mix, repeat) completed, so one crash cannot flatter a strategy.
    by_mix = {}
    for (strategy, mix, repeat), values in batches.items():
        by_mix.setdefault((mix, repeat), {})[strategy] = values
    paired = {strategy: [] for strategy in strategies}
    for (mix, repeat), per_strategy in by_mix.items():
        if len(per_strategy) != len(strategies):
            continue    # some strategy did not run this mix/repeat
        for strategy, values in per_strategy.items():
            if values:
                paired[strategy].append(max(values))   # makespan proxy

    none_means = statistics.mean(paired['none']) if paired.get('none') else None
    summary = []
    for strategy, bucket in sorted(strategies.items()):
        makespans = paired.get(strategy) or []
        mean_makespan = statistics.mean(makespans) if makespans else None
        summary.append({
            'strategy': strategy, 'batches': len(by_mix),
            'vms_total': bucket['total'], 'vms_ok': bucket['ok'],
            'mean_actions_s': round(statistics.mean(bucket['actions']), 3)
            if bucket['actions'] else None,
            'median_actions_s': round(statistics.median(bucket['actions']), 3)
            if bucket['actions'] else None,
            'mean_makespan_s': round(mean_makespan, 3) if mean_makespan is not None else None,
            'pairwise_vs_none_s': round(mean_makespan - none_means, 3)
            if mean_makespan is not None and none_means is not None else None,
        })
    write_csv(out / 'alloc-summary.csv', summary, STRATEGY_FIELDS)
    if not quiet:
        print('\n=== strategies (%s) ===' % path.name)
        print('%-8s %8s %8s %7s %13s %14s %15s'
              % ('strategy', 'batches', 'vms_ok', 'vms', 'mean_actions', 'median_actions',
                 'mean_makespan'))
        for row in summary:
            print('%-8s %8d %8d %7d %13s %14s %15s'
                  % (row['strategy'], row['batches'], row['vms_ok'], row['vms_total'],
                     row['mean_actions_s'] or '-', row['median_actions_s'] or '-',
                     row['mean_makespan_s'] or '-'))
        print('\nMakespan is the slowest VM per batch over fully-completed paired batches '
              'only. Descriptive, not a significance test.')
