"""`bwx sweep` -- Phase 1: case x level x concurrency sensitivity matrix.

Two operating modes, deliberately kept in one command because they share every
measurement detail (only the group/placement plan differs):

* `--concurrency 1` (default): one VM alone on a node. Measures the case's
  *intrinsic* bandwidth sensitivity: the cap is the only variable.
* `--concurrency N`: N replicas of the same case share one MB cap, so each VM
  effectively gets about cap/N. This is the "amplify until the degradation shows"
  mode: a light case that is flat at N=1 usually bends at N=8. Because the
  replicas contend, the absolute numbers describe contention behaviour, not
  intrinsic sensitivity -- that distinction is what the analysis uses N* for.

Level selection is **derived from the target host**, never hard-coded: the
requested set is snapped through a scratch group (`info/MB/bandwidth_gran` decides
how many distinct levels actually exist), and only distinct applied levels are run.
On this calibration host gran=9 collapses 1..8 into one level; on the real test
machine 1..9 are distinct, which is exactly the low-bandwidth region where hungry
cases degrade.

Isolation and accounting:
* each cell runs on its own node with SMT-disjoint CPUs (Allocator.validate);
* one ddrc collector spans the whole sweep, and every record gets its own
  [CASE_START, CASE_DONE] window, so idle time never dilutes a measurement;
* a baseline cell (`full`, no MPAM group) is always included: it is the
  denominator of every degradation ratio, and it must be measured under the same
  placement as the capped cells.
"""
import itertools
import time
from pathlib import Path

from .. import bandwidth as bw
from .. import cases as cases_mod
from .. import config as config_mod
from .. import driver
from .. import placement as placement_mod
from ..util import (Error, append_jsonl, require_root, utc_stamp, write_csv,
                    write_json)

COMMAND = 'sweep'
HELP = 'Phase 1: case x MB level x concurrency sensitivity matrix'

CSV_FIELDS = ['case', 'language', 'level_requested', 'level_applied', 'group',
              'concurrency', 'replica', 'repeat', 'status', 'verified',
              'serial_verification', 'completed_steps', 'total_steps',
              'case_wall_s', 'actions_s', 'boot_ready_s', 'vmm_wall_s',
              'nonzero_actions', 'max_step_s', 'node', 'cpus',
              'peak_gbs', 'mean_gbs', 'mean_rd_gbs', 'mean_wr_gbs', 'vm_dir']


def add_arguments(parser):
    parser.add_argument('--case', action='append', default=[], metavar='ID',
                        help='instance_id (repeatable)')
    parser.add_argument('--case-file', type=Path, help='one instance_id per line')
    parser.add_argument('--all', action='store_true', help='every converted case')
    parser.add_argument('--levels', default='', metavar='LIST',
                        help='comma-separated requested MB levels. Default: derived '
                             'from info/MB/bandwidth_gran (low range dense, which is '
                             'where hungry cases degrade on a fine-granularity host).')
    parser.add_argument('--no-baseline', action='store_true',
                        help='skip the uncapped baseline cell (NOT recommended: it is '
                             'the denominator of every degradation ratio)')
    parser.add_argument('--concurrency', type=int, default=1, metavar='N',
                        help='replicas of each case sharing one cap (default 1)')
    parser.add_argument('--repeats', type=int, default=1, help='repeats per cell')
    parser.add_argument('--nodes', default='', metavar='LIST',
                        help='NUMA nodes to use (default: auto, one per concurrent batch)')
    parser.add_argument('--out', type=Path, help='output dir (default logs/sweep/<stamp>)')
    parser.add_argument('--wait', choices=('on', 'off'), default='off')
    parser.add_argument('--case-timeout', type=float)
    parser.add_argument('--boot-timeout', type=float)
    parser.add_argument('--action-timeout', type=float, default=600)
    parser.add_argument('--hard-limit', action='store_true')
    parser.add_argument('--no-tools-disk', action='store_true')
    parser.add_argument('--stagger', type=float, default=0.0,
                        help='seconds between replica launches (default 0 = simultaneous)')
    parser.add_argument('--dry-run', action='store_true',
                        help='print the matrix and exit without launching any VM')


