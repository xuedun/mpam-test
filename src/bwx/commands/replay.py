"""`bwx replay` -- replay one case in one VM, optionally under an MPAM cap.

The single-VM path: convert must have run first. This is the building block every
other command uses, so it is kept deliberately small and side-effect free beyond
its own output directory.
"""
import time
from pathlib import Path

from .. import bandwidth as bw
from .. import cases as cases_mod
from .. import config as config_mod
from .. import driver
from .. import placement as placement_mod
from ..util import Error, require_root, utc_stamp, write_json

COMMAND = 'replay'
HELP = 'replay one case in one VM (optionally under an MPAM bandwidth cap)'


def add_arguments(parser):
    parser.add_argument('--case', required=True, metavar='ID', help='instance_id')
    parser.add_argument('--out', type=Path, help='output dir (default logs/replay/<stamp>)')
    parser.add_argument('--bandwidth', type=int, metavar='PCT',
                        help='MB cap percent for this run (1..100; 0 is refused)')
    parser.add_argument('--group', metavar='NAME',
                        help='resctrl group name (default: bwx-replay-<pct>)')
    parser.add_argument('--hard-limit', action='store_true',
                        help='also set MBHDL=1 (only if the kernel exposes it)')
    parser.add_argument('--wait', choices=('on', 'off'), default='off',
                        help='replay the trajectory delay_time between steps')
    parser.add_argument('--vcpus', type=int, help='vCPU count (default from config)')
    parser.add_argument('--mem-mib', type=int, help='guest MiB (default from config)')
    parser.add_argument('--numa-node', type=int, help='NUMA node (default from config)')
    parser.add_argument('--cpu-list', metavar='LIST',
                        help='explicit CPU list (must be on the node, vcpus+1 entries)')
    parser.add_argument('--case-timeout', type=float, help='guest execution budget')
    parser.add_argument('--boot-timeout', type=float, help='seconds to reach CASE_START')
    parser.add_argument('--action-timeout', type=float, default=600,
                        help='per-action watchdog seconds (default 600)')
    parser.add_argument('--no-tools-disk', action='store_true',
                        help='do not attach the shared python/loopback tools disk')
    parser.add_argument('--keep-work', action='store_true', default=True,
                        help='keep the VM scratch dir (default: keep)')
    parser.add_argument('--discard-work', dest='keep_work', action='store_false',
                        help='delete the VM scratch dir afterwards')
    parser.add_argument('--keep-disks', action='store_true',
                        help='keep rootfs.ext4/payload.ext4 in the VM dir for '
                             'post-mortem inspection (default: delete after result '
                             'extraction; they cost 1-4 GiB per run)')
    parser.add_argument('--bandwidth-interval', type=float, default=1.0,
                        help='ddrc sampling interval seconds (default 1)')


def run(args):
    # from_args already merges --vcpus/--mem-mib/--numa-node/--console overrides
    # because those keys are in config.DEFAULTS.
    config = config_mod.from_args(args)
    config_mod.validate_runtime(config)
    require_root('replay (KVM, resctrl writes, uncore PMU)')

    case = cases_mod.load_case(config, args.case)
    out = args.out or config.logs_dir / ('replay/' + utc_stamp())
    context = driver.RunContext(config, out, label='replay')
    context.snapshot({'case': case.as_dict(), 'args': driver.snapshot_args(args)})

    placement = _placement(config, args)
    placement_mod.Allocator.validate([placement])

    group = None
    collector = None
    topology = context.require_bandwidth()
    epoch = time.time()
    records = []
    try:
        if args.bandwidth is not None:
            name = args.group or ('bwx-replay-%d' % args.bandwidth)
            group = driver.make_group(context, name, args.bandwidth,
                                      hard_limit=args.hard_limit)
            print('mpam group %s applied=%s' % (group.name, group.snap()))
        if not args.no_tools_disk:
            context.require_tools_disk()
        if topology is not None:
            collector = bw.BandwidthCollector(topology, out,
                                              interval=args.bandwidth_interval,
                                              label='replay')
            collector.start()
            epoch = time.time()

        print('=== replay %s (%d steps, node %d, cpus %s)'
              % (case.instance_id, case.step_count, placement.node, placement.physcpubind))
        record = driver.run_one(context, case, placement=placement, mpam_group=group,
                                wait=args.wait, case_timeout=args.case_timeout,
                                boot_timeout=args.boot_timeout,
                                action_timeout=args.action_timeout,
                                label=case.instance_id, keep_work=args.keep_work,
                                keep_disks=args.keep_disks)
        records.append(record)
    finally:
        if collector is not None:
            collector.stop()
            driver.attach_bandwidth(context, collector, records, [placement], epoch)
        if group is not None and group.created:
            errors = group.remove()
            if errors:
                print('warning: group cleanup: %s' % errors)

    record = records[0] if records else {'status': 'not_run'}
    write_json(out / 'result.json', record)
    _print_summary(record, out)
    return 0 if record.get('status') in ('completed', 'budget_exhausted') else 1


def _placement(config, args):
    from ..vm import Placement
    node = args.numa_node if args.numa_node is not None else config.numa_node
    if args.cpu_list:
        cpus = bw.expand_cpulist(args.cpu_list)
        allowed = set(placement_mod.node_cpus(node))
        if not cpus <= allowed:
            raise Error('--cpu-list %s is not entirely on node %d' % (sorted(cpus), node))
        if len(cpus) < config.vcpus + 1:
            raise Error('--cpu-list needs vcpus+1=%d CPUs' % (config.vcpus + 1))
        return Placement(node, sorted(cpus))
    allocator = placement_mod.Allocator([node], config.vcpus, mem_mib=config.mem_mib)
    return allocator.take()


def _print_summary(record, out):
    print('\n--- result ---')
    for key in ('instance_id', 'status', 'verified_status', 'completed_steps',
                'total_steps', 'steps_total', 'case_wall_s', 'actions_s',
                'boot_ready_s', 'vmm_wall_s', 'vmm_exit_code', 'nonzero_actions'):
        if key in record:
            print('  %-18s %s' % (key, record[key]))
    verification = record.get('verification') or {}
    print('  %-18s %s' % ('verified', verification.get('ok')))
    print('  %-18s %s' % ('serial_verify', verification.get('serial_verification')))
    for name, value in (verification.get('checks') or {}).items():
        print('    check %-32s %s' % (name, value))
    bandwidth = record.get('bandwidth') or {}
    for key, value in bandwidth.items():
        if isinstance(value, dict):
            print('  bandwidth[%s] peak=%s mean=%s rd=%s wr=%s GB/s'
                  % (key, value.get('peak_gbs'), value.get('mean_gbs'),
                     value.get('mean_rd_gbs'), value.get('mean_wr_gbs')))
    print('  output: %s' % out)
