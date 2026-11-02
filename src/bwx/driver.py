"""Run orchestration shared by every replay entry point.

Responsibilities: turn a run request into (payload prep -> MPAM group -> placement
-> VM launch -> bandwidth window -> collected record), with one code path so that
`replay`, `sweep` and `alloc` cannot drift apart in how they measure.

Timing model (what every consumer of these records must know):

* `actions_s` / `case_wall_s` come from the **guest's** `date +%s%N`, so they are
  free of host serial-forwarding and polling latency. This is the primary metric.
* `vmm_wall_s` is the host-side Popen-to-exit span, including boot and shutdown.
  Use it only for whole-run accounting, never for degradation ratios.
* The bandwidth window is clipped to [CASE_START, CASE_DONE] as observed on the
  host, so idle/boot/collect traffic does not dilute the measurement.
"""
import json
import os
import platform as _platform
import time
from pathlib import Path

from . import bandwidth as bw
from . import cases as cases_mod
from . import payload as payload_mod
from . import serialio
from . import vm as vm_mod
from .mpam import Group, Platform
from .placement import Allocator, Placement
from .util import (Error, append_jsonl, srun, utc_stamp, write_csv, write_json)


class RunContext:
    """Everything a batch of runs shares: config, platform facts, output dir."""

    def __init__(self, config, out_dir, *, label='run'):
        self.config = config
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.label = label
        self.started = time.time()
        self.platform = None
        self.topology = None
        self.loopback = None
        self.tools_disk = None

    def require_resctrl(self):
        if self.platform is None:
            self.platform = Platform(self.config.resctrl)
        return self.platform

    def require_bandwidth(self):
        """Topology + compiled helper; None when bandwidth is disabled."""
        if not self.config.bandwidth_enabled:
            return None
        if self.topology is None:
            self.topology = bw.BandwidthTopology(
                scale_bytes=self.config.ddrc_scale_bytes)
            if not self.topology.available:
                self.topology = None
        return self.topology

    def require_loopback(self):
        if self.loopback is None:
            self.loopback = payload_mod.build_loopback_tool(self.config.work_dir / 'tools')
        return self.loopback

    def require_tools_disk(self, *, with_python=True):
        """Build (once) the shared read-only tools disk; return its path or None.

        Shared across every VM in a batch: Firecracker opens it read-only, so N
        concurrent VMs cost one copy on the host. Supplies python3 for images that
        have none (3 of the 4 sample images), without shadowing an image's own.
        """
        if not with_python:
            return None
        cache = self.config.work_dir / 'tools'
        disk = cache / ('tools-%s.ext4' % _platform.machine())
        marker = cache / 'tools-meta.json'
        if disk.is_file() and marker.is_file():
            self.tools_disk = disk
            return disk
        from . import tools as tools_mod
        cache.mkdir(parents=True, exist_ok=True)
        tree = cache / 'tree'
        runtime = tools_mod.find_python_runtime(self.config)
        meta = tools_mod.build_tools_dir(tree, self.require_loopback(),
                                         python_runtime=runtime if runtime else None)
        info = tools_mod.build_tools_image(tree, disk)
        info.update(meta)
        write_json(marker, info)
        self.tools_disk = disk
        return disk

    def snapshot(self, extra=None):
        """Record the host state this batch ran under."""
        doc = {'label': self.label, 'started_utc': utc_stamp(self.started),
               'config': _config_summary(self.config)}
        if self.platform is not None:
            doc['resctrl'] = self.platform.as_dict()
        if self.topology is not None:
            doc['bandwidth_topology'] = self.topology.as_dict()
        else:
            doc['bandwidth_topology'] = {'available': False}
        doc.update(extra or {})
        write_json(self.out_dir / 'environment.json', doc)
        return doc


def _config_summary(config):
    from . import config as config_mod
    return config_mod.summary(config)


def snapshot_args(args):
    """vars(args) minus the non-serializable dispatch entries (func, Path, etc.)."""
    out = {}
    for key, value in vars(args).items():
        if key == 'func' or callable(value):
            continue
        out[key] = str(value) if isinstance(value, Path) else value
    return out


def make_group(context, name, levels, *, hard_limit=False):
    """Create (or adopt) an MPAM group and return it, applied caps read back."""
    platform = context.require_resctrl()
    group = Group(platform, name, levels, hard_limit=hard_limit,
                  output_dir=context.out_dir)
    group.create(allow_root=context.config.allow_root_group)
    if group.adopted_root:
        print('warning: no resctrl control group could be created; the cap was '
              'applied to the ROOT group (machine-wide). recorded as adopted_root.')
    return group


