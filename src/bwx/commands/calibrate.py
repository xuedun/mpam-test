"""`bwx calibrate` -- Phase 0: measure the platform facts the plan must not assume.

Produces docs/EXPERIMENT-PLAN.md's `platform-calib.md` inputs:

1. **ddrc scale**: how many bytes one `flux_rd`/`flux_wr` count represents.
   Derived from a synthetic RMW load of *known* logical traffic: the probe runs
   `passes * buffer_bytes * 2` bytes of read+modify+write, and the ddrc counters
   are integrated over the same window. The ratio is the scale. If the derived
   scale is far from the configured one, the configuration is wrong (or the
   workload does not reach DRAM, e.g. it fits in LLC).
2. **MB domain <-> NUMA node mapping**: for each MB domain, generate traffic on one
   node only and see which ddrc group responds. Never assume domain i == node i.
3. **Level snap table**: request every level in the sweep set, read back what the
   kernel actually applied (this host has bandwidth_gran=9, so 1..8 all snap to 9;
   the real test environment is finer, which is exactly why the low range must be
   probed per machine).
4. **Bandwidth ceiling per level**: with the synthetic probe inside an MPAM group,
   measure achieved GB/s at each applied level. This tells you which levels are
   worth spending VM runs on (levels above the ceiling are not binding).

All four are optional individually (`--skip-*`), and every step only ever touches
resctrl groups it created itself.
"""
import json
import os
import subprocess
import time
from pathlib import Path

from .. import bandwidth as bw
from .. import config as config_mod
from .. import util
from ..mpam import Group, Platform
from ..util import Error, write_csv, write_json

COMMAND = 'calibrate'
HELP = 'Phase 0: measure ddrc scale, MB domain<->node mapping, level snap and ceiling'

PROBE_SRC = Path(__file__).resolve().parent.parent.parent.parent / 'probe' / 'bwx_probe.c'


def add_arguments(parser):
    parser.add_argument('--out', type=Path, help='output dir (default logs/calibrate/<stamp>)')
    parser.add_argument('--threads', type=int, default=8,
                        help='probe threads (default 8; more threads than the cap '
                             'can serve is safe, fewer can make the cap non-binding)')
    parser.add_argument('--buffer-mib', type=int, default=256,
                        help='per-thread RMW buffer (default 256 MiB; must exceed the '
                             'LLC slice or the probe measures cache, not DRAM)')
    parser.add_argument('--duration', type=float, default=6.0,
                        help='seconds per measurement (default 6)')
    parser.add_argument('--repeats', type=int, default=3, help='repeats per point (default 3)')
    parser.add_argument('--levels', default='',
                        help='comma-separated MB levels to probe (default: derived from '
                             'info/MB/bandwidth_gran, covering the full 1..100 range). '
                             'The snap table always reuses ONE scratch group, so a long '
                             'list is safe; the ceiling sweep creates one group per '
                             'distinct level and is therefore capped by --max-groups.')
    parser.add_argument('--max-groups', type=int, default=8,
                        help='hard cap on resctrl groups created by the ceiling sweep '
                             '(default 8). This host reports num_rmids=1, and creating '
                             'groups in a loop can exhaust the monitoring pool with no '
                             'observed recovery.')
    parser.add_argument('--node', type=int, default=None,
                        help='NUMA node for the probe (default: first node with CPUs)')
    parser.add_argument('--skip-scale', action='store_true', help='skip ddrc scale derivation')
    parser.add_argument('--skip-mapping', action='store_true',
                        help='skip MB domain<->node mapping')
    parser.add_argument('--skip-snap', action='store_true', help='skip level snap table')
    parser.add_argument('--skip-ceiling', action='store_true',
                        help='skip per-level achieved bandwidth')


