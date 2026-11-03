"""`bwx alloc` -- Phase 3: concurrent heterogeneous runs under allocation strategies.

This is where the experiment's actual claim is tested: given a mix of cases with
different bandwidth sensitivity, does *differentiated* MPAM allocation beat
*uniform* allocation?

Strategies (all run the same case mix, the same concurrency and the same repeats,
so the only difference is how bandwidth and nodes are handed out):

    none      no MPAM group at all. The upper bound: what the machine delivers
              when nobody is capped. Also the baseline for "is MPAM even hurting".
    shared    every VM in ONE group at one cap (what a naive deployment does).
              Total budget is fixed; each VM gets roughly cap/N.
    equal     one group PER VM, all at the same cap. Isolates "group sharing" from
              "cap value": differences vs `shared` are the cost of sharing a CLOSID.
    demand    one group per VM, cap proportional to the case's measured demand
              (from `bwx analyze --emit-demand`). Hungry cases get more, light
              cases get less, under a total budget. This is the hypothesis.
    spread    `demand` (or `equal` when no demand file is given) with VMs placed
              one-per-node so each gets a full MB domain. Tests whether domain
              separation beats in-domain differentiation.

Every strategy is run with the same seed-derived launch order and the repeats are
rotated (round-robin) so no strategy is systematically advantaged by running first
on a cold machine.
"""
import hashlib
import json
import random
import time
from pathlib import Path

from .. import bandwidth as bw
from .. import cases as cases_mod
from ..cases import _dedupe, parse_case_file
from .. import config as config_mod
from .. import driver
from .. import placement as placement_mod
from ..util import (Error, require_root, utc_stamp, write_csv, write_json)

COMMAND = 'alloc'
HELP = 'Phase 3: concurrent multi-case runs comparing bandwidth allocation strategies'

STRATEGIES = ('none', 'shared', 'equal', 'demand', 'spread')
CSV_FIELDS = ['strategy', 'mix', 'repeat', 'case', 'language', 'role', 'group',
              'level_requested', 'level_applied', 'node', 'cpus', 'status', 'verified',
              'completed_steps', 'total_steps', 'case_wall_s', 'actions_s',
              'boot_ready_s', 'vmm_wall_s', 'peak_gbs', 'mean_gbs', 'vm_dir']


def add_arguments(parser):
    parser.add_argument('--strategies', default='none,shared,equal,demand',
                        help='comma-separated subset of %s' % ','.join(STRATEGIES))
    parser.add_argument('--case', action='append', default=[], metavar='ID',
                        help='case to include in the mix (repeatable)')
    parser.add_argument('--case-file', type=Path,
                        help='cases from a file: .txt (one id per line, # comments) '
                             'or .json (["id",...] / {"cases":[...]} / structured '
                             'mixes {"mix":{"case":{"role","weight"}}}). With '
                             '--concurrency N: N < entries -> first N; N > entries '
                             '-> replicate round-robin to N VMs')
    parser.add_argument('--mix', type=Path, metavar='JSON',
                        help='explicit mix: {"name": {"case": {"role": "hungry|light", '
                             '"weight": 3}}}. Overrides --case/--case-file.')
    parser.add_argument('--demand', type=Path, metavar='JSON',
                        help='per-case demand file from `bwx analyze --emit-demand`; '
                             'required by the demand/spread strategies')
    parser.add_argument('--concurrency', type=int, default=4,
                        help='VMs running at once (default 4)')
    parser.add_argument('--cap', type=int, default=54, metavar='PCT',
                        help='MB cap for shared/equal strategies (default 54)')
    parser.add_argument('--budget', type=int, default=200, metavar='PCT_SUM',
                        help='total MB percent the demand strategy may spend '
                             '(default 200, i.e. 4 VMs x 50)')
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--nodes', default='', metavar='LIST', help='NUMA nodes to use')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--wait', choices=('on', 'off'), default='off')
    parser.add_argument('--case-timeout', type=float)
    parser.add_argument('--boot-timeout', type=float)
    parser.add_argument('--action-timeout', type=float, default=600)
    parser.add_argument('--hard-limit', action='store_true')
    parser.add_argument('--no-tools-disk', action='store_true')
    parser.add_argument('--stagger', type=float, default=0.0)
    parser.add_argument('--seed', type=int, default=0, help='mix shuffle seed')
    parser.add_argument('--dry-run', action='store_true')


