"""`bwx replay` -- concurrent multi-VM trajectory replay (no MPAM).

Design (one command, four input axes):

1. **Cases** -- `--case ID` (repeatable), `--cases a,b,c` (comma list), or
   `--case-file FILE` (txt: one id per line; json: array / {"cases": [...]} /
   structured mixes are flattened to their ids). All sources merge, order is
   kept, duplicates dropped.

2. **Concurrency** -- `--concurrency N` expands the case list to exactly N VMs:
   N < len(cases) truncates to the first N; N > len(cases) replicates
   round-robin (see cases.expand_cases). Default N = number of cases.

3. **CPU binding** -- one of:
   * `--cpu-list "0,2,4-6"` (explicit pool, split into per-VM chunks)
   * `--cpu-list-file FILE` (same, from a text or a JSON file)
   * `--cpu-start S --cpu-step P` (generate S, S+P, S+2P, ... -- auto-inference)
   * none of the above: auto-allocation from --numa-node (SMT-aware)
   Every VM gets exactly vcpus cpus (one per vCPU; Firecracker control-plane
   threads share the VM's mask -- there is no dedicated helper CPU). Pools may
   span NUMA nodes (each VM membinds to its own node set); placements are
   validated for disjointness and liveness. step=1 packs SMT siblings into a
   VM, step=2 gives each vCPU its own physical core.

4. **Fill mode** (`--fill`) -- keeps the machine loaded for the whole batch:
   the first N VMs are *real* (their records are the measurement). When a real
   VM finishes while others still run, a *filler* VM of the same case is
   launched in its CPU slot (and re-launched if the filler itself finishes
   early). When the LAST real VM finishes, all remaining fillers are killed
   (SIGKILL to their process groups). Real and filler VMs write to separate
   output trees (`real/`, `filler/`) and carry `kind: real|filler` in their
   records. Filler records are load bookkeeping, never measurement samples.
"""
import argparse
import threading
import time
from pathlib import Path

from .. import bandwidth as bw
from .. import cases as cases_mod
from .. import cpu as cpu_mod
from .. import driver
from .. import placement as placement_mod
from .. import vm as vm_mod
from ..cases import expand_cases, parse_case_file
from ..util import Error, require_root, utc_stamp, write_json

COMMAND = 'replay'
HELP = 'replay cases in concurrent Firecracker VMs (optional filler mode)'

CSV_FIELDS = ['kind', 'slot', 'case', 'language', 'status', 'verified',
              'completed_steps', 'total_steps', 'case_wall_s', 'actions_s',
              'boot_ready_s', 'vmm_wall_s', 'nonzero_actions', 'node', 'cpus',
              'vm_dir']


def add_arguments(parser):
    # -- 1. case selection ------------------------------------------------
    parser.add_argument('--case', action='append', default=[], metavar='ID',
                        help='one case id (repeatable)')
    parser.add_argument('--cases', metavar='LIST',
                        help='comma-separated case ids')
    parser.add_argument('--case-file', type=Path, metavar='FILE',
                        help='case ids from a file: .txt (one per line, # '
                             'comments) or .json (["id", ...] / {"cases": [...]} '
                             '/ structured mixes, flattened to ids)')

    # -- 2. concurrency ---------------------------------------------------
    parser.add_argument('--concurrency', type=int, metavar='N',
                        help='VMs running at once. Fewer than the case count: '
                             'truncate to the first N. More: replicate '
                             'round-robin to exactly N. Default: all cases.')

    # -- 3. cpu binding ---------------------------------------------------
    parser.add_argument('--cpu-list', metavar='LIST',
                        help='explicit cpu pool, e.g. "0,2,4-6" (split into '
                             'per-VM chunks of vcpus)')
    parser.add_argument('--cpu-list-file', type=Path, metavar='FILE',
                        help='cpu pool from a file (text: commas/spaces/lines, '
                             'a-b ranges; or JSON array / {"cpus": [...]})')
    parser.add_argument('--cpu-start', type=int, metavar='CPU',
                        help='generate cpus as start + i*step (with --cpu-step)')
    parser.add_argument('--cpu-step', type=int, metavar='N',
                        help='step for --cpu-start (1 = compact, packs SMT '
                             'siblings; 2 = one vCPU per physical core). Default 1.')

    # -- 4. fill mode -----------------------------------------------------
    parser.add_argument('--fill', action='store_true',
                        help='filler mode: keep every CPU slot loaded until the '
                             'last real VM finishes (fillers run the same case, '
                             'are killed when the batch is done, and log to '
                             'filler/ separately)')

    # -- VM / replay behaviour --------------------------------------------
    parser.add_argument('--vcpus', type=int, help='vCPU count (default from config)')
    parser.add_argument('--mem-mib', type=int, help='guest MiB (default from config)')
    parser.add_argument('--numa-node', type=int, help='NUMA node (default from config)')
    parser.add_argument('--wait', choices=('on', 'off'), default='off')
    parser.add_argument('--case-timeout', type=float)
    parser.add_argument('--boot-timeout', type=float)
    parser.add_argument('--action-timeout', type=float, default=600)
    parser.add_argument('--bandwidth', choices=('on', 'off'), default=None,
                        help='ddrc bandwidth capture (default: config '
                             'bandwidth_enabled)')
    parser.add_argument('--cpu-stats', choices=('on', 'off'), default=None,
                        help='per-CPU busy%% series (/proc/stat: pool, every '
                             'node, PSI; default: config cpu_stats_enabled)')
    parser.add_argument('--sample-interval', '--bandwidth-interval',
                        dest='sample_interval', type=float, default=1.0,
                        help='sampling interval seconds for BOTH the ddrc '
                             'bandwidth and cpu busy%% collectors (default 1)')
    parser.add_argument('--no-tools-disk', action='store_true')
    parser.add_argument('--keep-disks', action='store_true',
                        help='keep rootfs.ext4/payload.ext4 for post-mortem '
                             'inspection (default: deleted after extraction)')
    parser.add_argument('--keep-work', action='store_true', default=True)
    parser.add_argument('--discard-work', dest='keep_work', action='store_false')
    parser.add_argument('--out', type=Path, help='output dir (default logs/replay/<stamp>)')
    parser.add_argument('--dry-run', action='store_true',
                        help='print the plan (cases, expansion, cpu split) and exit')