def run(args):
    config = config_mod.from_args(args)
    out = args.out or config.logs_dir / ('calibrate/' + util.utc_stamp())
    out.mkdir(parents=True, exist_ok=True)
    util.require_root('calibrate (resctrl writes and uncore PMU access)')

    topology = bw.BandwidthTopology(scale_bytes=config.ddrc_scale_bytes)
    if not topology.available:
        raise Error('no ddrc PMUs found; cannot calibrate bandwidth '
                    '(set bandwidth_enabled=false to run without it)')
    bw.require_perf()
    platform_info = Platform(config.resctrl)
    node = args.node if args.node is not None else _default_node(topology)
    binary = _build_probe(out)
    levels = _levels(args, platform_info)

    report = {'out': str(out), 'node': node, 'levels': levels,
              'platform': platform_info.as_dict(), 'topology': topology.as_dict(),
              'probe': {'threads': args.threads, 'buffer_mib': args.buffer_mib,
                        'duration': args.duration, 'repeats': args.repeats}}
    print('bwx calibrate: node=%d levels=%s threads=%d buffer=%dMiB'
          % (node, levels, args.threads, args.buffer_mib))

    # -- 1. level snap table -------------------------------------------------
    if not args.skip_snap:
        snap = platform_info.level_map(levels)
        report['level_snap'] = snap
        write_json(out / 'level-snap.json', snap)
        print('\nlevel snap (requested -> applied):')
        for requested in levels:
            applied = snap[requested]
            note = '' if applied == requested else '  <-- snapped'
            print('  %3d -> %3d%s' % (requested, applied, note))
        distinct = sorted(set(snap.values()))
        report['distinct_levels'] = distinct
        print('distinct achievable levels: %s' % distinct)

    # -- 2. ddrc scale -------------------------------------------------------
    if not args.skip_scale:
        scale = _measure_scale(args, config, topology, node, binary, out)
        report['scale'] = scale
        print('\nddrc scale: derived=%s B/count configured=%s B/count ratio=%.3f'
              % (scale['derived_bytes_per_count'], scale['configured_bytes_per_count'],
                 scale['derived_bytes_per_count'] / max(1, scale['configured_bytes_per_count'])))

    # -- 3. MB domain <-> node mapping ---------------------------------------
    if not args.skip_mapping:
        mapping = _measure_mapping(args, config, topology, platform_info, node, binary, out)
        report['domain_mapping'] = mapping
        print('\nMB domain -> node response (which domains react to node %d traffic):' % node)
        for domain, detail in sorted(mapping.items(), key=lambda kv: int(kv[0])):
            print('  domain %s: %s' % (domain, detail['verdict']))

    # -- 4. per-level ceiling ------------------------------------------------
    if not args.skip_ceiling:
        ceiling = _measure_ceiling(args, config, topology, platform_info, node,
                                   binary, out, levels, report.get('level_snap', {}))
        report['ceiling'] = ceiling
        write_csv(out / 'ceiling.csv', ceiling,
                  ['requested', 'applied', 'repeat', 'gbs', 'rd_gbs', 'wr_gbs'])
        print('\nper-level achieved bandwidth (GB/s, node %d):' % node)
        by_level = {}
        for row in ceiling:
            by_level.setdefault(row['applied'], []).append(row['gbs'])
        for applied in sorted(by_level):
            values = by_level[applied]
            print('  level %3d: mean %6.2f  min %6.2f  max %6.2f  (n=%d)'
                  % (applied, sum(values) / len(values), min(values), max(values), len(values)))

    write_json(out / 'calibration.json', report)
    print('\nwrote %s' % (out / 'calibration.json'))
    return 0


# --------------------------------------------------------------------- probe
def _build_probe(out):
    """Compile the RMW bandwidth probe (static, cached)."""
    import platform as platform_mod
    import shutil
    binary = out.parent.parent / 'work' / 'tools' / ('bwx-probe-%s' % platform_mod.machine())
    binary.parent.mkdir(parents=True, exist_ok=True)
    if not PROBE_SRC.is_file():
        raise Error('probe source missing: %s' % PROBE_SRC)
    if binary.is_file() and binary.stat().st_mtime >= PROBE_SRC.stat().st_mtime:
        return binary
    compiler = shutil.which('gcc') or shutil.which('cc')
    if not compiler:
        raise Error('no C compiler; cannot build the bandwidth probe')
    result = subprocess.run([compiler, '-static', '-O2', '-pthread', '-o', str(binary),
                             str(PROBE_SRC)], capture_output=True, text=True)
    if result.returncode != 0:
        binary.unlink(missing_ok=True)
        raise Error('probe build failed: %s' % result.stderr.strip()[:400])
    os.chmod(binary, 0o755)
    return binary


def _probe_cpus(node, threads):
    """`threads` CPUs on `node`, one per physical core (no SMT siblings)."""
    from ..placement import node_cpus, select_cores
    cores = select_cores(node_cpus(node), smt=True)
    if len(cores) < threads:
        raise Error('node %d has %d single-threaded cores, need %d for the probe'
                    % (node, len(cores), threads))
    return cores[:threads]