def run(args):
    config = config_mod.from_args(args)
    config_mod.validate_runtime(config)
    require_root('sweep (KVM, resctrl writes, uncore PMU)')
    if args.concurrency < 1:
        raise Error('--concurrency must be >= 1')

    selected = cases_mod.select_cases(config, cases=','.join(args.case) or None,
                                      case_file=args.case_file, all_converted=args.all)
    out = args.out or config.logs_dir / ('sweep/' + utc_stamp())
    context = driver.RunContext(config, out, label='sweep')
    platform = context.require_resctrl()
    topology = context.require_bandwidth()

    requested_levels = _requested_levels(args, platform)
    snap = platform.level_map(requested_levels)
    distinct = sorted(set(snap.values()))
    nodes = _nodes(args, platform, topology)
    matrix = _matrix(selected, requested_levels, snap, distinct, args)

    context.node_plan = nodes
    context.snapshot({'matrix_cells': len(matrix), 'level_snap': snap,
                      'distinct_levels': distinct, 'requested_levels': requested_levels,
                      'concurrency': args.concurrency, 'repeats': args.repeats,
                      'nodes': nodes, 'cases': [c.as_dict() for c in selected]})
    write_json(out / 'level-snap.json', snap)

    print('sweep plan: %d case(s) x %d level(s) x N=%d x %d repeat(s) = %d cell run(s)'
          % (len(selected), len(distinct) + (0 if args.no_baseline else 1),
             args.concurrency, args.repeats, len(matrix)))
    print('levels requested %s -> applied %s' % (requested_levels, distinct))
    print('nodes: %s' % nodes)
    if args.dry_run:
        for cell in matrix:
            cell['node'] = _pick_node(context, cell, cell['concurrency'])
            print('  case=%-40s level=%-4s N=%d repeat=%d node=%s'
                  % (cell['case'].instance_id, cell['level'], cell['concurrency'],
                     cell['repeat'], cell['node']))
        write_json(out / 'plan.json',
                   [{'case': c['case'].instance_id, 'level': c['level'],
                     'node': c['node'], 'repeat': c['repeat']} for c in matrix])
        return 0

    if not args.no_tools_disk:
        context.require_tools_disk()

    csv_path = out / 'sweep.csv'
    collector = None
    epoch = time.time()
    rows, records, placements = [], [], []
    groups = driver.GroupPool(context, 'bwx-sweep')
    try:
        if topology is not None:
            collector = bw.BandwidthCollector(topology, out, interval=1.0, label='sweep')
            if collector.start():
                epoch = time.time()
            else:
                collector = None
                print('warning: ddrc collector did not start; bandwidth columns empty')
        for index, cell in enumerate(matrix, 1):
            # Groups are created once per distinct cap and reused across every
            # cell; see GroupPool for why (num_rmids=1 makes create/destroy cycles
            # exhaust the monitoring pool).
            group = None
            if cell['level'] != 'full':
                group = groups.acquire(int(cell['level']), hard_limit=args.hard_limit)
            cell['node'] = _pick_node(context, cell, cell['concurrency'])
            print('=== [%d/%d] %s level=%s N=%d repeat=%d node=%d'
                  % (index, len(matrix), cell['case'].instance_id, cell['level'],
                     cell['concurrency'], cell['repeat'], cell['node']))
            if group is not None:
                print('    mpam group %s applied=%s' % (group.name, group.snap()))
            cell_records, cell_placements = _run_cell(
                context, cell, group=group, args=args, out=out)
            records.extend(cell_records)
            placements.extend(cell_placements)
            for position, record in enumerate(cell_records):
                rows.append(_row(cell, record, cell_placements[position], position))
            # Incremental CSV: an interrupted sweep still leaves usable data.
            write_csv(csv_path, rows, CSV_FIELDS)
            write_json(out / 'records-partial.json', records)
    except KeyboardInterrupt:
        print('\ninterrupted; partial results kept in %s' % out)
    finally:
        if collector is not None:
            collector.stop()
            driver.attach_bandwidth(context, collector, records, placements, epoch)
            # Bandwidth stats are only known after the collector stops, so the CSV
            # is rewritten once at the end with the window statistics filled in.
            for index, record in enumerate(records):
                _fill_bandwidth(rows[index], record)
            write_csv(csv_path, rows, CSV_FIELDS)
        errors = groups.release()
        if errors:
            print('warning: resctrl cleanup left groups behind: %s' % errors)

    write_json(out / 'records.json', records)
    write_csv(out / 'sweep.csv', rows, CSV_FIELDS)
    _summarize(out, rows)
    failed = [r for r in rows if r['status'] not in ('completed', 'budget_exhausted')]
    return 1 if failed else 0