def run(args):
    config = config_mod.from_args(args)
    config_mod.validate_runtime(config)
    require_root('alloc (KVM, resctrl writes, uncore PMU)')

    strategies = [s.strip() for s in args.strategies.split(',') if s.strip()]
    for strategy in strategies:
        if strategy not in STRATEGIES:
            raise Error('unknown strategy %r (have %s)' % (strategy, ','.join(STRATEGIES)))
    mixes = _mixes(args, config)
    demand = _demand(args)
    for strategy in strategies:
        if strategy in ('demand', 'spread') and not demand:
            raise Error('strategy %r needs --demand (run `bwx analyze --emit-demand` '
                        'on sweep results first)' % strategy)

    out = args.out or config.logs_dir / ('alloc/' + utc_stamp())
    context = driver.RunContext(config, out, label='alloc')
    context.require_resctrl()
    topology = context.require_bandwidth()
    nodes = _nodes(args, topology)

    plan = []
    for repeat in range(args.repeats):
        for strategy in strategies:
            for mix_name, mix in mixes.items():
                plan.append({'repeat': repeat, 'strategy': strategy,
                             'mix': mix_name, 'entries': mix})
    # Rotate the strategy order per repeat so no strategy always runs first.
    random.Random(args.seed).shuffle(plan)

    context.snapshot({'strategies': strategies, 'mixes': mixes, 'demand': demand,
                      'concurrency': args.concurrency, 'cap': args.cap,
                      'budget': args.budget, 'repeats': args.repeats, 'nodes': nodes,
                      'plan': [{'repeat': p['repeat'], 'strategy': p['strategy'],
                                'mix': p['mix']} for p in plan]})
    print('alloc plan: %d run(s) = %d repeat x %d strategy x %d mix, N=%d'
          % (len(plan), args.repeats, len(strategies), len(mixes), args.concurrency))
    if args.dry_run:
        for entry in plan:
            allocation = _allocate(entry['strategy'], entry['entries'], args, demand,
                                   nodes, config)
            print('  repeat=%d strategy=%-7s mix=%s' % (entry['repeat'], entry['strategy'],
                                                        entry['mix']))
            for item in allocation['items']:
                print('      %-42s role=%-6s level=%-4s node=%s group=%s'
                      % (item['case_id'][:42], item['role'], item['level'],
                         item['node'], item['group']))
        write_json(out / 'plan.json',
                   [{'repeat': p['repeat'], 'strategy': p['strategy'], 'mix': p['mix']}
                    for p in plan])
        return 0

    if not args.no_tools_disk:
        context.require_tools_disk()

    csv_path = out / 'alloc.csv'
    rows, records, placements = [], [], []
    collector = None
    epoch = time.time()
    groups = driver.GroupPool(context, 'bwx-alloc')
    try:
        if topology is not None:
            collector = bw.BandwidthCollector(topology, out, interval=1.0, label='alloc')
            if not collector.start():
                collector = None
                print('warning: ddrc collector did not start; bandwidth columns empty')
            else:
                epoch = time.time()
        for index, entry in enumerate(plan, 1):
            print('=== [%d/%d] repeat=%d strategy=%s mix=%s'
                  % (index, len(plan), entry['repeat'], entry['strategy'], entry['mix']))
            batch_rows, batch_records, batch_placements = _run_batch(
                context, entry, args, demand, nodes, out, groups)
            rows.extend(batch_rows)
            records.extend(batch_records)
            placements.extend(batch_placements)
            write_csv(csv_path, rows, CSV_FIELDS)
            groups.release()
    except KeyboardInterrupt:
        print('\ninterrupted; partial results kept in %s' % out)
    finally:
        if collector is not None:
            collector.stop()
            driver.attach_bandwidth(context, collector, records, placements, epoch)
            for position, record in enumerate(records):
                _fill_bandwidth(rows[position], record)
        errors = groups.release()
        if errors:
            print('warning: resctrl cleanup left groups behind: %s' % errors)

    write_json(out / 'records.json', records)
    write_csv(csv_path, rows, CSV_FIELDS)
    _compare(out, rows)
    return 0