def run_one(context, case, *, placement, mpam_group=None, out_dir=None,
            console_mode=None, console_verify=None, wait='off',
            case_timeout=None, boot_timeout=None, action_timeout=600,
            label=None, keep_work=True):
    """Execute one VM run and return its record dict."""
    config = context.config
    console_mode = console_mode or config.console
    console_verify = config.console_verify if console_verify is None else console_verify
    case_timeout = config.case_timeout if case_timeout is None else case_timeout
    boot_timeout = config.boot_timeout if boot_timeout is None else boot_timeout
    label = label or case.instance_id
    target_root = Path(out_dir) if out_dir else context.out_dir

    run = vm_mod.VmRun(
        config, instance_id=case.instance_id, steps=case.steps, workdir=case.workdir,
        placement=placement, console_mode=console_mode, console_verify=console_verify,
        wait=wait, case_timeout=case_timeout, boot_timeout=boot_timeout,
        action_timeout=action_timeout, mpam_group=mpam_group, work_root=target_root,
        loopback_binary=None if context.tools_disk else context.require_loopback(),
        tools_disk=context.tools_disk, label=label)
    run.prepare(case.base)

    wall_launch = time.time()
    result = run.run()
    wall_end = time.time()

    record = result.as_dict()
    record.update({
        'label': label, 'language': case.language, 'workdir': case.workdir,
        'steps_total': case.step_count, 'trajectory_sha256': case.trajectory_sha,
        'image_sha256': case.image_sha, 'wait': wait,
        'case_timeout': case_timeout, 'boot_timeout': boot_timeout,
        'console_mode': console_mode, 'console_verify': console_verify,
        'vm_dir': str(run.target),
        't_launch_wall': wall_launch, 't_end_wall': wall_end,
    })
    if mpam_group is not None:
        record['mpam'] = mpam_group.as_dict()
        record['bandwidth_requested'] = mpam_group.snap()

    record['_poll_count'] = getattr(run, '_poll_count', 0)
    record['_membership_debug'] = getattr(run, '_membership_debug', None)
    record['_last_collect'] = getattr(run, '_last_collect', None)
    record['_popen_s'] = getattr(run, '_popen_s', None)
    record['_observe_s'] = getattr(run, '_observe_s', None)
    if not keep_work:
        run.cleanup()
        record['vm_dir'] = None
    return record


def attach_bandwidth(context, collector, records, placements, epoch):
    """Fill per-record bandwidth statistics from a finished collector.

    The window is clipped to each VM's CASE_START..CASE_DONE as observed by the
    host, falling back to launch..end when markers were not captured (console off).
    `epoch` is time.time() at collector start, since perf prints relative seconds.
    """
    if collector is None:
        return records
    series = collector.read()
    if not series:
        for record in records:
            record['bandwidth'] = {'available': False}
        return records
    write_json(context.out_dir / 'bandwidth-series.json',
               {str(k): v for k, v in series.items()})
    for record, placement in zip(records, placements):
        node = placement.node if placement is not None else None
        window = _active_window(record, epoch)
        stats = {}
        if window:
            keys = ['all'] if node is None else [node, 'all']
            for key in keys:
                if key in series:
                    stat = collector.window_stats(series, key, *window)
                    if stat:
                        label = 'node%d' % key if isinstance(key, int) else 'all'
                        stats[label] = stat
        record['bandwidth'] = {'available': bool(stats),
                               'window_s': list(window) if window else None, **stats}
    return records


def _active_window(record, epoch):
    """[t_start, t_end] in collector-relative seconds."""
    launch = record.get('t_launch_wall')
    end = record.get('t_end_wall')
    if launch is None or end is None:
        return None
    start = launch - epoch
    finish = end - epoch
    # Exclude boot: the trajectory's memory traffic is what we want to attribute.
    boot = record.get('boot_ready_s')
    if isinstance(boot, (int, float)) and boot > 0:
        start += boot
    if start >= finish:
        return None
    return (max(0.0, start), finish)


class GroupPool:
    """Owns the resctrl groups a command created, so cleanup cannot be forgotten.

    Groups are **created once per distinct cap and reused**, never per run. This is
    a hard platform constraint, not an optimisation: creating and removing a group
    allocates and frees a monitoring ID, and on this host `num_rmids=1` with
    `4 pmg` -- a rapid create/destroy cycle (measured: a 100-level probe loop)
    exhausts the pool, after which the kernel refuses *every* further `mkdir` with
    ENOSPC ("Out of RMIDs") and it did not recover within 60 s. So the number of
    distinct caps in a whole experiment must stay small, and groups live until the
    command finishes.
    """

    def __init__(self, context, prefix):
        self.context = context
        self.prefix = prefix
        self.groups = []
        self._by_key = {}

    def acquire(self, levels, *, hard_limit=False, hint=''):
        """Return the group for this exact cap, creating it only on first use."""
        if isinstance(levels, int):
            key = (('all', levels), hard_limit)
            name = '%s-lvl%d%s' % (self.prefix, levels, '-h' if hard_limit else '')
        else:
            key = (tuple(sorted((int(k), int(v)) for k, v in levels.items())), hard_limit)
            name = '%s-%s%s' % (self.prefix, '-'.join('d%sx%s' % kv for kv in key[0]),
                                '-h' if hard_limit else '')
        if key in self._by_key:
            return self._by_key[key]
        group = make_group(self.context, name, levels, hard_limit=hard_limit)
        self._by_key[key] = group
        self.groups.append(group)
        return group

    @property
    def created_count(self):
        return sum(1 for group in self.groups if group.created)

    def release(self):
        """Remove the groups we created. Never touches pre-existing ones."""
        errors = []
        for group in self.groups:
            if group.created:
                errors.extend(group.remove())
        self.groups = []
        self._by_key = {}
        return errors
