"""Firecracker VM launch + serial-driven trajectory replay.

This is the core of the experiment: boot a converted rootfs with a per-run payload
disk, replay a trajectory inside the guest, capture the result over serial, extract
the authoritative on-disk copy, and verify both agree.

Key decisions (each one exists because of a measured failure mode):

* `--no-api` + `--config-file`: no API socket to race against, nothing else to
  clean up. The whole VM is described by one JSON we also archive.
* The delivery rootfs is **copied** (reflink on xfs/btrfs, sparse elsewhere) and
  the copy is booted. The delivery image is never opened for write, so a crashed
  run cannot corrupt a case that took an hour to convert.
* Per-run parameters (token, timeouts, wait mode) go on the **kernel command
  line**, not into the disk. The image is therefore identical across every run of
  a case, which is what makes repeated measurements comparable.
* Serial capture is switchable (off / markers / full). Action output never goes to
  serial -- it is written to per-action log files on the payload disk -- so the
  serial stream stays a handful of protocol lines and `markers` mode costs
  essentially nothing while still allowing full verification.
* Verification is mandatory by default: Firecracker exits **0** after a
  `panic=1 reboot=k` init failure, so exit code alone cannot distinguish success
  from a dead VM.

NUMA/CPU placement: `numactl --physcpubind=<cpus> --membind=<nodes>` wraps the
VMM so every thread inherits the affinity and the memory policy (a VM whose
CPUs span NUMA nodes membinds to that node set). After launch we pin each
`fc_vcpu N` thread to its own CPU; control-plane threads (main/API/serial)
keep the VM's whole mask and share it with the vCPUs -- there is no dedicated
helper CPU. Both verifications (vcpu_pinning / guest_ram_numa) are recorded,
and a run that passed the guest but failed placement is reported as
`placement_failed` rather than silently accepted.
"""
import json
import os
import re
import resource
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import payload as payload_mod
from . import serialio
from .placement import Placement  # noqa: F401 - re-exported for callers
from .util import (Error, copy_sparse, read_json, run, sha256_file, srun,
                   write_json)

# Firecracker names its vCPU threads `fc_vcpu <n>`.
_VCPU_RE = re.compile(r'vcpu\D*(\d+)$')


def _proc_stat_jiffies(pid):
    """utime+stime (jiffies) of one process from /proc/<pid>/stat.

    Field order after the comm field: state, ppid, ..., utime, stime -- so
    with `pid (comm) ` split off, utime/stime are split-indices 11/12.
    """
    try:
        text = Path('/proc/%d/stat' % pid).read_text()
        fields = text.rsplit(')', 1)[1].split()
        return int(fields[11]) + int(fields[12])
    except (OSError, ValueError, IndexError):
        return None


def _jiffies_delta_seconds(samples):
    """First-to-last vmm_cpu_jiffies delta as CPU-seconds (None if unsampled)."""
    values = [s.get('vmm_cpu_jiffies') for s in samples
              if isinstance(s.get('vmm_cpu_jiffies'), int)]
    if len(values) < 2:
        return None
    try:
        hz = os.sysconf('SC_CLK_TCK')
    except (ValueError, OSError):
        hz = 100
    return max(0, values[-1] - values[0]) / float(hz)
_GUEST_RAM_FRACTION = 0.8


class VmResult:
    """Everything one VM run produced."""

    def __init__(self, instance_id, token):
        self.instance_id = instance_id
        self.token = token
        self.status = 'failed'
        self.verified = None
        self.fields = {}

    def as_dict(self):
        out = {'instance_id': self.instance_id, 'token': self.token,
               'status': self.fields.get('verified_status', self.status)}
        out.update(self.fields)
        return out


def build_config(*, kernel, boot_args, rootfs, payload_disk, vcpus, mem_mib,
                 tools_disk=None, firecracker_log=None, metrics_path=None):
    """Firecracker `--config-file` document. No network: guests use loopback.

    Drives: vda=rootfs (per-run copy of the delivery image), vdb=payload (per-run,
    writable, holds framework+actions+results), vdc=tools (optional, read-only,
    shared across VMs; supplies python3 for images that lack it).
    """
    config = {
        'boot-source': {'kernel_image_path': str(kernel), 'boot_args': boot_args},
        'drives': [
            {'drive_id': 'rootfs', 'path_on_host': str(rootfs),
             'is_root_device': True, 'is_read_only': False},
            {'drive_id': 'payload', 'path_on_host': str(payload_disk),
             'is_root_device': False, 'is_read_only': False},
        ],
        'machine-config': {'vcpu_count': int(vcpus), 'mem_size_mib': int(mem_mib),
                           'smt': False},
    }
    if tools_disk:
        config['drives'].append(
            {'drive_id': 'tools', 'path_on_host': str(tools_disk),
             'is_root_device': False, 'is_read_only': True})
    if firecracker_log:
        config['logger'] = {'log_path': str(firecracker_log), 'level': 'Warning',
                            'show_level': True}
    if metrics_path:
        config['metrics'] = {'metrics_path': str(metrics_path)}
    return config