def _requested_levels(args, platform):
    """Requested levels, always including the fine low range this experiment needs."""
    if args.levels.strip():
        levels = sorted({int(x) for x in args.levels.split(',') if x.strip()})
    else:
        gran = max(1, platform.granularity)
        # Dense over 1..9 regardless of granularity: on a fine-granularity host these
        # are nine distinct caps and the most informative region; on a coarse host
        # they collapse to one level and cost a single run after snapping.
        levels = sorted(set(list(range(1, 10))
                            + list(range(gran, 101, gran))
                            + [max(1, 100 - gran // 2), 100]))
    return [platform.check_level(level) for level in levels]


def _nodes(args, platform, topology):
    if args.nodes.strip():
        return [int(x) for x in args.nodes.split(',') if x.strip()]
    if topology is not None and topology.nodes():
        return topology.nodes()
    return placement_mod.node_ids()


def _matrix(selected, requested, snap, distinct, args):
    """Expand the (case, level, repeat) product into concrete run cells.

    Baseline (`full`) first per case, so an aborted sweep still leaves a usable
    denominator for the levels that did run.
    """
    cells = []
    levels = ['full'] + distinct if not args.no_baseline else list(distinct)
    for case in selected:
        for level in levels:
            for repeat in range(args.repeats):
                cells.append({'case': case, 'level': level, 'concurrency': args.concurrency,
                              'repeat': repeat, 'node': None})
    return cells


def _run_cell(context, cell, *, group, args, out):
    """Run one cell: N replicas of one case under one cap, on disjoint CPUs."""
    config = context.config
    case = cell['case']
    n = cell['concurrency']
    # The node is chosen by the caller so it can be reported before the run.
    node = cell.get('node') or _pick_node(context, cell, n)
    cell['node'] = node
    allocator = placement_mod.Allocator([node], config.vcpus, mem_mib=config.mem_mib)
    per_node, _ = allocator.capacity()
    if per_node.get(node, 0) < n:
        raise Error('node %d can host %d VM(s) at vcpus=%d, need %d'
                    % (node, per_node.get(node, 0), config.vcpus, n))
    placements = allocator.take_many(n)
    placement_mod.Allocator.validate(placements)

    cell_out = out / ('cells/%s-lvl%s-n%d-r%d' % (case.instance_id, cell['level'],
                                                  n, cell['repeat']))
    cell_out.mkdir(parents=True, exist_ok=True)
    write_json(cell_out / 'cell.json',
               {'case': case.instance_id, 'level': cell['level'], 'concurrency': n,
                'repeat': cell['repeat'], 'node': node,
                'group': group.as_dict() if group else None,
                'placements': [p.as_dict() for p in placements]})

    records = [None] * n
    threads = []
    if n == 1:
        records[0] = driver.run_one(context, case, placement=placements[0],
                                    mpam_group=group, out_dir=cell_out,
                                    wait=args.wait, case_timeout=args.case_timeout,
                                    boot_timeout=args.boot_timeout,
                                    action_timeout=args.action_timeout,
                                    label='%s-r%d' % (case.instance_id, cell['repeat']),
                                    keep_work=True)
    else:
        import threading
        errors = []

        def worker(index):
            try:
                if index and args.stagger:
                    time.sleep(args.stagger * index)
                records[index] = driver.run_one(
                    context, case, placement=placements[index], mpam_group=group,
                    out_dir=cell_out, wait=args.wait, case_timeout=args.case_timeout,
                    boot_timeout=args.boot_timeout, action_timeout=args.action_timeout,
                    label='%s#%d-r%d' % (case.instance_id, index + 1, cell['repeat']),
                    keep_work=True)
            except Exception as exc:  # noqa: BLE001 - keep the other replicas running
                errors.append('%d: %s' % (index, exc))
                records[index] = {'status': 'driver_error', 'error': str(exc)}

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            print('    replica errors: %s' % '; '.join(errors))
    write_json(cell_out / 'cell-records.json', records)
    return records, placements


def _pick_node(context, cell, n):
    """Round-robin over the nodes that can host n VMs.

    A counter (not a hash) drives the rotation, so the plan is reproducible across
    processes and repeated cells do not always hammer the same node. Honours
    --nodes when given; otherwise falls back to the measured topology.
    """
    from .. import placement as placement_mod
    config = context.config
    usable = []
    for node in getattr(context, 'node_plan', None) or _nodes_from_context(context):
        try:
            allocator = placement_mod.Allocator([node], config.vcpus,
                                                mem_mib=config.mem_mib)
        except Exception:  # noqa: BLE001 - node unusable (CPU/memory)
            continue
        per_node, _ = allocator.capacity()
        if per_node.get(node, 0) >= n:
            usable.append(node)
    if not usable:
        raise Error('no NUMA node can host %d VM(s) at vcpus=%d mem_mib=%d'
                    % (n, config.vcpus, config.mem_mib))
    counter = getattr(context, '_node_cursor', 0)
    context._node_cursor = counter + 1
    return usable[counter % len(usable)]


def _nodes_from_context(context):
    topology = context.require_bandwidth()
    if topology is not None and topology.nodes():
        return topology.nodes()
    return placement_mod.node_ids()


def _row(cell, record, placement, replica):
    verification = (record or {}).get('verification') or {}
    bandwidth = _bandwidth_for(record, placement)
    return {'case': cell['case'].instance_id, 'language': cell['case'].language,
            'level_requested': cell['level'],
            'level_applied': (record or {}).get('bandwidth_requested', cell['level']),
            'group': ((record or {}).get('mpam') or {}).get('name', ''),
            'concurrency': cell['concurrency'], 'replica': replica + 1,
            'repeat': cell['repeat'],
            'status': (record or {}).get('status', 'missing'),
            'verified': verification.get('ok'),
            'serial_verification': verification.get('serial_verification', ''),
            'completed_steps': (record or {}).get('guest_completed_steps'),
            'total_steps': (record or {}).get('steps_total'),
            'case_wall_s': (record or {}).get('case_wall_s'),
            'actions_s': (record or {}).get('actions_s'),
            'boot_ready_s': (record or {}).get('boot_ready_s'),
            'vmm_wall_s': (record or {}).get('vmm_wall_s'),
            'nonzero_actions': (record or {}).get('guest_nonzero_actions'),
            'max_step_s': (record or {}).get('max_step_s'),
            'node': placement.node if placement else None,
            'cpus': placement.physcpubind if placement else '',
            'peak_gbs': bandwidth.get('peak_gbs'), 'mean_gbs': bandwidth.get('mean_gbs'),
            'mean_rd_gbs': bandwidth.get('mean_rd_gbs'),
            'mean_wr_gbs': bandwidth.get('mean_wr_gbs'),
            'vm_dir': (record or {}).get('vm_dir')}


def _bandwidth_for(record, placement):
    bandwidth = (record or {}).get('bandwidth') or {}
    if not bandwidth.get('available'):
        return {}
    key = 'node%d' % placement.node if placement is not None else 'all'
    return bandwidth.get(key) or bandwidth.get('all') or {}


def _fill_bandwidth(row, record):
    """Second pass: bandwidth windows are only known after the collector stops."""
    bandwidth = (record or {}).get('bandwidth') or {}
    stats = bandwidth.get('node%d' % row['node']) or bandwidth.get('all') or {}
    for key in ('peak_gbs', 'mean_gbs', 'mean_rd_gbs', 'mean_wr_gbs'):
        row[key] = stats.get(key)


def _summarize(out, rows):
    """Per-case, per-level median actions_s, plus degradation vs the baseline."""
    import statistics
    by = {}
    for row in rows:
        if row['status'] not in ('completed', 'budget_exhausted'):
            continue
        value = row['actions_s'] if row['actions_s'] is not None else row['case_wall_s']
        if value is None:
            continue
        by.setdefault((row['case'], row['level_applied']), []).append(float(value))
    summary = []
    cases = sorted({row['case'] for row in rows})
    for case in cases:
        baseline = by.get((case, 'full'))
        base = statistics.median(baseline) if baseline else None
        for (row_case, level), values in sorted(by.items(), key=lambda kv: str(kv[0][1])):
            if row_case != case:
                continue
            median = statistics.median(values)
            summary.append({'case': case, 'level_applied': level, 'runs': len(values),
                            'median_actions_s': round(median, 3),
                            'baseline_actions_s': round(base, 3) if base else None,
                            'degradation_pct': round((median - base) / base * 100, 2)
                            if base else None})
    write_csv(out / 'sweep-summary.csv', summary,
              ['case', 'level_applied', 'runs', 'median_actions_s',
               'baseline_actions_s', 'degradation_pct'])
    print('\n%-42s %8s %7s %10s %10s %10s'
          % ('case', 'level', 'runs', 'median_s', 'base_s', 'degrad%'))
    for row in summary:
        print('%-42s %8s %7d %10s %10s %10s'
              % (row['case'][:42], row['level_applied'], row['runs'],
                 row['median_actions_s'], row['baseline_actions_s'] if row['baseline_actions_s'] is not None else '-',
                 row['degradation_pct'] if row['degradation_pct'] is not None else '-'))
    print('\nwrote %s' % (out / 'sweep.csv'))