# ----------------------------------------------------------------- mix / demand
def _mixes(args, config):
    """{mix_name: [{case_id, role, weight}]}

    Sources, in priority order:
    * --mix FILE: JSON object of named mixes (the full format)
    * --case-file FILE: .txt (one id per line) or .json (array / {"cases": [...]}
      for a plain default mix, or the same structured-mixes object as --mix)
    * --case ID (repeatable)

    Expansion to --concurrency happens later, in _allocate: fewer entries than
    concurrency are replicated round-robin, more are truncated to the first
    `concurrency` (see its docstring).
    """
    if args.mix:
        doc = json.loads(Path(args.mix).read_text(encoding='utf-8-sig'))
        from ..cases import _normalize_json_selection
        parsed = _normalize_json_selection(doc, Path(args.mix))
        if parsed['mixes'] is None:
            # An array or {"cases": [...]}: one plain mix named "default".
            return {'default': [{'case_id': iid, 'role': 'mixed', 'weight': 1.0}
                                for iid in parsed['ids']]}
        return parsed['mixes']

    ids = list(args.case)
    if args.case_file:
        parsed = parse_case_file(args.case_file)
        if parsed['mixes'] is not None:
            return parsed['mixes']
        ids.extend(parsed['ids'])
    if not ids:
        raise Error('no mix given: pass --mix, --case or --case-file '
                    '(txt or json)')
    return {'default': [{'case_id': iid, 'role': 'mixed', 'weight': 1.0}
                        for iid in _dedupe(ids)]}


def _demand(args):
    if not args.demand:
        return None
    doc = json.loads(Path(args.demand).read_text(encoding='utf-8-sig'))
    # Accept both {case: {level, gbs}} and {cases: {case: {...}}}.
    table = doc.get('cases', doc)
    return {case: {'level': int(meta.get('min_acceptable_level', 100)),
                   'gbs': float(meta.get('demand_gbs') or 0.0),
                   'class': meta.get('class', 'unknown')}
            for case, meta in table.items() if isinstance(meta, dict)}


def _nodes(args, topology):
    if args.nodes.strip():
        return [int(x) for x in args.nodes.split(',') if x.strip()]
    if topology is not None and topology.nodes():
        return topology.nodes()
    return placement_mod.node_ids()


# --------------------------------------------------------------------- allocate
def _allocate(strategy, entries, args, demand, nodes, config=None):
    """Decide per-VM group, cap and node for one batch. Pure function (testable)."""
    n = args.concurrency
    # Replicate/trim the mix to exactly n VMs (round-robin, stable order).
    items = []
    for index in range(n):
        entry = entries[index % len(entries)]
        items.append(dict(entry, slot=index))
    if len(items) != n:
        raise Error('mix expansion produced %d items for concurrency %d' % (len(items), n))

    if strategy == 'none':
        levels = [None] * n
        groups = [None] * n
    elif strategy == 'shared':
        levels = [args.cap] * n
        groups = ['shared%d' % args.cap] * n          # one group name -> shared CLOSID
    elif strategy == 'equal':
        levels = [args.cap] * n
        groups = ['eq%d-%d' % (args.cap, i) for i in range(n)]
    elif strategy in ('demand', 'spread'):
        weights, levels, groups = [], [], []
        for item in items:
            meta = (demand or {}).get(item['case_id'], {})
            # Weight by the measured minimum acceptable level, defaulting to full.
            weight = meta.get('level') or 100
            weights.append(float(weight))
        total = sum(weights) or 1.0
        for index, item in enumerate(items):
            share = args.budget * weights[index] / total
            levels.append(_clamp_level(share))
            groups.append('dm%d-%s' % (index, item['case_id'][:20]))
    else:
        raise Error('unknown strategy %r' % strategy)

    # Node placement: `spread` puts one VM per node (cycling); others pack nodes so
    # the VMs actually contend for the same MB domain.
    node_plan = _node_plan(strategy, n, nodes, config, items)
    for index, item in enumerate(items):
        item['level'] = levels[index]
        item['group'] = groups[index]
        item['node'] = node_plan[index]
    return {'strategy': strategy, 'items': items, 'nodes': node_plan,
            'total_requested': sum(l for l in levels if l is not None)}


def _clamp_level(value):
    """Round a fractional share to a safe integer level."""
    level = int(round(value))
    return max(1, min(100, level))


class _Sizing:
    """vcpus/mem_mib fallback so allocation stays a pure, testable function."""

    def __init__(self, vcpus=2, mem_mib=4096):
        self.vcpus = vcpus
        self.mem_mib = mem_mib