def _run_probe(args, node, binary, cpus, *, group=None, label='', out_dir=None):
    """Run the probe under numactl (and optionally an MPAM group); return its JSON."""
    command = ['numactl', '--physcpubind=%s' % ','.join(map(str, cpus)),
               '--membind=%d' % node, str(binary),
               '--threads', str(args.threads), '--buffer-mib', str(args.buffer_mib),
               '--duration', str(args.duration)]
    if group is not None:
        command = group.join_argv(command)
    result = util.srun(command, timeout=args.duration + 120, quiet=True)
    record = None
    for line in (result.stdout or '').splitlines():
        line = line.strip()
        if line.startswith('{') and '"event"' in line:
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if doc.get('event') == 'result':
                record = doc
    if record is None:
        raise Error('probe produced no result (%s): %s' % (label, (result.stderr or '')[:300]))
    if out_dir is not None:
        write_json(Path(out_dir) / ('probe-%s.json' % label), record)
    return record


def _collect_ddrc(topology, node, duration, out_dir, label):
    """Run `perf stat` over the ddrc counters for `duration`; return per-node GB/s."""
    csv_path = Path(out_dir) / ('ddrc-%s.csv' % label)
    collector = bw.BandwidthCollector(topology, Path(out_dir), interval=1.0, label=label)
    if not collector.start():
        raise Error('perf could not start; uncore PMU access needs root')
    try:
        time.sleep(duration)
    finally:
        collector.stop()
    series = collector.read()
    stats = collector.window_stats(series, node, 1.0, duration)
    return stats or {'peak_gbs': None, 'mean_gbs': None}, series


def _default_node(topology):
    nodes = topology.nodes()
    if not nodes:
        from ..placement import node_ids
        nodes = node_ids()
    if not nodes:
        raise Error('no NUMA node with CPUs found')
    return nodes[0]


def _levels(args, platform_info):
    if args.levels.strip():
        levels = [int(x) for x in args.levels.split(',') if x.strip()]
    else:
        gran = max(1, platform_info.granularity)
        # Cover every distinct achievable level: one request per granularity step,
        # plus the endpoints. This is what makes the "1..9 are distinct on the real
        # test machine" case visible without hard-coding a level list.
        levels = sorted(set(list(range(1, min(100, gran * 12) + 1))
                            + list(range(gran, 101, gran)) + [100]))
    for level in levels:
        platform_info.check_level(level)
    return levels


def _measure_scale(args, config, topology, node, binary, out):
    """Derive bytes-per-count from a probe whose logical traffic is known."""
    cpus = _probe_cpus(node, args.threads)
    samples = []
    for index in range(args.repeats):
        label = 'scale-%d' % index
        # Start perf first, then the probe, so the counter window covers the load.
        collector = bw.BandwidthCollector(topology, out, interval=1.0, label=label)
        if not collector.start():
            raise Error('perf could not start; uncore PMU access needs root')
        try:
            record = _run_probe(args, node, binary, cpus, label=label, out_dir=out)
        finally:
            collector.stop()
        series = collector.read()
        # Trim the first and last second (ramp-up / drain).
        window = collector.window_stats(series, node, 1.5, max(2.0, args.duration - 0.5))
        if not window:
            continue
        # The probe knows its own logical traffic exactly: passes * buffer * 2 bytes
        # (one read + one write per element per pass). Comparing that against the
        # ddrc-measured rate gives the ratio measured/logical, which is 1.0 when the
        # configured scale is right AND the logical traffic really reached DRAM.
        logical_gbs = record['logical_read_write_bytes'] / max(1e-9, record['elapsed_sec']) / 1e9
        measured_gbs = window['mean_gbs']
        ratio = measured_gbs / logical_gbs if logical_gbs else None
        samples.append({'repeat': index, 'logical_gbs': round(logical_gbs, 3),
                        'measured_gbs': round(measured_gbs, 3),
                        'probe_passes': record.get('passes_total'),
                        'ratio': round(ratio, 4) if ratio else None})
    if not samples:
        raise Error('ddrc scale measurement produced no samples')
    ratios = [s['ratio'] for s in samples if s['ratio']]
    mean_ratio = sum(ratios) / len(ratios) if ratios else 1.0
    return {'configured_bytes_per_count': config.ddrc_scale_bytes,
            'derived_bytes_per_count': round(config.ddrc_scale_bytes * mean_ratio, 3),
            'mean_measured_over_logical': round(mean_ratio, 4),
            'samples': samples,
            'note': 'ratio near 1.0 means the configured scale is right and the '
                    'logical traffic reached DRAM; a stable ratio != 1 means either '
                    'the scale is wrong or traffic was absorbed (write combining, '
                    'prefetch, LLC hits). A ratio far below 1 with ratio growing as '
                    'threads increase means the load is not saturating the cap.'}