def run(args):
    from .. import config as config_mod
    overrides = {key: getattr(args, key, None)
                 for key in ('vcpus', 'mem_mib', 'numa_node')}
    config = config_mod.load(args.config, overrides)
    config_mod.validate_runtime(config)
    require_root('replay (KVM, firecracker, /proc sampling)')

    ids = _collect_case_ids(args)
    count = args.concurrency if args.concurrency else len(ids)
    expanded = expand_cases(ids, count)
    if args.concurrency and args.concurrency != len(ids):
        note = ('expanded: %d case(s) -> %d VM(s) (%s)'
                % (len(ids), count,
                   'truncated to first %d' % count if count < len(ids)
                   else 'replicated round-robin'))
    else:
        note = '%d case(s) -> %d VM(s)' % (len(ids), count)

    out = args.out or config.logs_dir / ('replay/' + utc_stamp())
    context = driver.RunContext(config, out, label='replay')
    placements = _placements(config, args, count)
    plan = _plan_records(expanded, placements)

    print('replay plan: %s | mode=%s | nodes=%s | vcpus=%d mem=%dMiB'
          % (note, 'fill' if args.fill else 'normal', placements[0].nodes,
             config.vcpus, config.mem_mib))
    for item in plan:
        print('  VM%-2d %-45s cpus=%s' % (item['slot'], item['case'], item['cpus']))
    context.snapshot({'mode': 'fill' if args.fill else 'normal', 'cases': ids,
                      'concurrency': count, 'note': note,
                  'plan': [{'slot': i['slot'], 'case': i['case'],
                            'node': i['placement'].node,
                            'nodes': i['placement'].nodes, 'cpus': i['cpus']}
                           for i in plan],
                      'args': driver.snapshot_args(args)})
    if args.dry_run:
        return 0

    resolved = {iid: cases_mod.load_case(config, iid) for iid in dict.fromkeys(expanded)}
    if not args.no_tools_disk:
        context.require_tools_disk()

    runner = _Runner(context, args, plan, resolved, out)
    bw_on = _resolve_flag(args.bandwidth, config.bandwidth_enabled)
    cpu_on = _resolve_flag(args.cpu_stats, config.cpu_stats_enabled)
    collector, cpu_collector, epoch = None, None, time.time()
    topology = context.require_bandwidth() if bw_on else None
    try:
        if bw_on and topology is not None:
            collector = bw.BandwidthCollector(topology, out,
                                              interval=args.sample_interval,
                                              label='replay')
            if collector.start():
                epoch = time.time()
            else:
                collector = None
                print('warning: ddrc collector did not start; no bandwidth data')
        elif bw_on:
            print('warning: ddrc PMUs not available; no bandwidth data')
        if cpu_on:
            pool_cpus = {cpu for item in plan for cpu in item['placement'].cpus}
            cpu_collector = cpu_mod.CpuCollector(out,
                                                 interval=args.sample_interval,
                                                 pool_cpus=pool_cpus,
                                                 label='replay')
            cpu_collector.start()
        if args.fill:
            runner.run_fill()
        else:
            runner.run_normal()
    finally:
        records = runner.real_records + runner.filler_records
        if collector is not None:
            collector.stop()
            by_slot = {item['slot']: item['placement'] for item in plan}
            placements = [by_slot.get(r.get('slot')) for r in records]
            driver.attach_bandwidth(context, collector, records, placements, epoch)
        if cpu_collector is not None:
            cpu_collector.stop()
            driver.attach_cpu(records, cpu_collector.read(),
                              cpu_collector.epoch)

    summary = runner.summary()
    write_json(out / 'result.json', summary)
    _print_summary(summary, out)
    real_ok = [r for r in runner.real_records
               if r.get('status') in ('completed', 'budget_exhausted')]
    return 0 if len(real_ok) == len(runner.real_records) else 1