def _node_plan(strategy, n, nodes, config, items):
    config = config or _Sizing()
    """Assign a node to each of the n VMs.

    `spread` cycles over nodes (one VM per MB domain, so each gets a full domain).
    The other strategies pack onto as few nodes as capacity allows, because
    contention within one domain is what makes a cap observable.
    """
    if strategy == 'spread' and len(nodes) > 1:
        return [nodes[index % len(nodes)] for index in range(n)]
    plan, cursor = [], 0
    capacity = {}
    for node in nodes:
        try:
            allocator = placement_mod.Allocator([node], config.vcpus,
                                                mem_mib=config.mem_mib)
            per_node, _ = allocator.capacity()
            capacity[node] = per_node.get(node, 0)
        except Exception:  # noqa: BLE001 - node unusable (CPU/memory shortage)
            capacity[node] = 0
    usable = [node for node in nodes if capacity.get(node, 0) > 0]
    if not usable:
        raise Error('no usable NUMA node for %d VMs at vcpus=%d mem_mib=%d'
                    % (n, config.vcpus, config.mem_mib))
    for _ in range(n):
        node = usable[cursor % len(usable)]
        plan.append(node)
        capacity[node] -= 1
        if capacity[node] <= 0:
            cursor += 1
    return plan


# ------------------------------------------------------------------------ batch
def _run_batch(context, entry, args, demand, nodes, out, groups):
    config = context.config
    strategy = entry['strategy']
    allocation = _allocate(strategy, entry['entries'], args, demand, nodes, config)
    batch_out = out / ('batches/%s-%s-r%d' % (strategy, entry['mix'], entry['repeat']))
    batch_out.mkdir(parents=True, exist_ok=True)
    write_json(batch_out / 'allocation.json', allocation)

    # Resolve cases once (fails fast if a case is not converted).
    resolved = {}
    for item in allocation['items']:
        if item['case_id'] not in resolved:
            resolved[item['case_id']] = cases_mod.load_case(config, item['case_id'])

    # Create the resctrl groups this batch needs (shared strategy -> one group).
    group_objects = {}
    for item in allocation['items']:
        if item['level'] is None or item['group'] in group_objects:
            continue
        # hash() is salted per process, so a stable digest keeps group names
        # reproducible across runs (and short enough for resctrl).
        mix_tag = hashlib.sha256(entry['mix'].encode()).hexdigest()[:6]
        group_objects[item['group']] = groups.acquire(
            item['level'], hard_limit=args.hard_limit, hint=mix_tag)

    # Placements: one allocator per node, taken in slot order.
    allocators = {}
    placements = [None] * len(allocation['items'])
    for item in allocation['items']:
        node = item['node']
        if node not in allocators:
            allocators[node] = placement_mod.Allocator([node], config.vcpus,
                                                       mem_mib=config.mem_mib)
        placements[item['slot']] = allocators[node].take()
    placement_mod.Allocator.validate(placements)

    print('    %d VM(s): %s' % (len(allocation['items']),
                                ', '.join('%s@%s/node%d'
                                          % (i['case_id'][:18], i['level'], i['node'])
                                          for i in allocation['items'])))

    records = [None] * len(allocation['items'])
    start = time.monotonic()
    wall_start = time.time()
    if len(allocation['items']) == 1:
        records[0] = _run_item(context, allocation['items'][0], resolved, placements[0],
                               group_objects, args, batch_out, 0)
    else:
        import threading
        errors = []

        def worker(index):
            item = allocation['items'][index]
            try:
                if index and args.stagger:
                    time.sleep(args.stagger * index)
                records[index] = _run_item(context, item, resolved, placements[index],
                                           group_objects, args, batch_out, index)
            except Exception as exc:  # noqa: BLE001 - other VMs keep running
                errors.append('%s: %s' % (item['case_id'], exc))
                records[index] = {'status': 'driver_error', 'error': str(exc),
                                  'instance_id': item['case_id']}

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(len(allocation['items']))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            print('    errors: %s' % '; '.join(errors))
    makespan = time.monotonic() - start

    rows = []
    for item, record, placement in zip(allocation['items'], records, placements):
        verification = (record or {}).get('verification') or {}
        rows.append({
            'strategy': strategy, 'mix': entry['mix'], 'repeat': entry['repeat'],
            'case': item['case_id'], 'language': resolved[item['case_id']].language,
            'role': item['role'], 'group': (record or {}).get('mpam', {}).get('name', ''),
            'level_requested': item['level'],
            'level_applied': (record or {}).get('bandwidth_requested', item['level']),
            'node': placement.node, 'cpus': placement.physcpubind,
            'status': (record or {}).get('status', 'missing'),
            'verified': verification.get('ok'),
            'completed_steps': (record or {}).get('guest_completed_steps'),
            'total_steps': (record or {}).get('steps_total'),
            'case_wall_s': (record or {}).get('case_wall_s'),
            'actions_s': (record or {}).get('actions_s'),
            'boot_ready_s': (record or {}).get('boot_ready_s'),
            'vmm_wall_s': (record or {}).get('vmm_wall_s'),
            'peak_gbs': None, 'mean_gbs': None, 'vm_dir': (record or {}).get('vm_dir'),
        })
    write_json(batch_out / 'batch.json',
               {'strategy': strategy, 'mix': entry['mix'], 'repeat': entry['repeat'],
                'makespan_s': round(makespan, 3), 'wall_start': wall_start,
                'allocation': allocation, 'records': records, 'rows': rows})
    print('    makespan %.1fs, statuses: %s'
          % (makespan, ','.join((r or {}).get('status', 'missing') for r in records)))
    return rows, records, placements