class VmRun:
    """Prepare, launch, observe and collect one Firecracker replay."""

    def __init__(self, config, *, instance_id, steps, workdir, placement,
                 console_mode='markers', console_verify=True, wait='off',
                 case_timeout=1800, boot_timeout=120, action_timeout=600,
                 work_root=None, boot_args_extra='',
                 sample_interval=0.2, loopback_binary=None, tools_disk=None,
                 label=None, keep_disks=False):
        self.config = config
        self.instance_id = instance_id
        self.steps = steps
        self.workdir = workdir
        self.placement = placement
        self.console_mode = console_mode
        self.console_verify = console_verify
        self.wait = wait
        self.case_timeout = float(case_timeout)
        self.boot_timeout = float(boot_timeout)
        self.action_timeout = float(action_timeout)
        self.sample_interval = float(sample_interval)
        self.boot_args_extra = boot_args_extra
        # Filler-mode cancellation: set from another thread to stop this VM
        # (SIGKILL to the VMM's process group) at the next observe poll.
        self._cancel = threading.Event()
        # rootfs.ext4 / payload.ext4 are scratch: results are extracted from the
        # payload disk after exit, so the images themselves are disposable. A
        # converted rootfs copy costs ~1-4 GiB per run; keeping them by default
        # would multiply storage by the number of runs for no diagnostic value.
        # opt-in with keep_disks=True for post-mortem debugfs inspection.
        self.keep_disks = keep_disks
        self.loopback_binary = loopback_binary
        self.tools_disk = tools_disk
        self.token = uuid.uuid4().hex[:16]
        self.label = label or instance_id

        root = Path(work_root) if work_root else config.work_dir
        self.target = root / ('vm-%s-%s' % (self.label, self.token[:8]))
        self.process = None
        self._stop = threading.Event()

    # ------------------------------------------------------------ preparation
    def prepare(self, base_image):
        """Copy the delivery image, build the payload disk, write the FC config."""
        self.target.mkdir(parents=True, exist_ok=True)
        image_path = Path(base_image['path'])
        if not image_path.is_file():
            raise Error('base image missing: %s' % image_path)
        if base_image.get('sha256') and sha256_file(image_path) != base_image['sha256']:
            raise Error('base image %s changed since conversion (sha256 mismatch)'
                        % image_path)
        self.rootfs = self.target / 'rootfs.ext4'
        copy_sparse(image_path, self.rootfs)

        payload_dir = self.target / 'payload'
        self.payload_meta = payload_mod.write_payload_tree(
            payload_dir, self.steps, workdir=self.workdir,
            loopback_binary=self.loopback_binary)
        self.payload_disk = self.target / 'payload.ext4'
        payload_mod.build_payload_image(payload_dir, self.payload_disk)

        boot_args = serialio.boot_args(
            self.console_mode, self.token, case_timeout=int(self.case_timeout),
            boot_timeout=int(self.boot_timeout), action_timeout=int(self.action_timeout),
            wait=self.wait, extra=self.boot_args_extra)
        self.boot_args = boot_args

        fc_log = self.target / 'firecracker.log'
        metrics = self.target / 'metrics.jsonl'
        fc_log.touch()
        metrics.touch()
        vm_config = build_config(kernel=self.config.kernel, boot_args=boot_args,
                                 rootfs=self.rootfs, payload_disk=self.payload_disk,
                                 vcpus=self.config.vcpus, mem_mib=self.config.mem_mib,
                                 tools_disk=self.tools_disk,
                                 firecracker_log=fc_log, metrics_path=metrics)
        self.vm_config = vm_config
        # What Firecracker itself consumes.
        write_json(self.target / 'vm-config.json', vm_config)
        # Full run context for the experiment record.
        write_json(self.target / 'config.json',
                   {'firecracker': vm_config, 'boot_args': boot_args,
                    'token': self.token, 'instance_id': self.instance_id,
                    'placement': self.placement.as_dict(),
                    'console_mode': self.console_mode,
                    'console_verify': self.console_verify,
                    'wait': self.wait, 'case_timeout': self.case_timeout,
                    'boot_timeout': self.boot_timeout,
                    'action_timeout': self.action_timeout,
                    'payload': self.payload_meta})
        return self.target

    def launch_command(self):
        """argv for the VMM, wrapped in numactl (affinity + memory policy)."""
        return ['numactl', '--physcpubind=%s' % self.placement.physcpubind,
                '--membind=%s' % ','.join(str(n) for n in self.placement.nodes),
                str(self.config.firecracker), '--no-api', '--id', self.token,
                '--config-file', str(self.target / 'vm-config.json')]

    # ------------------------------------------------------------------ launch
    def run(self):
        """Execute the full lifecycle and return a VmResult."""
        result = VmResult(self.instance_id, self.token)
        command = self.launch_command()
        write_json(self.target / 'command.json', {'argv': command})
        captured = self.console_mode != 'off'
        console_path = self.target / 'console.raw' if captured else Path(os.devnull)
        filter_ = serialio.ConsoleFilter(self.token,
                                         keep_full_lines=self.console_mode == 'full')
        usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        pins, samples = {}, []

        launch = time.monotonic()
        # Wall-clock anchors for collector-window attribution (bandwidth/CPU):
        # set here so every caller gets them, not just the driver wrapper.
        t_launch_wall = time.time()
        host_times = {}
        console_handle = None
        try:
            if captured:
                console_handle = console_path.open('wb')
                stdout = console_handle
            else:
                stdout = subprocess.DEVNULL
            self.process = subprocess.Popen(
                command, stdout=stdout, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True)
            writer_thread = None
            if captured:
                writer_thread = threading.Thread(
                    target=self._pump_console, args=(filter_, host_times), daemon=True)
                writer_thread.start()
            self._observe(filter_, pins, samples, launch, result, host_times)
            if writer_thread is not None:
                writer_thread.join(timeout=5)
        finally:
            if console_handle is not None:
                console_handle.close()
            self._kill('cleanup')

        wall_s = time.monotonic() - launch
        usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        vmm_exit = self.process.returncode if self.process else None

        # Serial-only fields.
        result.fields.update({
            'vmm_exit_code': vmm_exit, 'vmm_wall_s': round(wall_s, 3),
            't_launch_wall': t_launch_wall,
            't_end_wall': time.time(),
            'host_user_s': round(usage_after.ru_utime - usage_before.ru_utime, 3),
            'host_system_s': round(usage_after.ru_stime - usage_before.ru_stime, 3),
            'placement': self.placement.as_dict(),
            'thread_pinning': pins,
            'vcpu_pinning_verified': self._vcpu_pinned(pins),
            'guest_ram_numa_verified': self._ram_on_node(samples),
            'memory_policy': next((s.get('memory_policy') for s in reversed(samples)
                                   if s.get('memory_policy')), None),
            'guest_ram_samples': sum(1 for s in samples if s.get('guest_ram_nodes')),
            'console': filter_.stats(),
            'samples': len(samples),
        })

        # Per-VM CPU time from the VMM's own utime+stime (immune to the
        # rusage-CHILDREN pollution that makes host_user_s/host_system_s
        # incomparable across concurrent VMs). util = CPU-seconds per
        # wall-second over vcpus+helper threads.
        cpu_s = _jiffies_delta_seconds(samples)
        if cpu_s is not None:
            result.fields['host_cpu_s'] = round(cpu_s, 3)
            if wall_s > 0:
                result.fields['host_vcpu_util'] = round(cpu_s / wall_s, 3)

        # Extract the authoritative on-disk summary from the payload disk.
        extracted = payload_mod.extract_results(self.payload_disk, self.target / 'guest')
        disk_summary = payload_mod.read_summary(extracted)
        result.fields['events'] = payload_mod.read_events(extracted)
        result.fields['action_logs'] = payload_mod.action_logs(extracted)
        if disk_summary is not None:
            result.fields['guest_summary'] = disk_summary

        # Timing: prefer the guest's own nanosecond counters (immune to host
        # serial/forwarding latency); fall back to host wall clock.
        self._apply_timing(result, filter_, disk_summary, wall_s)

        # Verify. `serial_available` is false in `off` mode, which routes
        # verification to the disk-only path.
        verification = serialio.verify(
            filter_, expected_steps=len(self.steps),
            expected_steps_crc=self.payload_meta['steps_crc'],
            disk_summary=disk_summary, vmm_exit_code=vmm_exit,
            strict=self.console_verify, serial_available=captured)
        result.verified = verification
        result.fields['verification'] = verification.as_dict()
        result.fields['markers'] = serialio.marker_table(filter_)

        # A cancelled filler VM is not a failure: it is load that was withdrawn
        # on purpose when the last real VM finished.
        if self._cancel.is_set():
            result.fields['status'] = 'cancelled'
            result.fields['verified_status'] = 'cancelled'
            result.fields['verification'] = {'ok': None, 'status': 'cancelled',
                                             'detail': 'killed as filler load',
                                             'checks': {}}
            result.status = 'cancelled'
            self._finalize_console(captured, filter_)
            self._write_result(result)
            self._remove_scratch_disks()
            return result

        # Placement failures outrank a good guest result: the measurement would
        # not reflect the intended NUMA configuration.
        status = verification.status
        if verification.ok and not (result.fields['vcpu_pinning_verified']
                                    and result.fields['guest_ram_numa_verified']):
            status = 'placement_failed'
            verification.ok = False
            verification.status = status
            verification.detail = 'vcpu_pinning=%s guest_ram_numa=%s' % (
                result.fields['vcpu_pinning_verified'],
                result.fields['guest_ram_numa_verified'])
        result.fields['verified_status'] = status
        result.status = status

        self._finalize_console(captured, filter_)
        self._write_result(result)
        self._remove_scratch_disks()
        return result

    def _remove_scratch_disks(self):
        """Unlink the scratch disk images once their contents are consumed.

        Order matters: this must run after extract_results() (the guest summary
        comes off the payload disk) and after _finalize_console. Everything else
        in the VM directory (config, console, guest results, payload tree) stays.
        """
        if self.keep_disks:
            return
        removed = []
        for name in ('rootfs.ext4', 'payload.ext4'):
            disk = self.target / name
            if disk.is_file():
                disk.unlink()
                removed.append(name)
        if removed:
            (self.target / 'disks-removed.txt').write_text(
                '\n'.join(removed) + '\n', encoding='utf-8')

    # ------------------------------------------------------------- internals
    def _pump_console(self, filter_, host_times):
        """Stream console.raw into the parser without buffering the whole file."""
        path = self.target / 'console.raw'
        deadline = time.monotonic() + self.boot_timeout + self.case_timeout + 60
        with path.open('rb') as reader:
            while time.monotonic() < deadline:
                chunk = reader.read(65536)
                if chunk:
                    for marker in filter_.feed(chunk):
                        host_times.setdefault(marker.kind, time.monotonic())
                    if filter_.first('EXIT') is not None:
                        # PID 1 announced it is about to reboot; drain and stop.
                        time.sleep(0.2)
                        filter_.feed(reader.read())
                        break
                elif self.process is not None and self.process.poll() is not None:
                    for marker in filter_.feed(reader.read()):
                        host_times.setdefault(marker.kind, time.monotonic())
                    break
                else:
                    time.sleep(self.sample_interval)
            filter_.close()

    def _observe(self, filter_, pins, samples, launch, result, host_times):
        """Poll the VMM: pin threads, sample /proc, enforce host-side timeouts."""
        boot_deadline = launch + self.boot_timeout
        case_deadline = None
        while self.process.poll() is None:
            if self._cancel.is_set():
                self._kill('cancelled')
                result.fields['forced_reason'] = 'cancelled'
                break
            now = time.monotonic()
            self._pin_threads(pins)
            samples.append(self._sample(now - launch))
            self._poll_count = getattr(self, '_poll_count', 0) + 1
            if 'CASE_START' in host_times and case_deadline is None:
                case_deadline = host_times['CASE_START'] + self.case_timeout \
                    + max(1.0, self.sample_interval)
            # Host-side fallbacks. The guest supervisor is the primary watchdog;
            # these only fire if the guest itself is wedged.
            if case_deadline is None and now > boot_deadline:
                self._kill('boot_timeout')
                result.fields['forced_reason'] = 'boot_timeout'
                break
            if case_deadline is not None and now > case_deadline:
                self._kill('case_watchdog_fallback')
                result.fields['forced_reason'] = 'case_watchdog_fallback'
                break
            time.sleep(self.sample_interval)
        self._pin_threads(pins)
        # Record boot latency: launch -> first CASE_START seen on the host.
        if 'CASE_START' in host_times:
            result.fields['boot_ready_s'] = round(host_times['CASE_START'] - launch, 3)

    def _pin_threads(self, pins):
        """Pin fc_vcpu N -> its own CPU, everything else -> helper CPU."""
        if self.process is None:
            return
        task_dir = Path('/proc') / str(self.process.pid) / 'task'
        if not task_dir.is_dir():
            return
        # The command starts as numactl, which then execs firecracker. Narrowing
        # numactl's own affinity would make it reject --physcpubind as out of range
        # and exit 1, so do nothing until the VMM binary is really in place.
        # comm is capped at 15 chars and may be "firecracker" or the binary's
        # basename (some builds report the exe name), so compare via /proc/<pid>/exe
        # as a fallback before giving up.
        try:
            comm = (task_dir.parent / 'comm').read_text().strip()
            if comm != 'firecracker':
                exe = os.readlink('/proc/%s/exe' % self.process.pid)
                if Path(exe).name != 'firecracker':
                    return
        except OSError:
            return
        vcpu_cpus = self.placement.cpus
        pinned_vcpus = 0
        for entry in task_dir.iterdir():
            tid = entry.name
            if tid in pins:
                continue
            try:
                name = (entry / 'comm').read_text().strip()
            except OSError:
                continue
            match = _VCPU_RE.search(name)
            if match:
                index = int(match.group(1))
                if index < len(vcpu_cpus):
                    self._set_affinity(tid, {vcpu_cpus[index]})
                    pins[tid] = {'name': name, 'cpu': vcpu_cpus[index], 'role': 'vcpu%d' % index}
                    pinned_vcpus += 1
            else:
                # Control-plane threads (main/API/serial) keep the VM's whole
                # mask from numactl; no dedicated helper CPU to pin them to.
                pins[tid] = {'name': name, 'cpu': None, 'role': 'helper'}

    @staticmethod
    def _set_affinity(tid, cpus):
        try:
            os.sched_setaffinity(int(tid), cpus)
        except (OSError, ProcessLookupError):
            pass

    def _vcpu_pinned(self, pins):
        count = sum(1 for p in pins.values() if p['role'].startswith('vcpu'))
        return count >= self.config.vcpus

    def _sample(self, elapsed):
        """One /proc sample: RSS, VMM cpu time, guest-RAM nodes, policy."""
        pid = self.process.pid if self.process else None
        if pid is None:
            return {}
        sample = {'elapsed_s': round(elapsed, 3)}
        try:
            sample['rss_bytes'] = int((Path('/proc/%d/statm' % pid)).read_text().split()[1]) * 4096
        except (OSError, ValueError, IndexError):
            pass
        jiffies = _proc_stat_jiffies(pid)
        if jiffies is not None:
            sample['vmm_cpu_jiffies'] = jiffies
        ram = self._guest_ram(pid)
        if ram:
            sample.update(ram)
        return sample

    def _guest_ram(self, pid):
        """Locate the guest RAM mapping and report where its pages actually live.

        The guest RAM region is identified by *virtual* size (>= 80% of
        mem_size_mib), not by resident size: Firecracker maps the whole region up
        front but the guest touches pages lazily, so early in a run only a small
        fraction is present. Matching on present pages would never fire and every
        run would be reported as a placement failure.

        Returns {'guest_ram_bytes', 'guest_ram_pages', 'guest_ram_nodes',
                 'memory_policy'}; nodes/policy are what the verification reads.
        """
        threshold = int(self.config.mem_mib * 1024 * 1024 * _GUEST_RAM_FRACTION)
        try:
            maps = Path('/proc/%d/maps' % pid).read_text().splitlines()
            numa = Path('/proc/%d/numa_maps' % pid).read_text().splitlines()
        except OSError:
            return None
        large = {}
        for line in maps:
            fields = line.split()
            low, _, high = fields[0].partition('-')
            try:
                size = int(high, 16) - int(low, 16)
            except ValueError:
                continue
            if size >= threshold:
                large[low.lower()] = size
        if not large:
            return None
        for line in numa:
            address = line.split()[0].lower()
            if address not in large:
                continue
            per_node, policy = {}, None
            for field in line.split()[1:]:
                if field.startswith('N') and '=' in field:
                    key, _, value = field.partition('=')
                    try:
                        pages = int(value)
                    except ValueError:
                        continue
                    if pages:
                        per_node[int(key[1:])] = pages
                elif ':' in field and field.split(':', 1)[0] in ('bind', 'prefer',
                                                                 'interleave', 'default'):
                    policy = field
            return {'guest_ram_bytes': large[address],
                    'guest_ram_pages': sum(per_node.values()),
                    'guest_ram_nodes': sorted(per_node), 'memory_policy': policy,
                    'guest_ram_node_pages': {str(k): v for k, v in sorted(per_node.items())}}
        return None

    def _ram_on_node(self, samples):
        """True when the guest RAM mapping was found and every present page is
        on the bound node set (cross-node VMs membind to all their nodes, so
        pages may land on any of them). Requiring at least one observation
        matters: a VM that died before its RAM was mapped must not be reported
        as correctly placed.
        """
        observed = [s for s in samples if s.get('guest_ram_nodes')]
        if not observed:
            return False
        allowed = set(self.placement.nodes)
        return all(set(s['guest_ram_nodes']) <= allowed for s in observed)

    def _apply_timing(self, result, filter_, disk_summary, wall_s):
        """Fill timing fields, preferring guest nanosecond counters.

        The guest stamps every action with `date +%s%N`, so guest-side deltas are
        free of host serial-forwarding and polling latency. Host wall clock is only
        a fallback (e.g. console mode `off`, where markers are not parsed).
        """
        if disk_summary:
            for key in ('status', 'completed_steps', 'total_steps', 'nonzero_actions',
                        'interrupted_action', 'case_wall_ns', 'actions_ns',
                        'partial_action_ns', 'max_step_ns', 'wait_ns', 'wait'):
                if key in disk_summary:
                    result.fields['guest_' + key] = disk_summary[key]
            for src, dst in (('case_wall_ns', 'case_wall_s'), ('actions_ns', 'actions_s'),
                             ('partial_action_ns', 'partial_action_s'),
                             ('max_step_ns', 'max_step_s'), ('wait_ns', 'wait_actual_s')):
                value = disk_summary.get(src)
                if isinstance(value, int):
                    result.fields[dst] = round(value / 1e9, 3)
        result.fields['vmm_wall_s'] = round(wall_s, 3)
        if 'case_wall_s' not in result.fields:
            result.fields['case_wall_s'] = round(wall_s, 3)
        # Cross-check the serial timestamps against the disk summary.
        start, done = filter_.first('CASE_START'), filter_.first('CASE_DONE')
        if start and done and start.fields and done.fields:
            try:
                start_ns, done_ns = int(start.fields[0]), int(done.fields[0])
                if done_ns >= start_ns:
                    result.fields['serial_case_wall_s'] = round((done_ns - start_ns) / 1e9, 3)
            except ValueError:
                pass

    def cancel(self):
        """Ask this VM to stop at the next observe poll (filler-mode kill)."""
        self._cancel.set()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def _kill(self, reason):
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            self.process.wait(timeout=10)

    def _finalize_console(self, captured, filter_):
        """Apply the console mode's retention policy."""
        raw = self.target / 'console.raw'
        if not captured:
            return
        if self.console_mode == 'markers':
            # Keep only protocol lines; drop the raw stream (kernel noise). On a
            # failed run keep it as console-failure.log: the raw stream is where a
            # numactl/kernel/VMM error message ends up, and losing it turns every
            # early failure into an unexplainable "guest_failed".
            marker_path = self.target / 'console-markers.log'
            marker_path.write_text(
                '\n'.join(m.raw for m in filter_.markers) + '\n', encoding='utf-8')
            if filter_.first('RESULT') is None:
                raw.rename(self.target / 'console-failure.log')
            else:
                raw.unlink(missing_ok=True)
        elif self.console_mode == 'full':
            (self.target / 'console.log').write_bytes(raw.read_bytes())
            raw.unlink(missing_ok=True)

    def _write_result(self, result):
        write_json(self.target / 'result.json', result.as_dict())

    def cleanup(self):
        """Remove the run's scratch directory (payload + rootfs copy)."""
        import shutil
        if self.target.is_dir():
            shutil.rmtree(self.target, ignore_errors=True)


def read_result(target):
    return read_json(Path(target) / 'result.json')