# ----------------------------------------------------------------- inputs
def _collect_case_ids(args):
    ids = list(args.case)
    if args.cases:
        ids.extend(part.strip() for part in args.cases.split(',') if part.strip())
    if args.case_file:
        ids.extend(parse_case_file(args.case_file)['ids'])
    seen, ordered = set(), []
    for iid in ids:
        if iid not in seen:
            seen.add(iid)
            ordered.append(iid)
    if not ordered:
        raise Error('no case given: pass --case, --cases or --case-file')
    return ordered


def _placements(config, args, count):
    """CPU binding: explicit pool / pool file / start+step / auto from node.

    Explicit cpu pools may span NUMA nodes (each VM membinds to its own node
    set); --numa-node is only consulted for the auto-allocation path.
    """
    vcpus = config.vcpus
    if args.cpu_start is not None:
        step = args.cpu_step if args.cpu_step else 1
        if placement_mod.node_of_cpu(args.cpu_start) is None:
            raise Error('--cpu-start %d is not an online cpu on any node'
                        % args.cpu_start)
        return placement_mod.placements_from_start_step(args.cpu_start, step,
                                                        vcpus, count)
    if args.cpu_list_file is not None:
        pool = placement_mod.read_cpu_pool(args.cpu_list_file)
    elif args.cpu_list:
        pool = placement_mod.parse_cpu_list(args.cpu_list)
    else:
        allocator = placement_mod.Allocator([config.numa_node], vcpus,
                                            mem_mib=config.mem_mib)
        return allocator.take_many(count)
    return placement_mod.placements_from_pool(pool, vcpus, count)


def _resolve_flag(cli_value, config_value):
    """CLI on/off beats the config default; unset falls back to config."""
    if cli_value == 'on':
        return True
    if cli_value == 'off':
        return False
    return bool(config_value)


def _plan_records(expanded, placements):
    if len(expanded) != len(placements):
        raise Error('plan mismatch: %d cases vs %d placements'
                    % (len(expanded), len(placements)))
    return [{'slot': index, 'case': case, 'placement': placement,
             'cpus': placement.physcpubind}
            for index, (case, placement) in enumerate(zip(expanded, placements))]