def _run_item(context, item, resolved, placement, group_objects, args, batch_out, index):
    group = group_objects.get(item['group']) if item['group'] else None
    return driver.run_one(context, resolved[item['case_id']], placement=placement,
                          mpam_group=group, out_dir=batch_out,
                          wait=args.wait, case_timeout=args.case_timeout,
                          boot_timeout=args.boot_timeout,
                          action_timeout=args.action_timeout,
                          label='%s-slot%d' % (item['case_id'], index), keep_work=True)


def _fill_bandwidth(row, record):
    bandwidth = (record or {}).get('bandwidth') or {}
    stats = bandwidth.get('node%d' % row['node']) or bandwidth.get('all') or {}
    row['peak_gbs'] = stats.get('peak_gbs')
    row['mean_gbs'] = stats.get('mean_gbs')


def _compare(out, rows):
    """Per-strategy aggregate: makespan proxy, mean/median actions_s, failures."""
    import statistics
    by = {}
    for row in rows:
        by.setdefault((row['strategy'], row['mix'], row['repeat']), []).append(row)
    strategies = {}
    for (strategy, mix, repeat), group in by.items():
        ok = [r for r in group if r['status'] in ('completed', 'budget_exhausted')]
        values = [float(r['actions_s'] or r['case_wall_s'] or 0) for r in ok]
        bucket = strategies.setdefault(strategy, {'runs': 0, 'ok': 0, 'actions': [],
                                                  'makespan': []})
        bucket['runs'] += 1
        bucket['ok'] += len(ok)
        bucket['actions'].extend(values)
        # Makespan proxy: the slowest VM in the batch (all started together).
        if values:
            bucket['makespan'].append(max(values))
    summary = []
    for strategy, bucket in sorted(strategies.items()):
        summary.append({
            'strategy': strategy, 'batches': bucket['runs'], 'vms_ok': bucket['ok'],
            'mean_actions_s': round(statistics.mean(bucket['actions']), 3)
            if bucket['actions'] else None,
            'median_actions_s': round(statistics.median(bucket['actions']), 3)
            if bucket['actions'] else None,
            'mean_makespan_s': round(statistics.mean(bucket['makespan']), 3)
            if bucket['makespan'] else None,
        })
    write_csv(out / 'alloc-summary.csv', summary,
              ['strategy', 'batches', 'vms_ok', 'mean_actions_s', 'median_actions_s',
               'mean_makespan_s'])
    print('\n%-8s %8s %7s %14s %15s %16s'
          % ('strategy', 'batches', 'vms_ok', 'mean_actions', 'median_actions', 'mean_makespan'))
    for row in summary:
        print('%-8s %8d %7d %14s %15s %16s'
              % (row['strategy'], row['batches'], row['vms_ok'],
                 row['mean_actions_s'], row['median_actions_s'], row['mean_makespan_s']))
    print('\nwrote %s' % (out / 'alloc.csv'))