def _measure_mapping(args, config, topology, platform_info, node, binary, out):
    """Which MB domains actually throttle traffic generated on `node`?

    Method: for each MB domain, create a group that caps *only* that domain low
    (all other domains at 100), run the probe pinned to `node`, and see whether the
    achieved bandwidth drops. A domain that responds is the one backing this node.
    """
    cpus = _probe_cpus(node, min(4, args.threads))
    probe_args = _with(args, threads=len(cpus))
    baseline = _probe_gbs(probe_args, node, binary, cpus, out, 'mapping-baseline')
    mapping = {}
    for domain in platform_info.domains:
        levels = {d: (9 if d == domain else 100) for d in platform_info.domains}
        name = 'bwx-map-%s-%s' % (domain, os.getpid())
        group = Group(platform_info, name, levels)
        try:
            group.create()
            capped = _probe_gbs(probe_args, node, binary, cpus, out,
                                'mapping-d%s' % domain, group=group, topology=topology)
        finally:
            if group.created:
                group.remove()
        drop = (baseline - capped) / baseline if baseline else 0.0
        mapping[domain] = {'baseline_gbs': round(baseline, 3), 'capped_gbs': round(capped, 3),
                           'drop_fraction': round(drop, 3),
                           'verdict': 'throttles node %d' % node if drop > 0.15
                           else 'no effect on node %d' % node}
    responding = [d for d, v in mapping.items() if v['drop_fraction'] > 0.15]
    mapping['_summary'] = {'probe_node': node, 'responding_domains': responding,
                           'note': 'exactly one responding domain means MB domains map '
                                   '1:1 onto NUMA nodes on this host'}
    write_json(out / 'domain-mapping.json', mapping)
    return mapping


def _measure_ceiling(args, config, topology, platform_info, node, binary, out, levels,
                     snap):
    """Achieved GB/s at every distinct applied level.

    Only *distinct* applied levels are measured (requested levels that snap to the
    same value would produce identical caps), and the number of groups created is
    hard-capped: on this host a create/destroy loop over 100 levels permanently
    exhausted the monitoring pool.
    """
    cpus = _probe_cpus(node, args.threads)
    distinct = sorted(set(snap.values())) if snap else sorted(set(levels))
    if len(distinct) > args.max_groups:
        print('ceiling: %d distinct levels exceed --max-groups %d; probing the lowest '
              '%d (the binding region)' % (len(distinct), args.max_groups, args.max_groups))
        distinct = distinct[:args.max_groups]
    rows = []
    for applied_level in distinct:
        name = 'bwx-ceil-%d-%s' % (applied_level, os.getpid())
        group = Group(platform_info, name, applied_level)
        try:
            group.create()
            for index in range(args.repeats):
                gbs = _probe_gbs(args, node, binary, cpus, out,
                                 'ceiling-%d-%d' % (applied_level, index), group=group,
                                 topology=topology)
                rows.append({'requested': applied_level, 'applied': group.snap(),
                             'repeat': index, 'gbs': round(gbs, 3),
                             'rd_gbs': None, 'wr_gbs': None})
        finally:
            if group.created:
                group.remove()
    write_json(out / 'ceiling-raw.json', rows)
    return rows


def _probe_gbs(args, node, binary, cpus, out, label, group=None, topology=None):
    """Probe + ddrc in the same window; return the measured GB/s on `node`."""
    topology = topology or bw.BandwidthTopology(scale_bytes=32)
    collector = bw.BandwidthCollector(topology, out, interval=1.0, label=label)
    started = collector.start()
    try:
        record = _run_probe(args, node, binary, cpus, group=group, label=label, out_dir=out)
    finally:
        collector.stop()
    if not started:
        # Fall back to the probe's own logical-traffic accounting.
        return record['logical_read_write_bytes'] / max(1e-9, record['elapsed_sec']) / 1e9
    series = collector.read()
    stats = collector.window_stats(series, node, 1.5, max(2.0, args.duration - 0.5))
    if stats:
        return stats['mean_gbs']
    return record['logical_read_write_bytes'] / max(1e-9, record['elapsed_sec']) / 1e9


class _with:
    """A copy of args with some fields overridden (keeps the probe helper simple)."""

    def __init__(self, source, **overrides):
        self.__dict__.update(vars(source))
        self.__dict__.update(overrides)