# ----------------------------------------------------------------- runner
class _Runner:
    """Launches the real VMs (and, in fill mode, the filler VMs).

    Threading model: one thread per live VM; the main thread joins the real
    threads. Filler lifecycle is decided under `lock` on real-VM completion:
    * real done, reals remaining  -> launch a filler for that slot
    * last real done              -> cancel every live filler
    A filler that finishes on its own while reals remain is relaunched (the
    point of fill mode is sustained load, so an idle slot is a bug).
    """

    def __init__(self, context, args, plan, resolved, out):
        self.context = context
        self.args = args
        self.plan = plan
        self.resolved = resolved
        self.out = out
        self.lock = threading.Lock()
        self.real_records = []
        self.filler_records = []
        self.real_done = 0
        self.filler_seq = 0
        self.stopping = False
        self.filler_runs = []          # live VmRun objects, for cancellation

    # ------------------------------------------------------------ normal
    def run_normal(self):
        threads = []
        for item in self.plan:
            thread = threading.Thread(target=self._real_worker, args=(item,),
                                      name='real-%d' % item['slot'], daemon=True)
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        self.stopping = True

    # -------------------------------------------------------------- fill
    def run_fill(self):
        threads = []
        for item in self.plan:
            thread = threading.Thread(target=self._real_worker_fill, args=(item,),
                                      name='real-%d' % item['slot'], daemon=True)
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        # Every real VM is done: withdraw the load.
        self._cancel_fillers()
        self.stopping = True
        # Give in-flight fillers a moment to record their cancellation.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and threading.active_count() > 1:
            time.sleep(0.2)

    def _real_worker(self, item):
        record = self._run_vm(item, kind='real')
        with self.lock:
            self.real_records.append(record)

    def _real_worker_fill(self, item):
        record = self._run_vm(item, kind='real')
        with self.lock:
            self.real_records.append(record)
            self.real_done += 1
            if self.real_done < len(self.plan):
                # A slot went idle while the batch is still running: keep it
                # loaded with a filler of the same case (per the fill contract).
                threading.Thread(target=self._filler_worker, args=(item,),
                                 name='filler-%d' % item['slot'],
                                 daemon=True).start()
            else:
                self._cancel_fillers()

    def _filler_worker(self, item):
        """Keep one slot loaded until the batch finishes or the filler is killed.

        Relaunches after each filler exit (a short case would otherwise leave
        the slot idle), so the loop only ends via `stopping`/cancellation.
        """
        while not self.stopping and self.real_done < len(self.plan):
            with self.lock:
                self.filler_seq += 1
                seq = self.filler_seq
            record = self._run_vm(item, kind='filler', seq=seq)
            with self.lock:
                self.filler_records.append(record)
            if self.stopping or self.real_done >= len(self.plan):
                break
            # The filler finished on its own while real VMs still run: the slot
            # would go idle, so relaunch (sustained load is the point).

    def _cancel_fillers(self):
        self.stopping = True
        for run in list(self.filler_runs):
            run.cancel()

    # ------------------------------------------------------------- shared
    def _run_vm(self, item, kind, seq=None):
        case = self.resolved[item['case']]
        config = self.context.config
        root = self.out / ('real' if kind == 'real' else 'filler')
        label = ('%s-slot%d' % (case.instance_id, item['slot']) if kind == 'real'
                 else '%s-slot%d-f%d' % (case.instance_id, item['slot'], seq))
        run = vm_mod.VmRun(
            config, instance_id=case.instance_id, steps=case.steps,
            workdir=case.workdir, placement=item['placement'],
            console_mode=config.console, console_verify=config.console_verify,
            wait=self.args.wait,
            case_timeout=self.args.case_timeout or config.case_timeout,
            boot_timeout=self.args.boot_timeout or config.boot_timeout,
            action_timeout=self.args.action_timeout,
            work_root=root, tools_disk=self.context.tools_disk, label=label,
            keep_disks=self.args.keep_disks)
        if kind == 'filler':
            self._track_filler(run)
        try:
            run.prepare(case.base)
        except Exception as exc:  # noqa: BLE001 - report, don't kill the batch
            return {'kind': kind, 'slot': item['slot'], 'case': case.instance_id,
                    'status': 'driver_error', 'error': str(exc)}
        try:
            result = run.run()
        except Exception as exc:  # noqa: BLE001
            return {'kind': kind, 'slot': item['slot'], 'case': case.instance_id,
                    'status': 'driver_error', 'error': str(exc)}
        record = result.as_dict()
        record.update({'kind': kind, 'slot': item['slot'],
                       'case': case.instance_id, 'label': label,
                       'language': case.language, 'wait': self.args.wait})
        return record

    def _track_filler(self, run):
        with self.lock:
            self.filler_runs.append(run)

    def summary(self):
        return {
            'mode': 'fill' if self.args.fill else 'normal',
            'concurrency': len(self.plan),
            'cases': [item['case'] for item in self.plan],
            'real': self.real_records,
            'filler': self.filler_records,
            'real_completed': sum(1 for r in self.real_records
                                  if r.get('status') in ('completed', 'budget_exhausted')),
            'fillers_launched': len(self.filler_records),
        }


def _print_summary(summary, out):
    print('\n--- real VMs ---')
    print('%-5s %-40s %-10s %-10s %10s %10s' %
          ('slot', 'case', 'status', 'verified', 'actions_s', 'wall_s'))
    for record in summary['real']:
        print('%-5s %-40s %-10s %-10s %10s %10s'
              % (record.get('slot'), (record.get('case') or '')[:40],
                 record.get('status'),
                 (record.get('verification') or {}).get('ok'),
                 record.get('actions_s'), record.get('vmm_wall_s')))
    if summary['mode'] == 'fill':
        print('--- filler VMs (load only, not samples) ---')
        for record in summary['filler']:
            print('%-5s %-40s %-10s %10s'
                  % (record.get('slot'), (record.get('case') or '')[:40],
                     record.get('status'), record.get('vmm_wall_s')))
        print('fillers launched: %d' % summary['fillers_launched'])
    print('\n%d/%d real VM(s) completed | output: %s'
          % (summary['real_completed'], len(summary['real']), out))
